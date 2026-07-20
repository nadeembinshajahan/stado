from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel

from . import flights
from . import onboard_track
from . import recorder
from . import report
from . import safety
from .autotune import manager as autotune_manager
from .config import settings
from .mavlink import commands, missions
from .mavlink.link import get_link
from .mavlink.registry import registry
from .coordination import coordination
from .survey import coordinated
from .survey.planner import clean_polygon, plan_survey

log = logging.getLogger("gcs.api")


# ── auth (OPTIONAL shared token) ─────────────────────────────────────────────
def token_ok(supplied: str | None) -> bool:
    """Return True if the supplied token satisfies the configured `api_token`.
    When `api_token` is empty (default) the surface is UNAUTHENTICATED and every
    request passes — main.py logs a prominent startup WARNING in that case."""
    expected = settings.api_token
    if not expected:
        return True
    return bool(supplied) and supplied == expected


def require_token(
    request: Request, x_api_token: str | None = Header(default=None)
) -> None:
    """FastAPI dependency for the command + logs routers. No-op when `api_token`
    is unset; otherwise demands a matching `X-API-Token` header — or a `?token=`
    query param, since browser EventSource/SSE can't set headers — and 401s when
    absent/wrong. WebSocket endpoints check `token_ok` themselves (see
    voice.voice_ws / main.py) because WS routes don't run HTTP dependencies."""
    supplied = x_api_token or request.query_params.get("token")
    if not token_ok(supplied):
        raise HTTPException(401, "missing or invalid API token")


# The command/flight router is token-gated (a no-op when api_token is unset).
router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


# ── request bodies ──────────────────────────────────────────────────────────
class TakeoffReq(BaseModel):
    altitude: float = 10.0
    vehicle: str | None = None  # specific drone id, "all"/"both", or None=active
    override: bool = False  # bypass the max-altitude ceiling (operator override)


class ModeReq(BaseModel):
    name: str


class GotoReq(BaseModel):
    lat: float
    lon: float
    alt: float = 20.0
    speed: float = -1.0
    override: bool = False  # bypass the max-altitude ceiling


class MoveReq(BaseModel):
    forward: float = 0.0  # body-frame metres (+ = along heading)
    right: float = 0.0
    up: float = 0.0
    speed: float = -1.0
    override: bool = False  # bypass the max-altitude ceiling on the derived climb


class TurnReq(BaseModel):
    degrees: float = 90.0
    direction: str = "left"  # "left" (CCW) | "right" (CW)


class OrbitReq(BaseModel):
    lat: float
    lon: float
    alt: float = 20.0
    radius: float = 20.0
    velocity: float = 3.0
    clockwise: bool = True
    override: bool = False  # bypass the max-altitude ceiling


class SetHomeReq(BaseModel):
    lat: float
    lon: float


class SpeedReq(BaseModel):
    speed: float  # m/s
    airspeed: bool = False


class SetActiveReq(BaseModel):
    id: str


class SurveyReq(BaseModel):
    polygon: list[tuple[float, float]]  # [(lat, lon), ...]
    altitude: float = 30.0
    line_spacing_m: float = 20.0
    heading_deg: float = 0.0
    execute: bool = False
    override: bool = False  # bypass the max-altitude ceiling on the survey altitude


class SurveyPlanReq(BaseModel):
    """Plan-only: tidy the polygon + compute the lawnmower preview. No upload."""
    polygon: list[tuple[float, float]]  # [(lat, lon), ...] — edited vertices
    altitude: float = 30.0
    line_spacing_m: float = 25.0
    heading_deg: float = 0.0


class CoordinatedSurveyReq(BaseModel):
    name: str | None = None
    # Either give an oriented rectangle (center + width/height) ...
    center: tuple[float, float] | None = None  # (lat, lon)
    width_m: float | None = None
    height_m: float | None = None
    heading_deg: float = 0.0
    # ... or a free polygon whose bounding box is split instead.
    polygon: list[tuple[float, float]] | None = None
    # Default: all CONNECTED vehicles in registry order.
    vehicles: list[str] | None = None
    altitude: float = 30.0
    line_spacing_m: float = 25.0
    # HORIZONTAL zone-corridor width (gap_m) between adjacent survey lanes — kept
    # small so the lawnmower coverage isn't wasted. NOTE: this does NOT set the
    # VERTICAL altitude separation, which is pinned to a firm >= 15 m everywhere
    # (preflight-02 F2, operator-approved); see plan_and_fly call below.
    min_separation_m: float = 5.0
    override: bool = False  # bypass the max-altitude ceiling on the survey altitudes


# ── telemetry / config ──────────────────────────────────────────────────────
@router.get("/state")
async def state():
    # Echo the fleet max-altitude ceiling alongside the active vehicle's state so
    # the UI can show the operator the active limit (None = no ceiling).
    return {**get_link().snapshot(), "max_altitude_m": safety.get_max_altitude()}


@router.get("/config")
async def config():
    return {
        "rtsp_url": settings.rtsp_url,
        "camera_hfov_deg": settings.camera_hfov_deg,
        # Fixed camera mount tilt: optical axis points this many degrees BELOW the
        # drone's forward horizon (15 = both vehicles' mounts; see config.py).
        "camera_pitch_deg": settings.camera_pitch_deg,
    }


# ── safety: runtime max-altitude ceiling (metres AGL) ─────────────────────────
class MaxAltitudeReq(BaseModel):
    altitude_m: float  # metres AGL


@router.get("/safety/max_altitude")
async def get_max_altitude():
    """The active fleet max-altitude ceiling (metres AGL), or null for no ceiling."""
    return {"max_altitude_m": safety.get_max_altitude()}


@router.post("/safety/max_altitude")
async def set_max_altitude(req: MaxAltitudeReq):
    """Set the runtime fleet max-altitude ceiling (metres AGL). No command may fly
    a drone above it without an explicit override. Rejects a non-positive value."""
    try:
        alt = safety.set_max_altitude(req.altitude_m)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "max_altitude_m": alt}


@router.delete("/safety/max_altitude")
async def clear_max_altitude():
    """Remove the ceiling (unlimited altitude)."""
    safety.clear_max_altitude()
    return {"ok": True, "max_altitude_m": None}


