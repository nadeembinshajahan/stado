from __future__ import annotations

import math
from dataclasses import asdict, dataclass


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class Setpoint:
    """Body-frame velocity command (PX4 SET_POSITION_TARGET_LOCAL_NED)."""

    vx: float  # forward, m/s
    vy: float  # right, m/s
    vz: float  # down, m/s
    yaw_rate: float  # rad/s, +cw


class FollowController:
    """Image-space follow law for a forward-looking camera.

    - horizontal offset of the target → yaw rate (turn toward it)
    - apparent height (box height)     → forward speed (hold standoff)
    - vertical offset                  → climb/descend to keep it framed
    """

    def __init__(
        self,
        target_height_frac: float = 0.28,
        max_speed: float = 5.0,
        max_yaw: float = 0.6,
        max_climb: float = 1.5,
    ) -> None:
        self.target_h = target_height_frac
        self.max_speed = max_speed
        self.max_yaw = max_yaw
        self.max_climb = max_climb
        self.k_yaw = 1.4
        self.k_fwd = 7.0
        self.k_vert = 2.0
        self.deadband = 0.04

    def compute(self, box: dict) -> Setpoint:
        cx = box["x"] + box["w"] / 2.0
        cy = box["y"] + box["h"] / 2.0
        ex = cx - 0.5  # +right
        ey = cy - 0.5  # +down
        if abs(ex) < self.deadband:
            ex = 0.0
        if abs(ey) < self.deadband:
            ey = 0.0
        size_err = self.target_h - box["h"]  # +ve ⇒ target too small ⇒ move closer

        yaw_rate = _clamp(self.k_yaw * ex, -self.max_yaw, self.max_yaw)
        vx = _clamp(self.k_fwd * size_err, -self.max_speed, self.max_speed)
        vz = _clamp(self.k_vert * ey, -self.max_climb, self.max_climb)
        return Setpoint(vx=vx, vy=0.0, vz=vz, yaw_rate=yaw_rate)


def setpoint_dict(sp: Setpoint) -> dict:
    return asdict(sp)


def geolocate_pixel(
    cx: float,
    cy: float,
    lat: float,
    lon: float,
    alt_rel: float,
    heading_deg: float,
    hfov_deg: float = 69.0,
    cam_pitch_deg: float = 15.0,
    drone_pitch_deg: float = 0.0,
    aspect: float = 9.0 / 16.0,
) -> tuple[float, float] | None:
    """Project a normalized image pixel (cx, cy ∈ [0,1]) onto a flat ground plane.

    Geometry (flat-earth, fixed-mount assumption):
      * The camera looks down at a depression angle measured below the horizon.
        ``cam_pitch_deg`` is the mount tilt (90 = straight down / nadir, 0 =
        horizon). The drone's own nose-down pitch (``drone_pitch_deg``, +down)
        adds to that depression — so a forward-pitched copter sees further out.
        The default (15°) matches the real fixed mount (config.camera_pitch_deg),
        so a caller that omits it geolocates correctly rather than as if nadir.
      * The VERTICAL pixel offset from frame-center maps to an extra/less
        depression via the vertical FOV (``hfov_deg`` × ``aspect``). Top of the
        frame = nearer the horizon (less depression), bottom = steeper.
      * Ground range = alt_rel / tan(depression) for the resulting ray; we then
        offset by the bearing = drone heading + horizontal pixel offset × HFOV.

    Returns (lat, lon) on the ground, or None when there's no GPS/altitude or the
    ray points at/above the horizon (can't hit the ground).

    Accuracy caveats: assumes flat ground at the drone's launch elevation and a
    KNOWN mount pitch (no live gimbal feedback). Error grows with terrain relief,
    shallow look angles (near the horizon), and any real gimbal offset from
    ``camera_pitch_deg``. Good enough for situational awareness, not survey-grade.
    """
    if lat is None or lon is None or alt_rel is None or alt_rel < 1:
        return None
    base = math.radians(cam_pitch_deg) + math.radians(drone_pitch_deg)
    vfov = math.radians(hfov_deg) * aspect
    # Frame center is the look axis; +cy (lower in the image) = steeper depression.
    depression = base + (cy - 0.5) * vfov
    if depression <= 0.02:
        return None  # ray at/above the horizon — never meets the ground plane
    ground_dist = alt_rel / math.tan(depression)
    bearing = math.radians(heading_deg) + (cx - 0.5) * math.radians(hfov_deg)
    north = ground_dist * math.cos(bearing)
    east = ground_dist * math.sin(bearing)
    r = 6378137.0
    tlat = lat + math.degrees(north / r)
    tlon = lon + math.degrees(east / (r * math.cos(math.radians(lat))))
    return tlat, tlon


def geolocate_box(
    box: dict,
    lat: float,
    lon: float,
    alt_rel: float,
    heading_deg: float,
    hfov_deg: float = 69.0,
    cam_pitch_deg: float = 15.0,
    drone_pitch_deg: float = 0.0,
    use_bottom: bool = True,
) -> tuple[float, float] | None:
    """Ground geolocation of a detected object's box.

    ``use_bottom`` projects the box BOTTOM-CENTER (the ground-contact point of a
    car/person standing on the ground) instead of the box center — this is the
    correct point for "where is this object on the map". Delegates to
    :func:`geolocate_pixel`. See it for the geometry + accuracy caveats.
    """
    cx = box["x"] + box["w"] / 2.0
    cy = (box["y"] + box["h"]) if use_bottom else (box["y"] + box["h"] / 2.0)
    cy = min(0.999, max(0.0, cy))
    return geolocate_pixel(
        cx, cy, lat, lon, alt_rel, heading_deg,
        hfov_deg=hfov_deg, cam_pitch_deg=cam_pitch_deg, drone_pitch_deg=drone_pitch_deg,
    )


def geolocate_target(
    box: dict,
    lat: float,
    lon: float,
    alt_rel: float,
    heading_deg: float,
    hfov_deg: float = 69.0,
    cam_pitch_deg: float = 15.0,
) -> tuple[float, float] | None:
    """Rough ground geolocation of a tracked target, projecting its box CENTER.

    Kept for the existing orbit_target flow. Uses the box center (not bottom)
    because the orbit point should sit under the target, not at its feet.
    """
    return geolocate_box(
        box, lat, lon, alt_rel, heading_deg,
        hfov_deg=hfov_deg, cam_pitch_deg=cam_pitch_deg, use_bottom=False,
    )
