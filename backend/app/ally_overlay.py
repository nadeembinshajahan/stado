from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from .config import settings
from .mavlink.registry import registry
from .ws.hub import hub

log = logging.getLogger("gcs.ally_overlay")

# Publish rate for the ally-overlay loop. Kept in the 5-8 Hz band the spec asks
# for — smooth on the FPV canvas without competing with telemetry (10 Hz) or the
# map-objects loop (~3.5 Hz). The frontend fades a stale marker via its TTL.
ALLY_OVERLAY_HZ = 6.0

# Which vehicle's camera we project INTO, and which vehicle we project (the ally
# whose GPS we draw a marker for). Matches the fleet ids in config.fleet().
OBSERVER_ID = "overwatch"
ALLY_ID = "outrider"
ALLY_LABEL = "OUTRIDER"

_R_EARTH = 6378137.0  # WGS-84 equatorial radius (m), same constant follow.py uses


def project_world_to_pixel(
    obs: dict[str, Any],
    ally: dict[str, Any],
    *,
    hfov_deg: float | None = None,
    cam_pitch_deg: float | None = None,
    aspect: float = 9.0 / 16.0,
) -> dict[str, Any] | None:
    """Project the ally's GPS position into the observer's camera image.

    This is the REVERSE of vision.follow.geolocate_pixel (which maps an image
    pixel onto the ground). Here we know where the ally IS in the world and ask
    where it APPEARS in the observer's frame, so the same conventions are used
    end-to-end:

      * Heading/bearing is a COMPASS bearing (0 = North, 90 = East). A point to
        the camera's right lands at a bearing CLOCKWISE of the observer's
        heading, which maps to u > 0.5 — exactly the inverse of follow.py's
        ``bearing = heading + (cx - 0.5) * HFOV``.
      * The camera's optical axis points DOWN at a fixed depression
        ``cam_pitch_deg`` below the drone's forward horizon (the configured mount
        tilt — ``settings.camera_pitch_deg``, 15 deg for both vehicles' mounts —
        when not overridden). The observer's own ATTITUDE pitch (+nose-down) is
        folded on top, identical to follow.py's ``base = cam_pitch + drone_pitch``.
        A target lower in the image (v > 0.5) sits at a STEEPER depression — the
        inverse of follow.py's ``depression = base + (cy - 0.5) * vfov``.
      * VFOV is derived from HFOV and the video aspect (the UI feed is 16:9, so
        ``aspect = 9/16``), matching follow.py.

    Geometry: build a local-tangent-plane (ENU) vector from the observer to the
    ally using an equirectangular approximation (fine for the short ranges
    between two drones), then rotate it into the camera frame and map the
    azimuth/elevation offsets through the FOV to normalized image coords.

    Returns a dict ``{u, v, range_m, in_view, behind}`` where u,v are normalized
    image coords in [0,1] (clamped only for the returned value; ``in_view`` is
    the truthful unclamped test), ``range_m`` is the slant range in metres, and
    ``behind`` is True when the ally is behind the camera (or the geometry can't
    place it in front). Returns None if either vehicle lacks a position fix.
    """
    # Resolve FOV / mount-tilt from config when not explicitly overridden, so the
    # 15 deg downward camera tilt comes from settings.camera_pitch_deg (no stray
    # hardcoded angle that could drift from config).
    if hfov_deg is None:
        hfov_deg = settings.camera_hfov_deg
    if cam_pitch_deg is None:
        cam_pitch_deg = settings.camera_pitch_deg

    olat, olon = obs.get("lat"), obs.get("lon")
    alat, alon = ally.get("lat"), ally.get("lon")
    if olat is None or olon is None or alat is None or alon is None:
        return None

    # Altitude difference (ally relative to observer), +up. Prefer MSL (both
    # vehicles share a datum); fall back to relative altitude if MSL is missing.
    oalt = obs.get("alt_msl")
    aalt = ally.get("alt_msl")
    if oalt is None or aalt is None:
        oalt = obs.get("alt_rel")
        aalt = ally.get("alt_rel")
    dz = (aalt - oalt) if (oalt is not None and aalt is not None) else 0.0

    # ── ENU offset from observer → ally (equirectangular small-distance approx) ──
    lat_rad = math.radians(olat)
    north = math.radians(alat - olat) * _R_EARTH
    east = math.radians(alon - olon) * _R_EARTH * math.cos(lat_rad)

    horiz = math.hypot(north, east)
    slant = math.hypot(horiz, dz)
    if slant < 1e-3:
        # Co-located (or sensorless) — nothing meaningful to draw.
        return {"u": 0.5, "v": 0.5, "range_m": 0.0, "in_view": False, "behind": False}

    # Compass bearing observer→ally (0 = N, 90 = E), matching follow.py's frame.
    bearing = math.atan2(east, north)  # radians, 0 = North, +clockwise toward East

    # Heading of the observer (compass deg). Fall back to 0 (North) if unknown.
    heading = obs.get("heading")
    heading_rad = math.radians(heading) if heading is not None else 0.0

    # Azimuth offset of the ally from the camera's optical axis (the heading).
    # +az = to the camera's RIGHT. Normalize into (-pi, pi].
    az = bearing - heading_rad
    az = (az + math.pi) % (2 * math.pi) - math.pi

    # Elevation of the ally above the LOCAL HORIZON at the observer (+up).
    elev = math.atan2(dz, horiz) if horiz > 1e-6 else (math.pi / 2 if dz > 0 else -math.pi / 2)

    # Camera optical axis depression below the drone's forward horizon: the fixed
    # configured mount tilt (cam_pitch_deg, 15 deg) + the observer's nose-down
    # pitch (ATTITUDE pitch is +nose-up, so negate it), identical to follow.py's
    # base = cam_pitch + drone_pitch.
    pitch_rad = obs.get("pitch")
    drone_pitch_deg = -math.degrees(pitch_rad) if pitch_rad is not None else 0.0
    depression = math.radians(cam_pitch_deg) + math.radians(drone_pitch_deg)

    # The ally's OWN depression below the horizon (+down). elev is +up, so a
    # target below the horizon (elev < 0) has a positive depression. This is the
    # exact quantity follow.py inverts: there the ray at image row cy has
    # depression = base + (cy - 0.5) * vfov, where base == our `depression` axis.
    ally_depression = -elev

    # "Behind the camera": the ally is in front of the camera only when its
    # azimuth lies within +-90deg of the optical axis (the heading). Beyond that
    # it's behind us and can't be in the forward-looking frame.
    behind = abs(az) > (math.pi / 2)

    hfov = math.radians(hfov_deg)
    vfov = hfov * aspect

    # Map azimuth / depression offsets to normalized image coords — the exact
    # inverse of follow.py:
    #   follow.py: bearing    = heading + (cx - 0.5) * HFOV   →  u = 0.5 + az / HFOV
    #   follow.py: depression = base    + (cy - 0.5) * VFOV   →  v = 0.5 + (ally_depression - base) / VFOV
    # A target steeper than the axis (ally_depression > axis depression) lands
    # LOWER in the frame (v > 0.5); above the axis lands higher (v < 0.5). To the
    # camera's right (az > 0) lands right of center (u > 0.5).
    u = 0.5 + az / hfov
    v = 0.5 + (ally_depression - depression) / vfov

    in_view = (not behind) and (0.0 <= u <= 1.0) and (0.0 <= v <= 1.0)

    return {
        "u": round(min(1.0, max(0.0, u)), 5),
        "v": round(min(1.0, max(0.0, v)), 5),
        "range_m": round(slant, 1),
        "in_view": bool(in_view),
        "behind": bool(behind),
    }