# ── demo: reset the SITL to a clean state ──────────────────────────────────
# For a public demo, judges/reviewers accumulate state during a session
# (drones airborne, coordination loops running, etc.). This endpoint hard-
# resets by asking PID 1 (the container entrypoint) to exit — Docker's
# `--restart unless-stopped` policy respawns the container from scratch,
# giving fresh PX4 SITL processes at the configured spawn coords in ≈60 s.
# Frontend triggers via the "Reset Sim" button in the status bar; caller
# should show a spinner and reload the page after the wait.
@router.post("/sim/reset")
async def reset_sim():
    """Restart the SITL container. Returns immediately; PID 1 dies ~1 s later."""
    import subprocess
    # Detached subprocess so the response makes it back to the browser BEFORE
    # PID 1 dies. `kill -TERM 1` triggers a clean container exit → docker
    # restart-policy respawn → fresh SITL boot.
    subprocess.Popen(
        ["bash", "-c", "sleep 1 && kill -TERM 1"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return {
        "ok": True,
        "message": "Restarting SITL — reload the page in about 60 seconds.",
        "downtime_s": 60,
    }


# ── safety: per-vehicle Ready-for-Flight gate ────────────────────────────────
# The gate is OFF at startup for every vehicle. While OFF, no flight-authorizing
# command (arm/takeoff/goto/orbit/mode/…) can be sent from ANY source. Recovery
# commands (LAND/RTL/HOLD/BRAKE/disarm) are exempt so a backend restart mid-flight
# never strands a drone. Turning OFF while the vehicle is armed AND alt_rel>1m is
# refused with 409 (mid-flight lock).
class ReadyForFlightReq(BaseModel):
    vehicle: str
    ready: bool


def _ready_snapshot(vid: str) -> dict:
    armed, alt_rel = _armed_alt_for(vid)
    return {
        "vehicle": vid,
        "ready": safety.is_ready(vid),
        "locked": safety.is_locked(armed, alt_rel),
    }


@router.get("/safety/ready_for_flight")
async def get_ready_for_flight():
    """Per-vehicle ready-for-flight gate state. `locked` is True when the vehicle
    is armed AND airborne (>1m alt_rel) — the frontend uses it to grey out the
    disable toggle."""
    return {"vehicles": [_ready_snapshot(v.id) for v in registry.list()]}


@router.put("/safety/ready_for_flight")
async def set_ready_for_flight(req: ReadyForFlightReq):
    """Toggle a vehicle's ready-for-flight gate. Turning OFF while the vehicle is
    armed+airborne returns 409 with the mid-flight lock reason. Turning OFF also
    tears down any running coordination / follow / onboard-track / autotune loop
    via safety.py's registered unwind hooks (see main.py:on_startup)."""
    try:
        v = registry.get(req.vehicle)
    except KeyError:
        raise HTTPException(404, f"unknown vehicle {req.vehicle}")
    armed, alt_rel = _armed_alt_for(v.id)
    try:
        safety.set_ready(v.id, req.ready, armed=armed, alt_rel=alt_rel)
    except safety.GateLocked as exc:
        raise HTTPException(
            409,
            {
                "error": "ready_for_flight_locked",
                "vehicle": v.id,
                "message": (
                    f"Cannot disable Ready-for-Flight for {v.name}: {exc.gate_reason}. "
                    f"Land or disarm first."
                ),
            },
        )
    return {"ok": True, **_ready_snapshot(v.id)}


# ── autotune (PX4 multicopter rate-controller tune) ───────────────────────────
# The operator-facing safety preconditions for autotune, surfaced in the confirm
# refusal AND echoed to the voice agent (voice.py reuses this text). Autotune is
# an in-flight axis-oscillation maneuver, so it MUST be explicitly confirmed.
AUTOTUNE_SAFETY = (
    "Autotune oscillates the drone on each axis in flight. Preconditions: the "
    "drone must be ARMED and HOVERING in a position-hold mode (Position/Altitude), "
    "in open airspace with room to wobble, and the operator ready to take manual "
    "control. It runs ~40 s; the new gains apply automatically on landing/disarm "
    "(MC_AT_APPLY=1). Re-send with confirm:true to begin."
)


class AutotuneStartReq(BaseModel):
    vehicle: str | None = None  # specific drone id; omit to use the active vehicle
    confirm: bool = False       # MUST be true — autotune is an in-flight maneuver


class AutotuneCancelReq(BaseModel):
    vehicle: str | None = None


def _autotune_target(vehicle: str | None):
    """Resolve (vehicle_id, link) for an autotune call. 404 on an unknown id;
    never a silent fall-back to the active drone for a named-but-typo'd vehicle —
    autotune must NEVER fire on the wrong drone. Omitted → the active vehicle."""
    if vehicle:
        try:
            v = registry.get(vehicle)
        except KeyError:
            raise HTTPException(404, f"unknown vehicle {vehicle}")
        return v.id, v.link
    link = get_link()
    return _vehicle_id_for(link), link


@router.post("/autotune/start")
async def autotune_start(req: AutotuneStartReq):
    """Begin PX4 autotune on a vehicle. SAFETY GATES (in order):
      * 404 if the named vehicle is unknown.
      * 409 if the vehicle is not connected (no link — nothing is sent).
      * 409 with the safety warning if `confirm` is not true (does NOT trigger on a
        bare call — autotune is an in-flight oscillation maneuver).
    A start while already running is IDEMPOTENT (returns the running state; no
    second enable is sent — only the controller's 1 Hz poll re-sends cmd 212)."""
    vid, link = _autotune_target(req.vehicle)
    _require_autotune(vid)
    # Autotune is an in-flight oscillation maneuver — always gate it.
    _require_ready(link)
    if not link.snapshot().get("connected"):
        raise HTTPException(409, f"{vid} is offline — cannot start autotune")
    if not req.confirm:
        raise HTTPException(
            409,
            detail={"ok": False, "vehicle": vid, "state": "IDLE",
                    "confirm_required": True, "reason": AUTOTUNE_SAFETY},
        )
    res = await autotune_manager.start(vid, link)
    return {"ok": True, "vehicle": vid, **res}


@router.get("/autotune/status")
async def autotune_status(vehicle: str | None = None):
    """Current autotune state. With `?vehicle=` → that drone's state (404 unknown);
    a vehicle that has never tuned reports IDLE. Without it → every controller's
    state (empty list if none has ever run)."""
    if vehicle:
        try:
            vid = registry.get(vehicle).id
        except KeyError:
            raise HTTPException(404, f"unknown vehicle {vehicle}")
        snap = autotune_manager.status(vid)
        if snap is None:
            return {"vehicle": vid, "state": "IDLE", "progress": 0, "axis": None,
                    "reason": None, "statustexts": [], "running": False}
        return snap
    return {"vehicles": autotune_manager.status_all()}


@router.post("/autotune/cancel")
async def autotune_cancel(req: AutotuneCancelReq):
    """Cancel a running autotune (sends cmd 212 with param1=0 to disable). 404 on
    an unknown vehicle. A cancel when nothing is running is a no-op."""
    vid, link = _autotune_target(req.vehicle)
    res = await autotune_manager.cancel(vid, link)
    return {"ok": True, "vehicle": vid, **res}


# ── vehicles ──────────────────────────────────────────────────────────────────
@router.get("/vehicles")
async def vehicles():
    active = registry.active_id()
    out = []
    for v in registry.list():
        snap = v.link.snapshot()
        armed, alt_rel = bool(snap.get("armed")), snap.get("alt_rel")
        out.append(
            {
                "id": v.id,
                "name": v.name,
                "kind": v.kind,
                "connected": bool(snap.get("connected")),
                "active": v.id == active,
                # Surface EXISTING capability flags so the cockpit UI can grey-out
                # actions a vehicle would refuse (preflight H1/H2). No guard logic here.
                "supports_offboard": registry.supports_offboard(v.id),
                "supports_missions": registry.supports_missions(v.id),
                "supports_autotune": registry.supports_autotune(v.id),
                # Ready-for-Flight gate (OFF at boot; auto-locked ON when armed+airborne).
                "ready_for_flight": safety.is_ready(v.id),
                "ready_for_flight_locked": safety.is_locked(armed, alt_rel),
            }
        )
    return out


@router.post("/vehicle/active")
async def set_active_vehicle(req: SetActiveReq):
    try:
        registry.set_active(req.id)
    except KeyError:
        raise HTTPException(404, f"unknown vehicle {req.id}")
    return {"ok": True, "active": registry.active_id()}


# ── flight commands ───────────────────────────────────────────────────────────
def _targets_for(vehicle: str | None):
    """Resolve a per-drone command target list: a specific vehicle id, ALL
    connected drones ('all'/'both'), or — for back-compat — the active vehicle.

    An UNKNOWN/typo'd vehicle id is a 404, NOT a silent fall-back to the active
    drone — a command meant for one drone must never execute on another (matches
    voice `dispatch`, which returns ok:false on a bad id)."""
    if vehicle in ("all", "both"):
        links = [v.link for v in registry.list() if v.link.snapshot().get("connected")]
        return links or [get_link()]
    if vehicle:
        try:
            return [registry.get(vehicle).link]
        except KeyError:
            raise HTTPException(404, f"unknown vehicle {vehicle}")
    return [get_link()]


def _vehicle_id_for(link) -> str:
    """Best-effort: map a link back to its registry vehicle id for per-target
    reporting. Falls back to the connection string."""
    for v in registry.list():
        if v.link is link:
            return v.id
    return getattr(link, "connection_string", "active")


def _require_offboard(link) -> None:
    """Capability guard (preflight H1): 422 if the link's vehicle can't take a
    GCS-side OFFBOARD command (turn / OFFBOARD mode / GCS follow). Refusing —
    instead of half-executing — stops the GCS from sending a DO_SET_MODE→OFFBOARD
    that would strand a flying DDS-bridge vehicle (Outrider) with no setpoint
    stream. The command is NEVER sent when this raises."""
    vid = registry.link_to_id(link)
    if not registry.supports_offboard(vid):
        name = registry.get(vid).name if vid else "this vehicle"
        raise HTTPException(
            422,
            f"{name} does not support GCS-side OFFBOARD — command refused (nothing sent). "
            f"Its tracking/yaw is handled onboard.",
        )


def _require_autotune(vid: str | None) -> None:
    """Capability guard: 422 if the vehicle can't run autotune over its command
    link. Outrider reaches PX4 via the DDS bridge, where autotune's cmd 212 returns
    UNSUPPORTED (reviews/autotune-over-dds-verdict.md) — triggering it would
    false-start (drone left armed/hovering, nothing tuned). Refuse up front; the
    command is NEVER sent when this raises."""
    if not registry.supports_autotune(vid):
        try:
            name = registry.get(vid).name if vid else "this vehicle"
        except KeyError:
            name = vid or "this vehicle"
        raise HTTPException(
            422,
            f"{name} does not support autotune over its command link — refused (nothing sent). "
            f"Its PX4 is reached via the DDS bridge, where autotune's cmd 212 returns UNSUPPORTED. "
            f"Tune it via the temporary MAVLink-on-TELEM2 path (reviews/autotune-mavlink-mode.md); "
            f"set OUTRIDER_SUPPORTS_AUTOTUNE=1 only while that path is active.",
        )


def _ceiling_detail(exc: safety.CeilingExceeded) -> str:
    """Map a CeilingExceeded into the operator-facing 422 detail string."""
    return str(exc)


def _require_missions(link) -> None:
    """Capability guard (preflight H2): 422 if the link's vehicle can't run the
    MISSION_* upload protocol (a DDS-bridge vehicle like Outrider). Refusing up
    front avoids a silent upload timeout / arming into an empty mission."""
    vid = registry.link_to_id(link)
    if not registry.supports_missions(vid):
        name = registry.get(vid).name if vid else "this vehicle"
        raise HTTPException(
            422,
            f"{name} cannot run survey/mission uploads over its DDS bridge — "
            f"command refused. Fly survey on a mission-capable drone.",
        )


def _armed_alt_for(vid: str) -> tuple[bool | None, float | None]:
    """Read the vehicle's armed + relative-altitude snapshot for the gate's
    airborne-lock check. Returns (None, None) if the vehicle or link is missing."""
    try:
        snap = registry.get(vid).link.snapshot()
    except Exception:  # noqa: BLE001 — best-effort telemetry read
        return None, None
    return bool(snap.get("armed")), snap.get("alt_rel")


def _require_ready(link) -> None:
    """Ready-for-Flight gate: 422 if the link's vehicle has NOT been armed for
    flight via the frontend slider (or the /safety/ready_for_flight API). This
    is the primary block against unrequested commands — every flight-authorizing
    HTTP route calls this before touching the vehicle. Recovery commands
    (LAND/RTL/HOLD/BRAKE/disarm) bypass this so they can always reach a drone."""
    vid = _vehicle_id_for(link)
    if not safety.is_ready(vid):
        name = registry.get(vid).name if vid in {v.id for v in registry.list()} else vid
        raise HTTPException(
            422,
            {
                "error": "ready_for_flight_off",
                "vehicle": vid,
                "message": (
                    f"Ready-for-Flight is OFF for {name}. Enable it in the status "
                    f"bar before commanding."
                ),
            },
        )


def _require_ready_targets(links) -> None:
    """Enforce the gate for every target link in a fleet call. Refuses the WHOLE
    call if ANY vehicle's gate is OFF — no partial fleet takeoffs. If you want
    just one drone, name it explicitly."""
    for link in links:
        _require_ready(link)


async def _arm_disarm_response(fn, force: bool, vehicle: str | None) -> dict:
    """Run arm/disarm on the resolved target(s) and report the REAL outcome.
    PX4 can reject arming on a preflight/precondition check; we wait for the
    COMMAND_ACK and surface ok:false + PX4's reason instead of a false success.
    Single target → flat {ok, ...}; fleet ('all'/'both') → per-vehicle results
    with a top-level ok that is True only if every target succeeded."""
    targets = _targets_for(vehicle)
    results: dict[str, dict] = {}
    for link in targets:
        results[_vehicle_id_for(link)] = await fn(link, force=force)
    if len(targets) == 1:
        return next(iter(results.values()))
    return {"ok": all(r.get("ok") for r in results.values()), "vehicles": results}


@router.post("/command/arm")
async def cmd_arm(force: bool = False, vehicle: str | None = None):
    # Ready-for-Flight gate: arm is the primary flight-authorizing action.
    _require_ready_targets(_targets_for(vehicle))
    return await _arm_disarm_response(commands.arm, force, vehicle)


@router.post("/command/disarm")
async def cmd_disarm(force: bool = False, vehicle: str | None = None):
    # Disarm bypasses the gate — it's a recovery/safety action. If someone flips
    # the gate off after a bad arming they still need to be able to disarm.
    return await _arm_disarm_response(commands.disarm, force, vehicle)


def _takeoff_result(ret) -> dict:
    """Normalize `commands.takeoff`'s return into {ok, reason}.

    The MAVLink layer is being updated so `takeoff` returns {ok, reason} (it
    threads the real arm ack — a takeoff whose arm was DENIED is ok:false). Until
    that lands it may still return None; treat None as a (best-effort) success for
    back-compat. NOTE: while takeoff returns None we cannot surface an arm denial,
    so the per-vehicle ok here is only as truthful as the command layer allows."""
    if isinstance(ret, dict):
        return {"ok": bool(ret.get("ok", True)), "reason": ret.get("reason")}
    return {"ok": True, "reason": None}


@router.post("/command/takeoff")
async def cmd_takeoff(req: TakeoffReq):
    """Take off to an altitude. For a single drone, climb to `altitude`. For the
    whole fleet ('all'/'both') the altitudes are STAGGERED — Overwatch always gets
    the higher band with >= the system's vertical separation — so two drones that
    launch ~10 m apart never share an altitude (the deconfliction invariant the
    coordination/voice layers enforce; reuses coordinated.assign_altitudes).

    Only CONNECTED targets are commanded (never a silent fall-back to an offline
    link). Returns per-vehicle {ok, reason, altitude}; a single drone collapses to
    a flat result."""
    fleet = req.vehicle in ("all", "both")
    if fleet:
        # Stagger: Overwatch on top, >= 15 m vertical separation (the system's
        # invariant). Only connected drones — never command an offline link.
        connected = [v for v in registry.list() if v.link.snapshot().get("connected")]
        if not connected:
            raise HTTPException(409, "no connected drones to take off")
        # Ready-for-Flight gate: every drone in the fleet takeoff must be armed.
        # Refuse the whole call if any is OFF — no partial fleet launches.
        _require_ready_targets([v.link for v in connected])
        ids = [v.id for v in connected]
        # SAFETY: fit the staggered stack UNDER the max-altitude ceiling (top <=
        # ceiling, >= 15 m sep, >= floor). Refuses (422) if the ceiling is too low
        # to fit the stack, unless override is set. No clamping silently.
        try:
            alts = coordinated.assign_altitudes_capped(
                ids, base_alt=req.altitude, sep_m=15.0, override=req.override)
        except safety.CeilingExceeded as exc:
            raise HTTPException(422, _ceiling_detail(exc))
        results: dict[str, dict] = {}
        for v in connected:
            alt = float(alts.get(v.id, req.altitude))
            ret = await commands.takeoff(v.link, alt, override=req.override)
            results[v.id] = {**_takeoff_result(ret), "altitude": alt}
        return {"ok": all(r["ok"] for r in results.values()), "vehicles": results}

    # Single target (named or active). _targets_for 404s on an unknown id and
    # never falls back to active for an offline named drone.
    targets = _targets_for(req.vehicle)
    link = targets[0]
    if not link.snapshot().get("connected"):
        raise HTTPException(409, f"{_vehicle_id_for(link)} is offline — cannot take off")
    _require_ready(link)
    try:
        ret = await commands.takeoff(link, req.altitude, override=req.override)
    except safety.CeilingExceeded as exc:
        raise HTTPException(422, _ceiling_detail(exc))
    return {**_takeoff_result(ret), "vehicle": _vehicle_id_for(link),
            "altitude": float(req.altitude)}


async def _fleet_command(fn, vehicle: str | None = None) -> dict:
    """Run a SAFETY command on the resolved target(s). LAND/RTL/HOLD/BRAKE are
    fleet-wide — a drone left airborne while another lands is a hazard, and these
    buttons previously hit only the active vehicle (so the 2nd drone was never
    commanded and ignored 'repeated' presses).

    `vehicle`: 'all'/'both' (or None, the legacy default) → EVERY connected drone,
    falling back to the active link if none report connected; a specific id → just
    that drone (404 on an unknown id, via _targets_for)."""
    # Stop any running coordination behavior FIRST. A formation/follow loop
    # repositions a drone ~1 Hz (DO_REPOSITION) and would otherwise override the
    # LAND/RTL/HOLD mode change a second later — the 'drone hovers at 8m on RTL' bug.
    stopped = coordination.stop_all()
    if stopped:
        log.info("stopped coordination before fleet command: %s", stopped)

    # A specific named drone → command only that one.
    if vehicle and vehicle not in ("all", "both"):
        link = _targets_for(vehicle)[0]  # 404s on unknown id
        vid = _vehicle_id_for(link)
        try:
            await fn(link)
            return {vid: "ok"}
        except Exception as exc:  # noqa: BLE001
            return {vid: f"error: {exc}"}

    targets = [v for v in registry.list() if v.link.snapshot().get("connected")]
    results: dict[str, str] = {}
    if not targets:
        await fn(get_link())
        return {"active": "ok"}
    for v in targets:
        try:
            await fn(v.link)
            results[v.id] = "ok"
        except Exception as exc:  # noqa: BLE001
            results[v.id] = f"error: {exc}"
    return results


@router.post("/command/land")
async def cmd_land(vehicle: str | None = None):
    return {"ok": True, "vehicles": await _fleet_command(commands.land, vehicle)}


@router.post("/command/rtl")
async def cmd_rtl(vehicle: str | None = None):
    return {"ok": True, "vehicles": await _fleet_command(commands.rtl, vehicle)}


@router.post("/command/hold")
async def cmd_hold(vehicle: str | None = None):
    return {"ok": True, "vehicles": await _fleet_command(commands.hold, vehicle)}


@router.post("/command/brake")
async def cmd_brake(vehicle: str | None = None):
    return {"ok": True, "vehicles": await _fleet_command(commands.brake, vehicle)}


@router.post("/command/mode")
async def cmd_mode(req: ModeReq):
    if req.name.upper() not in commands.C.MODES:
        raise HTTPException(400, f"unknown mode {req.name}")
    link = get_link()
    # Recovery modes (LAND/RTL/HOLD) bypass the gate — they're safety commands
    # that must always reach the drone. Any other mode change is flight-authorizing.
    if req.name.upper() not in ("LAND", "RTL", "HOLD"):
        _require_ready(link)
    # H1: never command a DDS-bridge vehicle (Outrider) into OFFBOARD — the bridge
    # can't stream setpoints, so PX4 would drop OFFBOARD in ~0.5 s and fail safe.
    if req.name.upper() == "OFFBOARD":
        _require_offboard(link)
    await commands.set_mode(link, req.name)
    return {"ok": True}


@router.post("/command/goto")
async def cmd_goto(req: GotoReq):
    link = get_link()
    _require_ready(link)
    try:
        await commands.goto(link, req.lat, req.lon, req.alt, req.speed,
                            override=req.override)
    except safety.CeilingExceeded as exc:
        raise HTTPException(422, _ceiling_detail(exc))
    return {"ok": True}


@router.post("/command/move")
async def cmd_move(req: MoveReq):
    link = get_link()
    _require_ready(link)
    try:
        res = await commands.move_relative(link, req.forward, req.right, req.up,
                                          req.speed, override=req.override)
    except safety.CeilingExceeded as exc:
        raise HTTPException(422, _ceiling_detail(exc))
    return {"ok": True, **res}


@router.post("/command/turn")
async def cmd_turn(req: TurnReq):
    # Per-vehicle turn. Overwatch (GCS-offboard) uses the precise OFFBOARD yaw-RATE
    # turn (direction guaranteed by the rate sign). Outrider (DDS, no GCS offboard —
    # the OFFBOARD turn would strand it) yaws via DO_REPOSITION to current±degrees,
    # which rides the same command path as goto/orbit over the bridge. Shortest-path
    # direction is exact for <=180°.
    link = get_link()
    _require_ready(link)
    if registry.supports_offboard(registry.link_to_id(link)):
        res = await commands.turn(link, req.degrees, req.direction)
    else:
        res = await commands.turn_to_heading(link, req.degrees, req.direction)
    return {"ok": True, **res}


@router.post("/command/orbit")
async def cmd_orbit(req: OrbitReq):
    link = get_link()
    _require_ready(link)
    try:
        await commands.orbit(
            link, req.lat, req.lon, req.alt, req.radius, req.velocity,
            req.clockwise, override=req.override,
        )
    except safety.CeilingExceeded as exc:
        raise HTTPException(422, _ceiling_detail(exc))
    return {"ok": True}


@router.post("/command/set_home")
async def cmd_set_home(req: SetHomeReq):
    link = get_link()
    _require_ready(link)
    await commands.set_home(link, req.lat, req.lon)
    return {"ok": True}


@router.post("/command/speed")
async def cmd_speed(req: SpeedReq):
    link = get_link()
    _require_ready(link)
    res = await commands.set_speed(link, req.speed, req.airspeed)
    return {"ok": True, **res}


# ── Outrider onboard tracker (Jetson, UDP :8771) — "click to track" on Outrider's
#    feed locks the tracker ONBOARD; the reticle is burned into the RGB stream. ──
class OnboardTrackReq(BaseModel):
    x: float
    y: float
    w: float
    h: float


def _require_ready_outrider() -> None:
    """Gate for the Outrider onboard-tracking endpoints. These aren't scoped by
    link but the effect is a flight action on Outrider, so use its gate."""
    if not safety.is_ready("outrider"):
        raise HTTPException(
            422,
            {
                "error": "ready_for_flight_off",
                "vehicle": "outrider",
                "message": (
                    "Ready-for-Flight is OFF for Outrider. Enable it in the "
                    "status bar before commanding the onboard tracker."
                ),
            },
        )


@router.post("/outrider/track")
async def outrider_track(req: OnboardTrackReq):
    _require_ready_outrider()
    return onboard_track.seed(req.x, req.y, req.w, req.h)


@router.post("/outrider/track/clear")
async def outrider_track_clear():
    # Clearing the tracker is a stop, not a start — do not gate.
    return onboard_track.clear()


class OnboardFollowReq(BaseModel):
    enable: bool
    # Optional per-class speed envelope to fly when enabling — a profile name
    # ("person"/"car"/"custom") OR any class word the UI has (e.g. "truck"); it's
    # normalized + validated server-side. Omitted ⇒ keep the controller's current
    # envelope. Backward-compatible: old clients send just {enable}.
    profile: str | None = None


@router.post("/outrider/follow")
async def outrider_follow(req: OnboardFollowReq):
    # `enable=False` is a stop — do not gate. `enable=True` starts a follow that
    # moves Outrider, so it's flight-authorizing.
    if req.enable:
        _require_ready_outrider()
    return onboard_track.follow(req.enable, req.profile)


class OnboardProfileReq(BaseModel):
    # Target class / profile name; normalized + validated in onboard_track.
    profile: str


@router.post("/outrider/profile")
async def outrider_profile(req: OnboardProfileReq):
    """Pre-select the onboard follow speed envelope by target class without
    changing the follow enable state (PROFILE over :8771)."""
    return onboard_track.set_profile(req.profile)


# ── local MP4 recording — capture a go2rtc restream (rtsp://127.0.0.1:8554/<stream>)
#    to <repo>/recordings/<stream>_<ts>.mp4 via ffmpeg (-c copy, clean stop). ──
class RecordReq(BaseModel):
    stream: str = "outrider"


@router.post("/record/start")
async def record_start(req: RecordReq):
    res = recorder.start(req.stream)
    if not res.get("ok"):
        raise HTTPException(500, res.get("error", "could not start recording"))
    return res


@router.post("/record/stop")
async def record_stop(req: RecordReq):
    return recorder.stop(req.stream)


@router.get("/record/status")
async def record_status():
    return recorder.status()


# ── named markers (POIs) — synced from the frontend, used as voice context ──────
class PoisReq(BaseModel):
    pois: list[dict]


@router.post("/pois")
async def set_pois(req: PoisReq):
    from . import pois as poi_store
    poi_store.set_pois(req.pois)
    return {"ok": True, "count": len(poi_store.get_pois())}


@router.get("/pois")
async def get_pois():
    from . import pois as poi_store
    return poi_store.get_pois()


# ── named search areas (regions) — synced from the frontend, used as voice context ─
class RegionsReq(BaseModel):
    regions: list[dict]


@router.post("/regions")
async def set_regions(req: RegionsReq):
    from . import regions as region_store
    region_store.set_regions(req.regions)
    return {"ok": True, "count": len(region_store.get_regions())}


@router.get("/regions")
async def get_regions():
    from . import regions as region_store
    return region_store.get_regions()


# ── missions ──────────────────────────────────────────────────────────────────
@router.post("/survey/plan")
async def survey_plan(req: SurveyPlanReq):
    """PLAN ONLY — clean the (possibly operator-edited) polygon and return the
    lawnmower survey path so the GCS can PREVIEW the planned mission before the
    operator confirms. Does NOT upload or fly anything.

    Returns the tidied polygon (so the map can snap to the clean ring), the
    ordered lawnmower turn points, and the waypoint count of the full
    takeoff→grid→RTL mission that "Confirm & fly" would upload."""
    try:
        clean = clean_polygon(req.polygon)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    grid = plan_survey(clean, req.altitude, req.line_spacing_m, req.heading_deg)
    if not grid:
        raise HTTPException(
            400, "survey produced no waypoints (polygon too small for the line spacing?)"
        )
    mission = missions.survey_mission(grid, req.altitude)
    return {
        "ok": True,
        "polygon": [[lat, lon] for lat, lon in clean],
        "path": [[w.lat, w.lon] for w in grid],
        "grid": len(grid),
        "waypoints": len(mission),
    }


class SurveyStageReq(BaseModel):
    """Stage a planned survey for confirm-then-fly (the map 'Create mission')."""
    polygon: list[tuple[float, float]]  # [(lat, lon), ...]
    label: str = "survey area"
    altitude: float = 30.0
    vehicle: str | None = None
    # The spacing the operator PREVIEWED at plan time, carried through so commit
    # flies the same grid (defaults to 25 m to match /survey/plan).
    line_spacing_m: float = 25.0


@router.post("/survey/stage")
async def survey_stage(req: SurveyStageReq):
    """Stage a planned survey mission for confirmation (no upload/fly). Shares the
    voice agent's pending-survey slot so a spoken 'confirm' flies what the map
    staged. The map already has the preview path from /survey/plan; this just
    records the staged polygon + spacing so /survey/commit (or voice
    execute_survey) flies the PREVIEWED grid."""
    # H2: refuse staging a survey for a mission-incapable drone (Outrider) so a
    # later confirm can't try (and fail) to upload a mission it can't run.
    if req.vehicle and not registry.supports_missions(req.vehicle):
        name = (registry.get(req.vehicle).name
                if req.vehicle in registry._vehicles else req.vehicle)
        raise HTTPException(
            422,
            f"{name} cannot run survey/mission uploads over its DDS bridge — "
            f"command refused. Stage the survey for a mission-capable drone.",
        )
    from .voice import stage_survey_for_confirm
    res = stage_survey_for_confirm(
        req.label, req.polygon, req.altitude, req.vehicle, req.line_spacing_m)
    if not res.get("ok"):
        raise HTTPException(400, res.get("error", "could not stage survey"))
    return res


@router.post("/survey/commit")
async def survey_commit():
    """CONFIRM & FLY the currently-staged survey mission (the map's 'Confirm &
    fly'). Mirrors voice `execute_survey`: a staged FLEET/region survey is flown
    per-drone (each zone at its previewed altitude); otherwise the single-region
    staged plan is flown. Flies the PREVIEWED grid — the staged `line_spacing_m`
    and altitude, not a hardcoded spacing. Returns 400 if nothing is staged."""
    import app.voice as voice  # read the staged missions live (module globals)
    from .ws.hub import hub

    # 1) A staged coordinated FLEET/region survey takes precedence — same branch
    #    order as voice.execute_survey, so 'Confirm & fly' flies what was previewed.
    #    CLAIM it under the lock so a concurrent voice-stage can't bleed in (M9).
    with voice._pending_survey_lock:
        fleet = voice._pending_fleet_survey
        if fleet is not None:
            voice._pending_fleet_survey = None
    if fleet:
        # Ready-for-Flight gate: every zone target vehicle must be armed for flight.
        # Any OFF gate refuses the whole commit — no partial fleet surveys.
        for z in fleet["zones"]:
            try:
                veh = registry.get(z["vehicle"])
            except KeyError:
                continue  # will be re-flagged below with a clean per-zone error
            _require_ready(veh.link)
        results: list[dict] = []
        flew = False
        for z in fleet["zones"]:
            try:
                veh = registry.get(z["vehicle"])
            except KeyError:
                results.append({"vehicle": z["vehicle"], "error": "unknown vehicle"})
                continue
            if not veh.link.snapshot().get("connected"):
                results.append({"vehicle": z["vehicle"], "name": z.get("name"),
                                "error": "link not connected"})
                continue
            alt = float(z.get("altitude", 30.0))
            spacing = float(z.get("line_spacing_m", 25.0))
            grid = plan_survey(
                [(float(a), float(b)) for a, b in z["polygon"]],
                altitude=alt, line_spacing_m=spacing)
            waypoints = missions.survey_mission(grid, alt)
            if not await missions.upload(veh.link, waypoints):
                results.append({"vehicle": z["vehicle"], "name": z.get("name"),
                                "error": "mission rejected by vehicle"})
                continue
            await missions.start(veh.link)
            flew = True
            results.append({"vehicle": z["vehicle"], "name": z.get("name"),
                            "altitude": alt, "waypoints": len(waypoints)})
        failed = sum(1 for r in results if r.get("error"))
        if not flew:
            raise HTTPException(502, "no drone accepted the survey mission")
        label = fleet["label"]
        voice.clear_pending_survey()
        hub.publish_threadsafe({"type": "fleet_zones", "label": label, "flying": True,
                                "zones": fleet["zones"]})
        hub.publish_threadsafe({"type": "survey_committed", "label": label})
        return {"ok": True, "surveying": label, "failed_zones": failed,
                "assignments": results}

    # 2) Single-region staged survey. CLAIM it under the lock (M9).
    with voice._pending_survey_lock:
        staged = voice._pending_survey
        if staged is not None:
            voice._pending_survey = None
    if not staged:
        raise HTTPException(400, "no staged survey to fly — create a mission first")
    link = (
        registry.get(staged["vehicle"]).link
        if staged.get("vehicle")
        else get_link()
    )
    # H2: refuse committing a survey to a mission-incapable drone (Outrider) — its
    # DDS bridge can't run the MISSION_* upload. Never sent, clear 422.
    _require_missions(link)
    _require_ready(link)
    alt = staged.get("altitude", 30.0)
    spacing = float(staged.get("line_spacing_m", 25.0))
    grid = plan_survey(staged["polygon"], altitude=alt, line_spacing_m=spacing)
    waypoints = missions.survey_mission(grid, alt)
    if not await missions.upload(link, waypoints):
        raise HTTPException(502, "mission upload was rejected by the vehicle")
    await missions.start(link)
    voice.clear_pending_survey()
    hub.publish_threadsafe({"type": "survey_committed", "label": staged["label"]})
    return {"ok": True, "surveying": staged["label"], "waypoints": len(waypoints)}


@router.post("/survey/cancel")
async def survey_cancel():
    """Discard a staged-but-unconfirmed survey (the map's 'Cancel')."""
    from .voice import clear_pending_survey
    clear_pending_survey()
    from .ws.hub import hub
    hub.publish_threadsafe({"type": "survey_cancelled"})
    return {"ok": True}


@router.post("/survey")
async def survey(req: SurveyReq):
    link = get_link()
    # H2: refuse a survey on a DDS-bridge vehicle (Outrider) up front — its bridge
    # can't run the MISSION_* upload, so this would otherwise time out silently.
    _require_missions(link)
    _require_ready(link)
    # SAFETY: refuse a survey whose altitude exceeds the max-altitude ceiling
    # (no silent clamp) unless the operator overrides.
    try:
        safety.check_altitude(req.altitude, override=req.override, context="survey")
    except safety.CeilingExceeded as exc:
        raise HTTPException(422, _ceiling_detail(exc))
    grid = plan_survey(req.polygon, req.altitude, req.line_spacing_m, req.heading_deg)
    if not grid:
        raise HTTPException(400, "survey produced no waypoints (check polygon/spacing)")
    # Wrap into a complete mission: takeoff → grid → RTL.
    waypoints = missions.survey_mission(grid, req.altitude)
    ok = await missions.upload(link, waypoints)
    if not ok:
        raise HTTPException(502, "mission upload was rejected by the vehicle")
    started = False
    if req.execute:
        await missions.start(link)
        started = True
    return {"ok": True, "waypoints": len(waypoints), "grid": len(grid), "started": started}


@router.post("/survey/coordinated")
async def survey_coordinated(req: CoordinatedSurveyReq):
    """Split a rectangular region into one zone per drone (with a separation
    corridor), plan a lawnmower survey per zone, upload + start each, and return
    the per-drone zone polygons + altitudes so the UI can color them."""
    # Resolve the region: either an oriented rectangle, or a polygon's bounding box.
    if req.polygon is not None:
        center_lat, center_lon, width_m, height_m, heading_deg = (
            coordinated.bbox_of_polygon(req.polygon)
        )
    elif req.center is not None and req.width_m is not None and req.height_m is not None:
        center_lat, center_lon = req.center
        width_m, height_m, heading_deg = req.width_m, req.height_m, req.heading_deg
    else:
        raise HTTPException(
            400, "provide either `polygon` or `center`+`width_m`+`height_m`"
        )

    # Default fleet = all connected vehicles, in registry order.
    # H2: a coordinated survey assigns each vehicle a lawnmower MISSION, so only
    # mission-capable drones can take a zone. A DDS-bridge drone (Outrider) is
    # EXCLUDED from the split — so a 2-drone fleet survey with Outrider present
    # surveys with the mission-capable drone(s) only rather than silently failing
    # on Outrider's zone. An EXPLICIT request for a mission-incapable vehicle is a
    # 422 (clear refusal), not a silent drop.
    if req.vehicles:
        bad = [vid for vid in req.vehicles if not registry.supports_missions(vid)]
        if bad:
            names = ", ".join(
                (registry.get(b).name if b in registry._vehicles else b) for b in bad
            )
            raise HTTPException(
                422,
                f"{names} cannot run survey/mission uploads over its DDS bridge — "
                f"command refused. Remove it from the survey or use a mission-capable drone.",
            )
        vehicles = req.vehicles
        excluded: list[str] = []
    else:
        connected = [
            v.id for v in registry.list() if v.link.snapshot().get("connected")
        ]
        vehicles = [vid for vid in connected if registry.supports_missions(vid)]
        excluded = [vid for vid in connected if not registry.supports_missions(vid)]
    if not vehicles:
        if excluded:
            names = ", ".join(registry.get(e).name for e in excluded)
            raise HTTPException(
                400,
                f"no mission-capable connected vehicles to survey with ({names} "
                f"cannot run survey/mission uploads)",
            )
        raise HTTPException(400, "no connected vehicles to survey with")

    # Ready-for-Flight gate: every selected vehicle must be armed for flight.
    _require_ready_targets([registry.get(vid).link for vid in vehicles])

    # Decouple HORIZONTAL from VERTICAL separation (preflight-02 F2). The request's
    # min_separation_m drives the HORIZONTAL zone corridor (gap_m, kept small so the
    # lawnmower lanes don't waste survey coverage). VERTICAL altitude separation is
    # pinned to a firm >= 15 m everywhere (operator signed off), regardless of the
    # requested min_separation_m, so a small horizontal corridor can never collapse
    # the altitude deconfliction.
    VERTICAL_SEP_M = 15.0
    # SAFETY: the staggered fleet survey puts the TOP drone (Overwatch) above the
    # requested base. Refuse (no clamp) if the resulting HIGHEST altitude exceeds
    # the ceiling, unless overridden — checking the derived top, not just `altitude`.
    derived = coordinated.assign_altitudes(vehicles, req.altitude, VERTICAL_SEP_M)
    try:
        safety.check_altitude(max(derived.values()) if derived else req.altitude,
                              override=req.override, context="survey_coordinated")
    except safety.CeilingExceeded as exc:
        raise HTTPException(422, _ceiling_detail(exc))
    zones = coordinated.split_rect(
        center_lat, center_lon, width_m, height_m, heading_deg,
        n=len(vehicles), gap_m=req.min_separation_m,
    )
    assignments = await coordinated.plan_and_fly(
        vehicles, zones, req.altitude, req.line_spacing_m, VERTICAL_SEP_M,
        source_polygon=req.polygon,
    )
    # Surface a top-level ok + failed-zone count so a caller checking only the
    # response shape can't mistake a partial/total per-zone rejection for success.
    failed = sum(1 for a in assignments if a.get("error"))
    launched = sum(1 for a in assignments if not a.get("error"))
    return {"name": req.name, "ok": launched > 0,
            "launched_zones": launched, "failed_zones": failed,
            "excluded": excluded,
            "assignments": assignments}


@router.post("/mission/start")
async def mission_start():
    # H2: /mission/start on a DDS-bridge vehicle (Outrider) would arm() + MISSION_START
    # + DO_SET_MODE MISSION with whatever (likely empty) mission PX4 holds — and it
    # ARMS first. Refuse up front for a mission-incapable vehicle (the command is
    # never sent) instead of arming a drone into a mission it can't run.
    link = get_link()
    _require_missions(link)
    _require_ready(link)
    await missions.start(link)
    return {"ok": True}


@router.post("/mission/clear")
async def mission_clear():
    link = get_link()
    _require_missions(link)
    _require_ready(link)
    await missions.clear(link)
    return {"ok": True}


# ── missions (groups of concurrent per-vehicle flights) ──────────────────────
@router.get("/missions")
async def list_missions():
    """Missions, newest first. A MISSION is the set of per-vehicle flights whose
    active windows overlap in time — i.e. the drones that flew TOGETHER. Each
    entry carries its mission_id, time window, member vehicles + flight ids."""
    return flights.store.list_missions()


@router.get("/missions/{mission_id}")
async def get_mission(mission_id: str):
    """A mission with the FULL detail of every member flight, so the frontend can
    render the drones' reports side-by-side and replay all of them together."""
    mission = flights.store.get_mission(mission_id)
    if mission is None:
        raise HTTPException(404, f"unknown mission {mission_id}")
    return mission


# ── flights / mission reports ───────────────────────────────────────────────
@router.get("/flights")
async def list_flights():
    """Summaries of recorded flights, most recent first."""
    return flights.store.list_summaries()


@router.get("/flights/{flight_id}")
async def get_flight(flight_id: str):
    """Full detail for one flight: stats, path, mode timeline, events, actions."""
    flight = flights.store.get(flight_id)
    if flight is None:
        raise HTTPException(404, f"unknown flight {flight_id}")
    return flight


@router.get("/flights/{flight_id}/summary")
async def flight_summary(flight_id: str):
    """Operator-grade mission summary (model-generated, cached per flight).

    Generated once and cached on the flight record; subsequent requests return
    the cached text. Degrades gracefully to a deterministic stats-based summary
    on any model error so the report / PDF never breaks."""
    flight = flights.store.get(flight_id)
    if flight is None:
        raise HTTPException(404, f"unknown flight {flight_id}")
    cached = flight.get("summary")
    if isinstance(cached, str) and cached.strip():
        return {"summary": cached, "cached": True}
    text = await report.generate_summary(flight)
    flights.store.set_summary(flight_id, text)
    return {"summary": text, "cached": False}
