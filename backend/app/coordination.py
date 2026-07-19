"""Multi-drone coordination layer.

A small registry of named, cancellable asyncio behaviors that compose the
existing single-vehicle primitives (`mavlink.commands`, `survey.coordinated`,
the shared `vision.pipeline`) into coordinated FLEET behaviors:

  * coordinated_orbit      — both drones circle a point at staggered altitudes
  * formation_flight       — Outrider continuously holds an offset from Overwatch
  * pair_overwatch_scout   — Outrider follows a target (low), Overwatch orbits it (high)
  * search_area            — coordinated fleet survey + vision flagging
  * stop_coordination      — cancel everything and HOLD

SAFETY INVARIANT (enforced everywhere both drones are airborne together):
**Overwatch always flies higher than Outrider, with at least `SEP_M` (15 m) of
vertical separation.** Behaviors that command both drones derive Outrider's
altitude as `overwatch_alt - SEP_M` (clamped to a safe floor) and never let the
two converge.

Behaviors run as long-lived asyncio tasks at ~1-2 Hz where a continuous control
loop is needed (formation, pair). `start_behavior` cancels any existing task of
the same name before launching the new one, so re-issuing a command cleanly
supersedes the previous run.

Vision-dependent behaviors degrade gracefully: in SITL there is no camera, so
they return a clear, non-crashing error (`{"ok": False, "error": ...}`) instead
of raising.
"""
from __future__ import annotations

import asyncio
import logging

from . import safety
from .config import settings
from .mavlink import commands
from .mavlink.commands import haversine_offset
from .mavlink.registry import registry
from .survey import coordinated
from .vision import grounding
from .vision.follow import geolocate_target
from .vision.pipeline import get_pipeline

log = logging.getLogger("gcs.coordination")

# Minimum vertical separation between drones sharing airspace (metres). Overwatch
# is always placed at least this far ABOVE Outrider. Operator signed off on 15 m
# (preflight-02 finding F2): unified to a firm >= 15 m everywhere. This is the
# VERTICAL altitude floor only — the HORIZONTAL survey-zone corridor (gap_m) is a
# separate, smaller value and is NOT changed here.
SEP_M = 15.0
# Lowest altitude (AGL) we will command a drone to so it never descends into the
# ground while staggering below Overwatch. Lowered to 3 m so LOW-altitude
# formations/orbits work (the low drone, Outrider, can sit at 5 m while Overwatch
# stays 15 m above) — the operator asked for 5 m formations/orbits.
MIN_ALT_M = 3.0
# The higher (Overwatch) orbit is widened so the two circles don't intersect.
RADIUS_STAGGER_M = 8.0