def _build_items() -> list[dict[str, Any]]:
    """Compute the current ally-overlay items from the live vehicle registry.

    Reads BOTH the observer (Overwatch) and ally (Outrider) snapshots. Emits one
    item for the ally when a projection is possible; emits an empty list (→ the
    frontend draws nothing / lets the marker fade) whenever a fix is missing."""
    try:
        obs_v = registry.get(OBSERVER_ID)
        ally_v = registry.get(ALLY_ID)
    except KeyError:
        return []

    obs = obs_v.link.snapshot()
    ally = ally_v.link.snapshot()

    proj = project_world_to_pixel(
        obs,
        ally,
        hfov_deg=settings.camera_hfov_deg,
        cam_pitch_deg=settings.camera_pitch_deg,
    )
    if proj is None:
        return []
    return [
        {
            "id": ALLY_ID,
            "label": ALLY_LABEL,
            "u": proj["u"],
            "v": proj["v"],
            "range_m": proj["range_m"],
            "in_view": proj["in_view"],
            "behind": proj["behind"],
        }
    ]


async def ally_overlay_loop() -> None:
    """Periodically project the ally's GPS into the observer's camera frame and
    publish an `ally_overlay` event over the hub at ~6 Hz.

    Modeled on vision_api._map_objects_loop: it only READS vehicle snapshots
    (never touches a reader thread) and publishes the same way map_objects does.
    Degrades gracefully — when either vehicle has no fix it publishes nothing, so
    the frontend's TTL fades any stale marker on its own. Never crashes the
    backend: any unexpected error is logged and the loop continues."""
    hz = ALLY_OVERLAY_HZ if ALLY_OVERLAY_HZ > 0 else 6.0
    interval = 1.0 / hz
    log.info("ally-overlay loop started (%.1f Hz): %s → %s camera", hz, ALLY_ID, OBSERVER_ID)
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                items = _build_items()
            except Exception:
                log.exception("ally-overlay projection failed (continuing)")
                continue
            if not items:
                continue
            await hub.publish({"type": "ally_overlay", "items": items})
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("ally-overlay loop crashed")
    finally:
        log.info("ally-overlay loop stopped")