# ── behavior registry ─────────────────────────────────────────────────────────
class Coordination:
    """Owns the set of running coordination behaviors keyed by name.

    Each behavior is a single asyncio.Task created from a zero-arg coroutine
    factory. Starting a behavior with a name already running cancels the old
    task first, so the latest command always wins.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def start_behavior(self, name: str, coro_factory) -> None:
        """(Re)start the named behavior. Cancels any existing same-named task,
        then schedules a fresh `asyncio.create_task(coro_factory())`."""
        self.stop(name)
        task = asyncio.create_task(coro_factory(), name=f"coord:{name}")
        task.add_done_callback(lambda t, n=name: self._on_done(n, t))
        self._tasks[name] = task
        log.info("coordination behavior started: %s", name)

    def _on_done(self, name: str, task: asyncio.Task) -> None:
        # Drop the slot only if it still points at this very task (a restart may
        # have already replaced it). Surface non-cancellation errors in the log.
        if self._tasks.get(name) is task:
            self._tasks.pop(name, None)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                log.error("coordination behavior %s crashed: %r", name, exc)

    def stop(self, name: str) -> bool:
        """Cancel a single named behavior. Returns True if one was running."""
        task = self._tasks.pop(name, None)
        if task is not None and not task.done():
            task.cancel()
            log.info("coordination behavior stopped: %s", name)
            return True
        return False

    def stop_all(self) -> list[str]:
        """Cancel every running behavior. Returns the names that were stopped."""
        names = list(self._tasks.keys())
        for name in names:
            task = self._tasks.pop(name, None)
            if task is not None and not task.done():
                task.cancel()
        if names:
            log.info("all coordination behaviors stopped: %s", names)
        return names

    def status(self) -> dict:
        """Snapshot of which behaviors are currently running."""
        running = [n for n, t in self._tasks.items() if not t.done()]
        return {"running": running, "count": len(running)}


coordination = Coordination()


# ── helpers ────────────────────────────────────────────────────────────────────
def _connected_vehicles() -> list:
    """All registry vehicles whose link reports a live connection."""
    return [v for v in registry.list() if v.link.snapshot().get("connected")]


def _vehicle(vid: str):
    """Return the registry vehicle for an id, or None if unknown."""
    try:
        return registry.get(vid)
    except KeyError:
        return None


def staggered_altitudes(overwatch_alt: float, sep_m: float = SEP_M) -> tuple[float, float]:
    """Given a desired high-band altitude for Overwatch, return
    (overwatch_alt, outrider_alt) with Overwatch at least `sep_m` ABOVE Outrider
    and Outrider clamped to a safe floor.

    If the requested Overwatch altitude is too low to fit the separation above
    the floor, Overwatch is raised so Outrider can sit at `MIN_ALT_M`.
    """
    outrider_alt = overwatch_alt - sep_m
    if outrider_alt < MIN_ALT_M:
        outrider_alt = MIN_ALT_M
        overwatch_alt = outrider_alt + sep_m
    return overwatch_alt, outrider_alt


# ── 1. coordinated orbit ────────────────────────────────────────────────────────
async def coordinated_orbit(
    lat: float | None = None,
    lon: float | None = None,
    radius_m: float = 25.0,
    altitude: float | None = None,
    override: bool = False,
) -> dict:
    """Both connected drones orbit a point at STAGGERED altitudes and radii.

    Default centre = the active drone's current position. Overwatch takes the
    high band (>= SEP_M above Outrider) and a wider radius (+RADIUS_STAGGER_M) so
    the two circles never intersect. Each vehicle gets its own `commands.orbit`,
    issued concurrently.

    SAFETY: the staggered stack is fit UNDER the max-altitude ceiling (Overwatch's
    high band <= ceiling, Outrider >= SEP_M below, >= the floor). If the ceiling is
    too low to fit the pair, it returns a clear refusal (override_required) unless
    `override` is set (which bypasses + audits it).
    """
    # Stop any running coordination loop (formation/pair) FIRST. Its ~1 Hz
    # DO_REPOSITION fights a real Orbit: it drags the centre off the requested
    # point and forces the drones to yaw toward travel instead of facing the
    # centre. Killing it lets PX4's native Orbit (yaw=face-centre) take over cleanly.
    coordination.stop_all()

    vehicles = _connected_vehicles()
    if not vehicles:
        return {"ok": False, "error": "no connected drones"}

    # Resolve the orbit centre: explicit lat/lon, else the active drone's pose.
    if lat is None or lon is None:
        try:
            active = registry.active_vehicle()
        except RuntimeError:
            return {"ok": False, "error": "no active drone for default centre"}
        s = active.link.snapshot()
        if s.get("lat") is None or s.get("lon") is None:
            return {"ok": False, "error": "no GPS fix for orbit centre"}
        lat, lon = s["lat"], s["lon"]
    lat, lon = float(lat), float(lon)

    base_radius = float(radius_m)
    # Pick a high-band altitude for Overwatch. Use the requested altitude (treated
    # as Overwatch's band) or fall back to a sensible default.
    overwatch_alt = float(altitude) if altitude is not None else 40.0
    ow_alt, our_alt = staggered_altitudes(overwatch_alt)
    # SAFETY: fit the staggered pair UNDER the max-altitude ceiling (Overwatch's
    # band is the TOP; Outrider SEP_M below, both >= the floor). Refuse with a
    # clear reason if the ceiling is too low, unless overridden.
    try:
        ow_alt, our_alt = safety.fit_stack_under_ceiling(
            ow_alt, SEP_M, MIN_ALT_M, override=override, context="coordinated_orbit")
    except safety.CeilingExceeded as exc:
        return {"ok": False, "error": str(exc), "ceiling_m": safety.get_max_altitude(),
                "override_required": True}

    ids = {v.id for v in vehicles}
    plan: list[dict] = []
    tasks = []
    for v in vehicles:
        if v.id == "overwatch":
            alt, radius = ow_alt, base_radius + RADIUS_STAGGER_M
        elif v.id == "outrider":
            alt, radius = our_alt, base_radius
        else:
            # Any other vehicle: keep it on the low band with the base radius.
            alt, radius = our_alt, base_radius
        plan.append({"vehicle": v.id, "name": v.name, "altitude": round(alt, 1),
                     "radius_m": round(radius, 1)})
        # override carried through: the stack was already fit under the ceiling
        # above, so this just keeps the command layer from re-refusing on override.
        tasks.append(commands.orbit(v.link, lat, lon, alt, radius, 4, override=override))

    # If only one of the pair is up, there's nothing to deconflict — still orbit.
    await asyncio.gather(*tasks, return_exceptions=True)
    note = "Overwatch on the high band (>= %.0f m above Outrider)." % SEP_M if "overwatch" in ids else None
    return {"ok": True, "center": [round(lat, 7), round(lon, 7)], "orbits": plan, "note": note}


# ── 2. formation flight ──────────────────────────────────────────────────────────
async def _formation_loop(offset_m: float, bearing_deg: float, period_s: float = 1.0) -> None:
    """Continuous behavior: Outrider holds a fixed (offset_m, bearing_deg) offset
    behind/around Overwatch, repositioning at ~1 Hz, always >= SEP_M BELOW it.

    `bearing_deg` is relative to Overwatch's heading (0 = ahead, 180 = behind,
    90 = right). Cancelled via the behavior registry."""
    ow = _vehicle("overwatch")
    our = _vehicle("outrider")
    if ow is None or our is None:
        log.warning("formation: overwatch/outrider not registered")
        return
    while True:
        s = ow.link.snapshot()
        if s.get("lat") is not None and s.get("lon") is not None:
            target = formation_offset_point(
                s["lat"], s["lon"], s.get("heading") or 0.0, offset_m, bearing_deg
            )
            # Outrider sits exactly SEP_M below Overwatch. The OLD code clamped
            # `our_alt = max(ow_alt - SEP_M, MIN_ALT_M)`, which SILENTLY BREAKS the
            # invariant whenever Overwatch flies low: at ow_alt == MIN_ALT_M both end
            # up at the floor (0 m separation = COLLISION), and below that Outrider is
            # commanded ABOVE Overwatch (inversion + crossing). Formation only
            # commands Outrider — it can't raise Overwatch — so if Overwatch is too
            # low to fit SEP_M above the floor, there is NO safe altitude below it:
            # SKIP this cycle (hold Outrider where it is) rather than command a
            # converging/inverted setpoint.
            ow_alt = s.get("alt_rel")
            if ow_alt is None or ow_alt - SEP_M < MIN_ALT_M:
                log.warning(
                    "formation: Overwatch alt %r too low to keep >= %.0f m above the "
                    "%.0f m floor — holding Outrider (no converging setpoint)",
                    ow_alt, SEP_M, MIN_ALT_M,
                )
                await asyncio.sleep(period_s)
                continue
            our_alt = ow_alt - SEP_M
            try:
                await commands.goto(our.link, target[0], target[1], our_alt)
            except Exception:  # noqa: BLE001
                log.exception("formation goto failed")
        await asyncio.sleep(period_s)


def formation_offset_point(
    lat: float, lon: float, heading_deg: float, offset_m: float, bearing_deg: float
) -> tuple[float, float]:
    """Point `offset_m` from (lat, lon) at `bearing_deg` RELATIVE to `heading_deg`.

    bearing 0 = directly ahead along the heading, 180 = directly behind, 90 =
    off the right wing. Returns (lat, lon) via north/east metres so it composes
    with the rest of the MAVLink layer.
    """
    import math

    absolute = math.radians(heading_deg + bearing_deg)
    north = offset_m * math.cos(absolute)
    east = offset_m * math.sin(absolute)
    return haversine_offset(lat, lon, north, east)


async def formation_flight(
    offset_m: float = 12.0, bearing_deg: float = 180.0, enable: bool = True
) -> dict:
    """Start (enable=True) or stop (enable=False) the formation behavior.

    Requires both Overwatch and Outrider connected, armed and airborne. On
    enable, launches the continuous `_formation_loop` under the behavior
    registry; on disable, cancels it. Default: 12 m behind (bearing 180°)."""
    if not enable:
        stopped = coordination.stop("formation")
        return {"ok": True, "formation": False, "was_running": stopped}

    ow = _vehicle("overwatch")
    our = _vehicle("outrider")
    if ow is None or our is None:
        return {"ok": False, "error": "formation needs both Overwatch and Outrider"}
    sow = ow.link.snapshot()
    sour = our.link.snapshot()
    if not sow.get("connected") or not sour.get("connected"):
        return {"ok": False, "error": "both drones must be connected for formation"}
    if not sow.get("armed") or not sour.get("armed"):
        return {"ok": False, "error": "both drones must be armed and airborne for formation"}

    offset_m = float(offset_m)
    bearing_deg = float(bearing_deg)
    coordination.start_behavior(
        "formation", lambda: _formation_loop(offset_m, bearing_deg)
    )
    return {
        "ok": True, "formation": True, "offset_m": offset_m, "bearing_deg": bearing_deg,
        "note": "Outrider holds %.0f m at bearing %.0f° from Overwatch, >= %.0f m below it."
                % (offset_m, bearing_deg, SEP_M),
    }


# ── 3. pair overwatch + scout ─────────────────────────────────────────────────────
async def pair_overwatch_scout(target_description: str) -> dict:
    """Outrider (low) acquires and FOLLOWS the described target via the shared
    vision pipeline; Overwatch (high) ORBITS the target's geolocated position.

    Mirrors the existing track_target + follow path, but pins the follow to
    Outrider and the orbit to Overwatch. Without a vision pipeline / camera feed
    (e.g. SITL) it returns a clear, non-crashing error."""
    pipe = get_pipeline()
    if pipe is None or not getattr(pipe, "_running", False):
        return {"ok": False, "error": (
            "pairing needs a live camera feed — start the vision pipeline first "
            "(no cameras in SITL)")}

    our = _vehicle("outrider")
    ow = _vehicle("overwatch")
    if our is None:
        return {"ok": False, "error": "Outrider not registered"}

    # H1: the scout role drives the follow via GCS-side OFFBOARD setpoints
    # (start_offboard + streamed velocity). Refuse if the scout vehicle can't take
    # GCS OFFBOARD (Outrider is a DDS-bridge vehicle) rather than switch it into
    # OFFBOARD with no setpoint stream and strand it.
    if not registry.supports_offboard(our.id):
        return {"ok": False, "vehicle": our.name, "capability": "offboard",
                "error": f"pairing needs GCS OFFBOARD follow, which {our.name} does not support",
                "note": (f"{our.name}'s tracking/follow is closed onboard, not via GCS "
                         f"OFFBOARD. Pairing refused — nothing was sent.")}

    jpeg = pipe.get_jpeg()
    if jpeg is None:
        return {"ok": False, "error": "no camera frame yet — cannot acquire target"}

    # Acquire the target box via the VLM grounder (same path as voice track_target).
    box = await grounding.resolve_target(
        jpeg, target_description, settings.grounding_backend
    )
    if not box:
        return {"ok": False, "error": f"'{target_description}' not found in view"}

    # Outrider: lock the tracker and engage follow (low scout). PIN the follow
    # setpoint target to Outrider so the capture loop's `_on_setpoint` commands
    # Outrider's link explicitly — not whatever vehicle happens to be active
    # (which is Overwatch by default). Without this the scout never follows.
    pipe.target_vehicle_id = "outrider"
    pipe.seed_tracker(box, target_description[:18])
    pipe.track_description = target_description
    pipe.set_follow(True)
    if our.link.snapshot().get("connected"):
        asyncio.create_task(commands.start_offboard(our.link))

    result = {"ok": True, "scout": "outrider", "target": target_description, "following": True}

    # Overwatch: geolocate the target and orbit it from the high band. The
    # grounder box came from the CAMERA-OWNER pipeline's frame (Overwatch's
    # feed), so we MUST geolocate through Overwatch's own pose — projecting
    # Overwatch's pixels through Outrider's position/heading/altitude would put
    # the orbit point at a geometrically meaningless location.
    if ow is not None and ow.link.snapshot().get("connected"):
        cam = ow.link.snapshot()
        s = our.link.snapshot()
        geo = geolocate_target(
            box if isinstance(box, dict) else _box_from_list(box),
            cam.get("lat"), cam.get("lon"), cam.get("alt_rel"),
            cam.get("heading") or 0.0, settings.camera_hfov_deg,
            settings.camera_pitch_deg,
        )
        if geo:
            ow_alt = max((ow.link.snapshot().get("alt_rel") or 40.0),
                         (s.get("alt_rel") or MIN_ALT_M) + SEP_M)
            await commands.orbit(ow.link, geo[0], geo[1], ow_alt, 30, 4)
            result["overwatch"] = {"orbiting": [round(geo[0], 7), round(geo[1], 7)],
                                   "altitude": round(ow_alt, 1)}
        else:
            result["overwatch_error"] = "cannot geolocate target for Overwatch orbit"
    else:
        result["overwatch_error"] = "Overwatch not connected — scout only"
    return result


def _box_from_list(box) -> dict:
    """Normalize a [x0,y0,x1,y1] grounder box into the dict geolocate expects."""
    x0, y0, x1, y1 = box
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


# ── 4. search area ─────────────────────────────────────────────────────────────────
async def search_area(size_m: float) -> dict:
    """Run the coordinated fleet survey over a `size_m`-side square centred on the
    active drone (reusing `coordinated.plan_and_fly`), and start the vision
    pipeline (if available) so detections along the lawnmower path are flagged.

    This is the same machinery as the fleet survey, framed as a SEARCH pattern."""
    try:
        active = registry.active_vehicle()
    except RuntimeError:
        return {"ok": False, "error": "no active drone"}
    s = active.link.snapshot()
    if s.get("lat") is None:
        return {"ok": False, "error": "no GPS fix"}

    side = float(size_m)
    # H2: a SEARCH flies a lawnmower MISSION per zone, so only mission-capable
    # drones get a zone. A DDS-bridge drone (Outrider) is EXCLUDED — it can't run
    # a MISSION_* upload — so the search runs on the mission-capable drone(s) only.
    vehicles = [v.id for v in _connected_vehicles() if registry.supports_missions(v.id)]
    excluded = [v.id for v in _connected_vehicles() if not registry.supports_missions(v.id)]
    if not vehicles:
        if excluded:
            names = ", ".join(registry.get(e).name for e in excluded)
            return {"ok": False, "error": "no mission-capable drones to search with",
                    "excluded": excluded,
                    "note": f"{names} cannot run survey/mission uploads."}
        return {"ok": False, "error": "no connected drones"}

    zones = coordinated.split_rect(s["lat"], s["lon"], side, side, 0.0, n=len(vehicles), gap_m=5.0)
    assignments = await coordinated.plan_and_fly(
        vehicles, zones, base_alt=30.0, line_spacing_m=25.0, sep_m=SEP_M
    )

    # Start the vision pipeline if one is configured so anything along the search
    # path gets flagged. Degrade gracefully when there is no camera (SITL).
    vision_status = "not available (no camera) — flying the pattern without detection flagging"
    pipe = get_pipeline()
    if pipe is not None:
        if not getattr(pipe, "_running", False):
            try:
                pipe.start()
            except Exception:  # noqa: BLE001
                log.exception("search_area: failed to start vision pipeline")
        if getattr(pipe, "_running", False):
            vision_status = "running — detections along the path will be flagged"

    return {
        "ok": True,
        "pattern": "search",
        "vision": vision_status,
        "excluded": excluded,
        "assignments": [
            {"vehicle": a["vehicle"], "name": a.get("name"),
             "altitude": a.get("altitude"), "polygon": a.get("polygon"),
             "error": a.get("error")}
            for a in assignments
        ],
    }


# ── stop ──────────────────────────────────────────────────────────────────────────
async def stop_coordination() -> dict:
    """Cancel ALL coordination behaviors and put every connected drone into HOLD."""
    stopped = coordination.stop_all()
    held = []
    for v in _connected_vehicles():
        try:
            await commands.hold(v.link)
            held.append(v.id)
        except Exception:  # noqa: BLE001
            log.exception("stop_coordination: hold failed for %s", v.id)
    return {"ok": True, "stopped": stopped, "holding": held}
