"""Voice agent core: STADO's tool surface, dispatcher, and safety gates.

Browser mic → the /ws/voice bridge (voice_qwen.py, Qwen Realtime duplex audio +
tool-calling) → the `dispatch()` entrypoint here → flight/vision commands, with
the spoken reply streamed back to the browser.

This module owns everything model-independent: the ~38 tool declarations
(plain JSON-Schema dicts), `dispatch()` with the Ready-for-Flight gate and
capability guards, the system prompt, and the alert/event queues the voice
session speaks from.

Audio: input PCM16 @16 kHz mono, output PCM16 @24 kHz mono.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import WebSocket

from .config import settings
from . import coordination as coord
from . import safety
from .autotune import manager as autotune_manager
from .api import AUTOTUNE_SAFETY
from .mavlink import commands, missions
from .mavlink.commands import haversine_offset
from .mavlink.link import get_link
from .mavlink.registry import registry
from .mavlink.jetson import jetson_track
from . import onboard_track
from . import completion
from . import recorder
from .survey import coordinated
from .survey.planner import clean_polygon, plan_survey
from .vision import grounding
from .vision.follow import geolocate_target
from .vision.pipeline import get_pipeline, init_pipeline
from .ws.hub import hub
from . import pois
from . import regions

log = logging.getLogger("gcs.voice")


# Voice tool names that operate across the whole fleet (no per-vehicle arg).
# Ready-for-Flight requires EVERY connected drone's gate to be ON before these
# run — matches api.py's fleet-takeoff policy.
_VOICE_FLEET_TOOLS = frozenset({
    "survey_area", "survey_area_with_fleet", "survey_region",
    "coordinated_orbit", "formation_flight",
    "pair_overwatch_scout", "search_area",
})

# Voice tool names that bypass the Ready-for-Flight gate on the single-vehicle
# code path. Recovery + stops + read-only helpers. `set_mode` bypasses only when
# the target mode is one of the recovery modes (LAND/RTL/HOLD — checked inline).
_VOICE_RECOVERY_TOOLS = frozenset({
    "disarm", "land", "return_to_launch", "hold",
    "stop_coordination", "cancel_survey", "cancel_autotune",
    # arm_check is a bench spin-up (arm → sleep → disarm); the drone never climbs
    # so keep it usable pre-flight for a preflight motor check.
    "arm_check",
    # Read-only / voice-side helpers that don't touch a vehicle.
    "get_status", "get_vehicle_id", "describe_view", "list_points",
    "find_survey_perimeters", "plan_survey_mission",
    "set_max_altitude", "clear_max_altitude",
    "record",
})


def clamp01(n: float) -> float:
    """Clamp a float into the normalized 0..1 range."""
    return max(0.0, min(1.0, float(n)))

# The Overwatch feed, transcoded to H.264 by go2rtc — what the tracker reads.
_OVERWATCH_RTSP = "rtsp://127.0.0.1:8554/drone"
_GO2RTC_FRAME = "http://127.0.0.1:1984/api/frame.jpeg?src=drone"
_frame_lock = asyncio.Lock()

# Proactive alerts the backend wants STADO to SPEAK (e.g. low-battery). The
# telemetry watchdog appends here; a live voice session drains it and injects
# each as a complete user turn so the model announces it. Cleared when read so
# an alert raised with no active session isn't spoken stale minutes later — the
# operator is still notified via the hub `low_battery` event regardless.
_pending_voice_alerts: list[str] = []

# Action-COMPLETION events (e.g. "Overwatch takeoff complete"). Separate queue so
# they're framed as [SYSTEM] status (relay tersely) vs [SYSTEM ALERT] (low battery
# → warn + ask RTL). The pump batches all pending events into ONE turn so two
# drones finishing together are reported together ("both drones …").
_pending_voice_events: list[str] = []


def queue_voice_alert(text: str) -> None:
    """Ask the live voice agent to speak `text` on its next tick (deduped)."""
    if text not in _pending_voice_alerts:
        _pending_voice_alerts.append(text)


def queue_voice_event(text: str) -> None:
    """Queue an action-completion line for STADO to report (deduped)."""
    if text not in _pending_voice_events:
        _pending_voice_events.append(text)


async def grab_live_frame() -> bytes | None:
    """Get a single live JPEG from the Overwatch camera, most-reliable path first:
    1) go2rtc's instant snapshot (works when the feed is healthy);
    2) the running vision pipeline's last decoded frame;
    3) a patient one-shot ffmpeg pull straight from the camera — the only path that
       survives a degraded air-link that yields just the occasional decodable
       keyframe (serialized so repeated calls don't fan out into camera sessions).
    Returns None only if nothing decodes at all."""
    # 1) go2rtc snapshot — instant when the transcode is keeping up.
    try:
        import httpx
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(_GO2RTC_FRAME)
        if r.status_code == 200 and len(r.content) > 2000:
            return r.content
    except Exception:  # noqa: BLE001
        pass
    # 2) the live pipeline's cached frame, if tracking is running.
    pipe = get_pipeline()
    if pipe is not None:
        j = pipe.get_jpeg()
        if j and len(j) > 2000:
            return j
    # 3) patient direct camera grab (tolerant flags; waits for a keyframe).
    src = settings.rtsp_url or settings.video_source
    if not src or not src.startswith("rtsp"):
        return None
    async with _frame_lock:
        out = "/tmp/stado_view.jpg"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
                "-fflags", "+genpts+discardcorrupt", "-i", src,
                "-frames:v", "1", "-q:v", "3", "-update", "1", "-y", out,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=16)
        except (asyncio.TimeoutError, FileNotFoundError, OSError):
            try:
                proc.kill()  # type: ignore[possibly-undefined]
            except Exception:  # noqa: BLE001
                pass
            return None
        try:
            with open(out, "rb") as f:
                data = f.read()
            return data if len(data) > 2000 else None
        except OSError:
            return None


def _jpeg_dims(data: bytes) -> tuple[int, int] | None:
    """(width, height) read straight from a JPEG's SOF marker — no image lib.
    Used to tag the seed box with the exact dimensions of the frame the VLM saw,
    so the Jetson can confirm it matches the tracker frame's aspect."""
    try:
        i, n = 2, len(data)  # skip SOI
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            # SOFn markers carry [precision, height(2), width(2)]; skip DHT/DAC/RST.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = (data[i + 5] << 8) | data[i + 6]
                w = (data[i + 7] << 8) | data[i + 8]
                return (w, h)
            i += 2 + ((data[i + 2] << 8) | data[i + 3])  # next marker segment
    except Exception:  # noqa: BLE001
        return None
    return None


def outrider_video_url() -> str:
    """The stream the GCS grabs to seed Outrider's tracker. Defaults to the
    Jetson's `/rgb` MJPEG — the SAME 640x480 frame the onboard CSRT tracker runs
    on — so a normalized seed box maps 1:1 onto the tracker image."""
    if settings.outrider_jetson_video_url:
        return settings.outrider_jetson_video_url
    if settings.outrider_jetson_host:
        return f"http://{settings.outrider_jetson_host}:{settings.outrider_jetson_video_port}/rgb"
    return ""


async def grab_outrider_frame() -> bytes | None:
    """One-shot JPEG from Outrider's onboard (Jetson) video, for seeding its
    onboard tracker with a VLM-grounded box. Uses the same patient ffmpeg path as the
    Overwatch grab. Returns None if the Jetson video URL isn't set/reachable.
    NOTE: full end-to-end needs the Jetson live; this is the GCS-side seam."""
    src = outrider_video_url()
    if not src:
        return None
    async with _frame_lock:
        out = "/tmp/outrider_view.jpg"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-fflags", "+genpts+discardcorrupt", "-i", src,
                "-frames:v", "1", "-q:v", "3", "-update", "1", "-y", out,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=12)
        except (asyncio.TimeoutError, FileNotFoundError, OSError):
            try:
                proc.kill()  # type: ignore[possibly-undefined]
            except Exception:  # noqa: BLE001
                pass
            return None
        try:
            with open(out, "rb") as f:
                data = f.read()
            return data if len(data) > 2000 else None
        except OSError:
            return None


def _ensure_pipeline():
    """Return a running vision pipeline, auto-starting it on the Overwatch feed if
    it isn't up yet — so voice 'track/follow' works without a manual START TRACKING.
    Wires the follow setpoint sink AND the map-objects geolocation loop the same way
    the REST /api/vision/start does, so a voice-acquired target is pinned to the map
    (the `map_objects` event is the ONLY one carrying a geolocated lat/lon).

    Runs synchronously inside the awaited `dispatch` coroutine (on the asyncio event
    loop), so start_map_objects_task's create_task is valid here."""
    pipe = get_pipeline()
    if pipe is not None and getattr(pipe, "_running", False):
        return pipe
    from .vision_api import _on_setpoint, start_map_objects_task
    pipe = init_pipeline(_OVERWATCH_RTSP, settings.yolo_model)
    pipe.on_setpoint = _on_setpoint
    pipe.start()
    start_map_objects_task()
    return pipe

_O, _N, _S, _B = ("object", "number", "string", "boolean")


def _schema(*, type: str, description: str | None = None, enum: list | None = None,
            items: dict | None = None, properties: dict | None = None,
            required: list | None = None) -> dict:
    """A JSON-Schema fragment for a tool parameter (OpenAI-style tool format,
    as consumed by Qwen Realtime's `session.update` and chat completions)."""
    out: dict = {"type": type}
    if description:
        out["description"] = description
    if enum:
        out["enum"] = list(enum)
    if items is not None:
        out["items"] = items
    if properties is not None:
        out["properties"] = properties
    if required is not None:
        out["required"] = required
    return out

# Candidate perimeters from the last find_survey_perimeters call (for execute_survey).
_last_perimeters: list = []

# The follow PROFILE last inferred for Outrider's onboard tracker, from the class
# in the operator's track_target description ("follow the CAR" → car). `follow`
# (which carries no description) reuses it so the controller flies the right
# per-class speed envelope. None = nothing inferred yet (controller default). See
# onboard_track.normalize_profile + reviews/outrider-follow-readiness.md.
_outrider_follow_profile: str | None = None

# The survey mission that's been PLANNED + previewed and is awaiting the
# operator's go-ahead. Set by plan_survey_mission (voice) or via the
# survey_pending hub event when the operator hits "Create mission" on the map.
# execute_survey flies whatever is pending. None = nothing staged.
#   {"label": str, "polygon": [(lat,lon)...], "vehicle": str|None, "altitude": f}
_pending_survey: dict | None = None

# A coordinated FLEET survey that's been PLANNED + previewed and is awaiting the
# operator's go-ahead. Set by survey_region (voice). execute_survey uploads +
# flies every staged per-drone mission; cancel_survey discards it. None = nothing
# staged. Shape:
#   {"label": str,
#    "zones": [
#       {"vehicle": str, "name": str, "polygon": [[lat,lon]...],
#        "path": [[lat,lon]...], "altitude": float}, ...]}
_pending_fleet_survey: dict | None = None

# M9: the pending-survey slots are module-global and written from BOTH the voice
# session and the API (map). Guard every mutation with this lock so a map-stage
# racing a voice-plan can't interleave a half-written slot, and stamp each staged
# plan with a monotonic id. The id lets a confirm correlate to a specific staged
# plan (e.g. the frontend can echo it on /survey/commit later) so 'confirm' flies
# the plan that was previewed, not whatever a second actor staged in between.
import threading  # noqa: E402

_pending_survey_lock = threading.Lock()
_pending_survey_seq = 0


def _next_survey_id() -> int:
    """Monotonic staged-plan id (caller holds _pending_survey_lock)."""
    global _pending_survey_seq
    _pending_survey_seq += 1
    return _pending_survey_seq


# Default survey line spacing (metres) when a planner/staging call doesn't carry
# one. Kept in ONE place so the preview, the staged dict, and the fly path agree.
_DEFAULT_LINE_SPACING_M = 25.0


def stage_survey_for_confirm(
    label: str,
    polygon: list,
    altitude: float = 30.0,
    vehicle: str | None = None,
    line_spacing_m: float = _DEFAULT_LINE_SPACING_M,
) -> dict:
    """Stage a planned survey mission for a confirm-then-fly. Called from the
    voice plan tool AND from the API when the operator presses 'Create mission'
    on the map, so a spoken 'confirm' flies whatever the map staged (and vice
    versa). The chosen `line_spacing_m` is carried in the staged dict so commit
    flies the PREVIEWED grid, not a hardcoded default. Returns {ok, waypoints} or
    {ok:False, error}."""
    global _pending_survey, _pending_fleet_survey
    poly = [(float(a), float(b)) for a, b in polygon]
    try:
        clean = clean_polygon(poly)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    grid = plan_survey(clean, altitude=altitude, line_spacing_m=line_spacing_m)
    if not grid:
        return {"ok": False, "error": "polygon too small for the survey line spacing"}
    wps = missions.survey_mission(grid, altitude)
    with _pending_survey_lock:
        sid = _next_survey_id()
        _pending_survey = {
            "id": sid,
            "label": label, "polygon": clean, "vehicle": vehicle, "altitude": altitude,
            "line_spacing_m": float(line_spacing_m),
        }
        # A new single-region stage supersedes any stale fleet stage.
        _pending_fleet_survey = None
    return {"ok": True, "id": sid, "label": label, "waypoints": len(wps)}


def clear_pending_survey() -> None:
    """Drop a staged-but-unconfirmed survey (operator cancelled on the map).
    Clears BOTH a single-region pending survey and a fleet/region pending survey."""
    global _pending_survey, _pending_fleet_survey
    with _pending_survey_lock:
        _pending_survey = None
        _pending_fleet_survey = None


def _fleet_zone_color_order(vehicles: list[str]) -> list[str]:
    """The vehicle ids in the same order the frontend palette is indexed, so the
    per-drone preview colors match the panel (Overwatch teal, Outrider amber)."""
    return vehicles


def plan_fleet_survey(
    label: str,
    center_lat: float,
    center_lon: float,
    width_m: float,
    height_m: float,
    heading_deg: float,
    vehicles: list[str],
    base_alt: float = 30.0,
    line_spacing_m: float = 25.0,
    gap_m: float = 5.0,  # HORIZONTAL zone corridor (kept small for coverage)
    sep_m: float = 15.0,  # VERTICAL altitude separation (operator-approved >= 15 m, preflight-02 F2)
) -> dict:
    """Split a region into one zone per drone and PLAN each zone's lawnmower grid
    WITHOUT uploading or flying. Returns the staged fleet shape (also caches it in
    `_pending_fleet_survey`) so execute_survey can fly it and the GCS can preview
    the divided zones + each drone's path in its fleet color. Returns
    {"ok": False, "error": ...} on a geometry failure."""
    global _pending_fleet_survey, _pending_survey
    n = len(vehicles)
    if n < 1:
        return {"ok": False, "error": "no drones to survey with"}
    try:
        zone_polys = coordinated.split_rect(
            center_lat, center_lon, width_m, height_m, heading_deg, n=n, gap_m=gap_m)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    # Deconflicted altitudes (Overwatch highest, >= sep_m apart) — same assignment
    # the fly path uses, so the preview altitudes match what will fly.
    alts = coordinated.assign_altitudes(vehicles, base_alt, sep_m)
    # SAFETY: refuse planning a survey whose DERIVED top altitude exceeds the
    # max-altitude ceiling (no clamp) — caught here so the preview never shows an
    # over-ceiling plan that would be refused at fly time.
    _top = max(alts.values()) if alts else base_alt
    if safety.exceeds(_top):
        return {"ok": False, **safety.refusal(_top)}
    zones: list[dict] = []
    for vid, poly in zip(vehicles, zone_polys):
        try:
            veh = registry.get(vid)
            name = veh.name
        except KeyError:
            name = vid
        alt = float(alts.get(vid, base_alt))
        grid = plan_survey(
            [(float(a), float(b)) for a, b in poly],
            altitude=alt, line_spacing_m=line_spacing_m)
        zones.append({
            "vehicle": vid,
            "name": name,
            "polygon": [[float(a), float(b)] for a, b in poly],
            "path": [[float(g.lat), float(g.lon)] for g in grid],
            "altitude": alt,
            "line_spacing_m": float(line_spacing_m),
        })
    with _pending_survey_lock:
        sid = _next_survey_id()
        _pending_fleet_survey = {"id": sid, "label": label, "zones": zones}
        # A new fleet stage supersedes any stale single-region stage.
        _pending_survey = None
    return {"ok": True, "id": sid, "label": label, "zones": zones}


def _fn(name, desc, props=None, req=None):
    params = (
        _schema(type=_O, properties=props, required=req or [])
        if props
        else {"type": "object", "properties": {}}
    )
    return {"name": name, "description": desc, "parameters": params}


# Optional target-drone selector, attached to every vehicle-specific command.
_VEHICLE_DESC = (
    "which drone to command: 'overwatch' (hexacopter) or 'outrider' (quadcopter); "
    "omit to use the currently active drone."
)
_VEHICLE_SCHEMA = _schema(type=_S, description=_VEHICLE_DESC)


def _veh(props=None):
    """Add the optional `vehicle` selector to a tool's props."""
    return {**(props or {}), "vehicle": _VEHICLE_SCHEMA}


# Shared `override` flag for altitude-bearing tools — bypasses the max-altitude
# ceiling, used ONLY after the operator explicitly confirms going above it.
_OVERRIDE_SCHEMA = _schema(type=_B, description=(
    "set true ONLY to bypass the max-altitude ceiling, after the operator has "
    "explicitly confirmed flying above it"))


def _offboard_refusal(vid: str | None, name: str, action: str) -> dict | None:
    """Capability guard (preflight H1): refuse a GCS-side OFFBOARD action for a
    vehicle whose command transport can't carry it (Outrider is a DDS-bridge
    vehicle — supports_offboard=False). Returns an operator-facing {ok:False,...}
    refusal that must be RETURNED (so the command is never sent), or None if the
    vehicle supports it. Refusing — instead of half-executing — stops the GCS from
    sending a DO_SET_MODE→OFFBOARD that would strand a flying Outrider with no
    setpoint stream (PX4 drops OFFBOARD in ~0.5 s and fails safe)."""
    if registry.supports_offboard(vid):
        return None
    return {
        "ok": False, "vehicle": name, "capability": "offboard",
        "error": f"{action} is not supported on {name}",
        "note": (f"{action} needs GCS-side OFFBOARD, which {name} does not support "
                 f"(its tracking/yaw is handled onboard). Command refused — nothing was sent."),
    }


def _missions_refusal(vid: str | None, name: str, action: str = "survey/mission") -> dict | None:
    """Capability guard (preflight H2): refuse a survey/mission for a vehicle whose
    transport can't run the MISSION_* upload protocol (Outrider — DDS bridge,
    supports_missions=False). Returns an operator-facing refusal to RETURN, or None
    if supported. Refusing up front avoids a silent upload timeout."""
    if registry.supports_missions(vid):
        return None
    return {
        "ok": False, "vehicle": name, "capability": "missions",
        "error": f"{action} is not supported on {name}",
        "note": (f"{name} cannot run survey/mission uploads over its DDS bridge. "
                 f"Command refused — fly survey on a mission-capable drone."),
    }


def _resolve_vehicle_id(spoken: str) -> str | None:
    """Map a spoken drone name (case-insensitive, with obvious variants) to a
    registry id, or None if it doesn't match a known vehicle."""
    s = (spoken or "").strip().lower()
    if not s:
        return None
    if s in registry._vehicles:  # already an exact id
        return s
    if "overwatch" in s or "over watch" in s or "hex" in s:
        return "overwatch"
    if "outrider" in s or "out rider" in s or "quad" in s:
        return "outrider"
    # Fall back to matching against the human-readable name of each vehicle.
    for v in registry.list():
        if v.name.lower() == s or v.id == s:
            return v.id
    return None


# Registry vehicle id → its go2rtc stream name. Overwatch's H.264 restream is
# "drone" (what the tracker reads); Outrider's is "outrider". The recorder
# captures rtsp://127.0.0.1:8554/<stream> to a local MP4. Keyed by the real
# fleet ids confirmed in config.fleet().
_VEHICLE_STREAM = {"overwatch": "drone", "outrider": "outrider"}


def _resolve_record_streams(spoken: str | None) -> tuple[list[tuple[str, str]], str | None]:
    """Resolve a spoken `vehicle` arg to the (vehicle_id, stream, name) targets to
    record. Returns ([(stream, vehicle_name), ...], error). 'all'/'both' → every
    mapped drone; empty → the active drone; otherwise the named drone."""
    s = (spoken or "").strip().lower()
    if s in ("all", "both", "everyone", "fleet"):
        targets = []
        for v in registry.list():
            stream = _VEHICLE_STREAM.get(v.id)
            if stream:
                targets.append((stream, v.name))
        if not targets:
            return [], "no recordable drones"
        return targets, None
    if s:
        vid = _resolve_vehicle_id(s)
        if vid is None:
            return [], f"unknown vehicle {spoken}"
    else:
        vid = registry.active_id()
    stream = _VEHICLE_STREAM.get(vid)
    if not stream:
        return [], f"no video stream for {vid}"
    return [(stream, registry.get(vid).name)], None


def tool_declarations() -> list[dict]:
    """The full STADO tool surface as OpenAI-style function declarations:
    [{"name", "description", "parameters": <JSON Schema>}, ...] — the format
    Qwen consumes in `session.update` / chat completions."""
    return [
        _fn("arm", "Arm the vehicle", _veh()),
        _fn("disarm", "Disarm the vehicle", _veh()),
        _fn("arm_check", (
            "Arm the motors for a brief test then automatically disarm a few seconds later — a "
            "safety/arming check (a.k.a. spin-up test). Does NOT take off — the vehicle never "
            "leaves the ground. Use for 'do an arm check' / 'arm check' / 'test the arming' / "
            "'spin up test'. Optional `vehicle` ('all'/'both' to test the whole fleet)."),
            _veh()),
        _fn("takeoff", "Arm and take off to an altitude",
            _veh({"altitude_m": _schema(type=_N, description="target altitude, meters"),
                  "override": _schema(type=_B, description=(
                      "set true ONLY to bypass the max-altitude ceiling after the operator "
                      "explicitly confirmed going above it"))}), ["altitude_m"]),
        _fn("land", "Land at the current position", _veh()),
        _fn("return_to_launch", "Return to launch / fly home and land", _veh()),
        _fn("hold", "Hold position immediately (pause / stop / brake)", _veh()),
        _fn("set_mode", "Set the flight mode",
            _veh({"mode": _schema(type=_S, description="POSITION, HOLD, MISSION, OFFBOARD, RTL, LAND")}), ["mode"]),
        _fn("track_target", (
            "Acquire and continuously track ANY object or person described in words — works for "
            "people just as well as vehicles."),
            _veh({"description": _schema(
                type=_S,
                description="e.g. 'the white pickup truck', 'the person in the red shirt', 'that man'")}),
            ["description"]),
        _fn("follow", "Start or stop the drone following the tracked target",
            _veh({"enable": _schema(type=_B)}), ["enable"]),
        _fn("orbit_target", (
            "Orbit / circle the currently tracked target. Optional radius_m sets the orbit "
            "radius in metres (e.g. 'orbit it at 40 metres' → radius_m=40); defaults to 25."),
            _veh({"radius_m": _schema(type=_N, description="orbit radius, metres (default 25)"),
                  "override": _OVERRIDE_SCHEMA})),
        _fn("survey_area", (
            "Survey a square area (side size_m, centered on the active drone), automatically "
            "DIVIDED among ALL connected drones — 1 connected → a single path over the WHOLE "
            "area; 2 connected → a zone each with a separation corridor (staggered altitudes, "
            "Overwatch higher). Every drone's path is shown on the map. This is the default for "
            "'survey this area' / 'survey a 200 m area' — it always scales to the connected fleet."),
            {"size_m": _schema(type=_N, description="side length, meters")}, ["size_m"]),
        _fn("survey_area_with_fleet", (
            "Coordinated multi-drone survey: divide a square area (centered on the ACTIVE drone) "
            "between ALL connected drones — each gets its own zone with a separation corridor, and "
            "the drones fly at staggered altitudes (Overwatch highest, >=5 m apart) as a backup. "
            "Use for 'survey this area with both drones' / 'split the area between the drones'."),
            {"size_m": _schema(type=_N, description="side length, meters")}, ["size_m"]),
        _fn("survey_region", (
            "Survey a NAMED search area the operator drew on the map (e.g. 'survey Sector 1' / "
            "'search Sector 1'). Looks the region up by name (its centre + dimensions + rotation) "
            "and runs the coordinated fleet survey over it — each drone gets a zone. Use this, NOT "
            "goto_point, when the operator names a search area."),
            {"name": _schema(type=_S, description="name of the search area")}, ["name"]),
        _fn("coordinated_orbit", (
            "Both connected drones orbit the SAME point at STAGGERED altitudes (Overwatch on the "
            "high band, >=5 m above Outrider) and slightly different radii so the circles never "
            "intersect. Default centre = the active drone's current position. Use for 'both orbit "
            "this point' / 'circle that spot with both drones'. To orbit a NAMED marker (e.g. 'both "
            "orbit B'), pass its name as `point` — its live coordinates are resolved server-side; do "
            "NOT guess lat/lon for a named point."),
            {"point": _schema(type=_S, description="name of a marked point to orbit (preferred over lat/lon)"),
             "lat": _schema(type=_N), "lon": _schema(type=_N),
             "radius_m": _schema(type=_N, description="orbit radius, meters (Outrider's; Overwatch +8 m)"),
             "altitude": _schema(type=_N, description="Overwatch high-band altitude, meters AGL"),
             "override": _OVERRIDE_SCHEMA}),
        _fn("formation_flight", (
            "Continuous formation: Outrider holds a fixed offset from Overwatch (default 12 m at "
            "bearing 180°, i.e. directly behind) and stays >=5 m BELOW it, repositioning ~1 Hz as "
            "Overwatch moves. enable=true starts it, enable=false stops it. Requires both drones "
            "armed and airborne. Use for 'fly in formation' / 'Outrider follow Overwatch'."),
            {"offset_m": _schema(type=_N, description="spacing from Overwatch, meters (default 12)"),
             "bearing_deg": _schema(type=_N, description="bearing relative to Overwatch heading (180=behind)"),
             "enable": _schema(type=_B)}, ["enable"]),
        _fn("pair_overwatch_scout", (
            "Pair the drones on a described target: Outrider acquires and FOLLOWS it from low while "
            "Overwatch ORBITS the target's position from the high band (>=5 m above Outrider). "
            "Needs a live camera feed — returns a clear error in SITL. Use for 'pair the drones on "
            "the truck' / 'scout that target with both drones'."),
            {"target_description": _schema(
                type=_S, description="e.g. 'the white pickup truck', 'the person in the red shirt'")},
            ["target_description"]),
        _fn("search_area", (
            "Coordinated SEARCH of a square area (side size_m, centered on the active drone): the "
            "fleet flies a divided lawnmower pattern (Overwatch higher, >=5 m apart) and the vision "
            "pipeline is started if available so anything detected along the path is flagged. Use "
            "for 'search this area' / 'sweep the area with both drones'."),
            {"size_m": _schema(type=_N, description="side length, meters")}, ["size_m"]),
        _fn("stop_coordination", (
            "Stop / abort ALL fleet coordination: cancel any running coordinated behavior "
            "(formation, pairing, etc.) and put every drone into HOLD. Use for 'stop coordination' "
            "/ 'abort the formation' / 'break off'.")),
        _fn("move", (
            "Move RELATIVE to the drone's current pose. forward_m / right_m are body-frame "
            "metres (forward is along the CURRENT heading; negative = back / left), up_m is "
            "metres to climb (negative = descend). The system applies the heading automatically — "
            "you NEVER need a compass bearing or the absolute position. Combine freely, e.g. "
            "'forward 50 and up 20' → forward_m=50, up_m=20."),
            _veh({"forward_m": _schema(type=_N), "right_m": _schema(type=_N),
                  "up_m": _schema(type=_N), "override": _OVERRIDE_SCHEMA})),
        _fn("turn", (
            "Yaw / rotate the drone in place by N degrees. direction is 'left' (counter-clockwise) "
            "or 'right' (clockwise). e.g. 'turn left 90' → degrees=90, direction='left'."),
            _veh({"degrees": _schema(type=_N), "direction": _schema(type=_S, description="'left' or 'right'")}),
            ["degrees", "direction"]),
        _fn("set_speed", (
            "Set the drone's cruise speed in metres/second for subsequent moves/missions "
            "(e.g. 'fly at 8 metres per second', 'set Outrider speed to 5'). Overrides the default."),
            _veh({"speed_ms": _schema(type=_N, description="target speed, m/s")}), ["speed_ms"]),
        _fn("get_status", "Get the live flight state (mode, armed, position, altitude AGL, heading, speed, battery, GPS). Also reports the active max-altitude ceiling.",
            _veh()),
        _fn("set_max_altitude", (
            "Set the fleet MAX-ALTITUDE CEILING in metres AGL — no drone may fly above it without "
            "an explicit operator override. Use for 'set the ceiling to 80 metres', 'max altitude "
            "120', 'don't go above 100 metres', 'altitude cap 60'."),
            {"altitude_m": _schema(type=_N, description="ceiling altitude, metres AGL")}, ["altitude_m"]),
        _fn("clear_max_altitude", (
            "Remove the fleet max-altitude ceiling entirely (unlimited altitude). Use for 'clear the "
            "ceiling', 'remove the altitude limit', 'no altitude cap'.")),
        _fn("get_vehicle_id", (
            "Get the ANONYMIZED registration info of the vehicle currently being tracked, once its "
            "plate has been read (plate, state, RTO, make/model, class/fuel/year, masked owner). Use "
            "for 'what's the plate?' / 'who owns the car?' / 'identify the vehicle'.")),
        _fn("describe_view", (
            "Look at a LIVE drone camera and describe what's visible. Use whenever the operator "
            "asks 'what do you see?' / 'describe the view' / 'tell me what you see'. Optional "
            "`question` to focus the look (e.g. 'how many vehicles?'). Optional `feed`: 'overwatch' "
            "(default, the main feed) or 'outrider' (a.k.a. the second feed / Feed 2 — Outrider's "
            "camera). Pick 'outrider' when the operator says 'feed 2' / 'the second feed' / 'Outrider's camera'."),
            {"question": _schema(type=_S, description="optional focus for the look"),
             "feed": _schema(type=_S, description="'overwatch' (default) or 'outrider'/'feed2'")}),
        _fn("find_survey_perimeters", (
            "Look at the satellite imagery and propose candidate survey areas. By default the "
            "detection is CENTERED on the drone's current position. To propose surveys AROUND a "
            "named marker instead (e.g. 'plan potential surveys around point A', 'find perimeters "
            "near the LZ'), pass that marker's name as `point` — its live coordinates are resolved "
            "server-side, so do NOT guess lat/lon. Returns a numbered list with "
            "labels/descriptions — read the options to the operator and ask which one to survey."),
            _veh({"point": _schema(
                type=_S,
                description="optional name of a marked point to center the detection on")})),
        _fn("plan_survey_mission", (
            "PLAN (but do NOT fly) a survey mission over one of the perimeters previously proposed "
            "by find_survey_perimeters, chosen by its 1-based number. This tidies the chosen "
            "polygon, computes the lawnmower flight path, and DRAWS the planned mission on the "
            "operator's map as a preview. It does NOT upload or fly anything yet — after calling "
            "this you MUST ask the operator to confirm before flying. Use for 'plan/create the "
            "mission for area 2', 'set up the survey'."),
            _veh({"choice": _schema(type=_N, description="1-based index from find_survey_perimeters")}),
            ["choice"]),
        _fn("execute_survey", (
            "Confirm and FLY the survey mission that was already PLANNED + previewed (by "
            "plan_survey_mission or by the operator pressing 'Create mission' on the map). Uploads "
            "and starts it. ONLY call this AFTER the operator explicitly confirms (e.g. 'yes', 'fly "
            "it', 'confirm', 'go'). `choice` is optional — if given, it plans+flies that 1-based "
            "perimeter directly; omit it to fly whatever is currently staged/previewed."),
            _veh({"choice": _schema(type=_N, description="optional 1-based index from find_survey_perimeters")})),
        _fn("cancel_survey", (
            "Cancel/discard the survey mission that was planned + previewed but not yet flown. Use "
            "when the operator says 'cancel', 'never mind', 'don't survey' after a plan preview.")),
        _fn("select_vehicle", (
            "Set the currently active drone. Use when the operator says e.g. 'switch to Outrider' "
            "or 'control Overwatch'. Subsequent unaddressed commands target this drone."),
            {"name": _schema(type=_S, description="'overwatch' or 'outrider'")},
            ["name"]),
        _fn("goto_point", (
            "Fly to a named marker the operator placed on the map (e.g. 'go to the LZ'). "
            "Resolves the marker by name. Optional altitude_m sets the cruise altitude "
            "(AGL); omit to keep the current altitude."),
            _veh({"name": _schema(type=_S, description="the marker's name"),
                  "altitude_m": _schema(type=_N, description="cruise altitude AGL, metres (optional)"),
                  "override": _OVERRIDE_SCHEMA}), ["name"]),
        _fn("orbit_point", (
            "Orbit a named marker the operator placed on the map (e.g. 'orbit Sector 1 at 40 metres'). "
            "Resolves the marker by name; radius_m optional (default 25)."),
            _veh({"name": _schema(type=_S, description="the marker's name"),
                  "radius_m": _schema(type=_N, description="orbit radius, metres"),
                  "altitude_m": _schema(type=_N, description="orbit altitude AGL, metres (optional)"),
                  "override": _OVERRIDE_SCHEMA}), ["name"]),
        _fn("list_points", "List the operator's currently marked points (names + positions)."),
        _fn("record", (
            "Start or stop recording a drone's video to a LOCAL MP4 file. action='start' begins "
            "recording, action='stop' ends it (and finalizes a playable file). `vehicle` selects "
            "the drone ('overwatch' or 'outrider'), or 'all'/'both' for the whole fleet; omit it "
            "to record the active drone. Use for 'record Overwatch', 'record both drones', 'stop "
            "recording Outrider', 'stop all recording'. Recording ONLY saves a local file — it does "
            "NOT change the live video feed or affect the flight."),
            {"action": _schema(type=_S, description="'start' or 'stop'"),
             "vehicle": _schema(type=_S, description=(
                 "'overwatch', 'outrider', or 'all'/'both' for the whole fleet; omit for the active drone"))},
            ["action"]),
        _fn("run_autotune", (
            "Run PX4 AUTOTUNE on a drone — an IN-FLIGHT maneuver that oscillates the "
            "drone on each axis to compute new rate-controller PID gains. Use for "
            "'run autotune', 'autotune Outrider', 'tune the drone'. SAFETY: this is an "
            "in-flight oscillation, so call WITHOUT `confirm` first to surface the "
            "preconditions and ASK the operator to confirm; only call WITH confirm=true "
            "AFTER they explicitly confirm. Optional `vehicle` ('overwatch'/'outrider'); "
            "omit for the active drone."),
            _veh({"confirm": _schema(type=_B, description=(
                "set true ONLY after the operator has explicitly confirmed running the "
                "in-flight autotune maneuver"))})),
        _fn("cancel_autotune", (
            "Cancel/stop a running autotune (sends the disable command to PX4). Use for "
            "'cancel autotune', 'stop the tune', 'abort autotune'. Optional `vehicle`; "
            "omit for the active drone."),
            _veh()),
    ]


SYSTEM_PROMPT = (
    "You are STADO, the voice copilot of a drone ground-control station. "
    "Always speak in English (en-US), regardless of the language the operator uses. "
    "You command a fleet: Overwatch (a medium hexacopter) and Outrider (a small quadcopter). "
    "The operator can address either drone by name — e.g. 'Overwatch, take off to 50', "
    "'Outrider, orbit the building' — in which case pass that drone as the `vehicle` argument on "
    "the tool call ('overwatch' or 'outrider'). If the operator does NOT name a drone, command the "
    "currently active drone (omit `vehicle`). Use `select_vehicle` to change which drone is active. "
    "You may issue commands to multiple drones in a single turn — emit one tool call per drone with "
    "the right `vehicle` arg. Execute commands directly without asking for confirmation.\n"
    "ACTION COMPLETION — when you issue a command that takes time (takeoff, land, return to launch, "
    "goto a point, orbit a point), acknowledge BRIEFLY that you're executing it (e.g. 'Overwatch "
    "lifting off') but do NOT claim it's complete yet. The system watches the drone and sends you a "
    "[SYSTEM] message when the action ACTUALLY finishes — when you receive a [SYSTEM] line, report "
    "that completion tersely to the operator (e.g. 'Overwatch takeoff complete'). If a [SYSTEM] line "
    "contains completions for more than one drone, report them together in one line. Never announce a "
    "completion before its [SYSTEM] event arrives.\n"
    "FLEET SAFETY — if the operator says 'land' / 'return' / 'come home' WITHOUT naming a drone and "
    "more than one drone is airborne, command ALL airborne drones (emit one land/return_to_launch call "
    "per drone) — never leave one up. Only target a single drone if the operator named it.\n"
    "LOW-BATTERY RTL (the ONE exception to the no-confirmation rule): if a [SYSTEM ALERT] tells you a "
    "drone's battery is low, immediately and tersely warn the operator with the drone name and "
    "percentage, then ASK whether to return to launch. Do NOT call return_to_launch until the "
    "operator confirms (yes / do it / affirmative / RTL). If they decline, hold off. On confirmation, "
    "call return_to_launch with that drone's `vehicle` arg.\n"
    "FLEET DECONFLICTION (safety): the two drones launch within ~10 m of each other, so they must "
    "NEVER share the same altitude while both are airborne. **OVERWATCH ALWAYS FLIES HIGHER THAN "
    "OUTRIDER** — Overwatch holds the high band, Outrider stays below it, with at least 15 m of "
    "vertical separation. If the operator gives two altitudes (e.g. '30 and 35'), assign the HIGHER "
    "value to Overwatch and the LOWER to Outrider regardless of the order they said them. If a "
    "command would put both at (or near) the same altitude (e.g. 'take off both to 30'), stagger "
    "them — Overwatch above, Outrider below (e.g. Overwatch 45 m, Outrider 25 m) — and say so. Apply "
    "the same gap for move/goto/orbit when both are near each other. If only one drone is flying, no "
    "staggering is needed.\n"
    "MAX-ALTITUDE CEILING (safety): there may be a max-altitude CEILING the operator set (it is shown "
    "in get_status as max_altitude_m, in metres AGL; null means no ceiling). When a ceiling is set, "
    "NEVER command or plan ANY altitude above it — takeoff, goto, orbit, move/climb, survey, or a "
    "staggered fleet stack (remember Overwatch flies 15 m above Outrider, so the TOP altitude must "
    "stay under the ceiling). To SET or change the ceiling call set_max_altitude (e.g. 'set the "
    "ceiling to 80'); to remove it call clear_max_altitude. If the operator EXPLICITLY asks to go "
    "ABOVE the ceiling, do NOT execute the command — first STATE the conflict (the requested altitude "
    "vs the ceiling) and ASK for explicit confirmation. ONLY after a clear yes / 'override' / 'do it "
    "anyway' do you RE-ISSUE the SAME command with override=true, and ANNOUNCE that you are applying an "
    "altitude override. Never set override=true on your own initiative — only after that explicit "
    "confirmation. A command refused for the ceiling comes back with override_required=true and the "
    "ceiling figure; relay the conflict and ask before retrying.\n"
    "AUTOTUNE — CONFIRM FIRST (an in-flight oscillation maneuver, so it follows the same "
    "ask-then-confirm flow as an altitude override). For 'run autotune' / 'tune the drone' / "
    "'autotune Outrider': call run_autotune WITHOUT confirm first. It comes back with "
    "confirm_required=true and the safety preconditions (the drone must be ARMED and HOVERING in "
    "a position-hold mode, in open airspace with room to wobble, operator ready to take manual "
    "control; it runs ~40 s and the new gains apply automatically on landing/disarm). STATE those "
    "preconditions and ASK the operator to confirm. ONLY after a clear yes / 'confirm' / 'do it' do "
    "you RE-CALL run_autotune with confirm=true (and the same vehicle). Never set confirm=true on "
    "your own initiative. Once started, acknowledge tersely (e.g. 'Outrider tuning') but do NOT "
    "claim it's done — a [SYSTEM] line reports the real completion/failure; relay that when it "
    "arrives. To stop a running tune ('cancel autotune', 'stop the tune', 'abort') call "
    "cancel_autotune.\n"
    "When you need the "
    "current flight state (mode, armed, position, heading, altitude AGL, speed, battery), call "
    "get_status — never claim you don't know where the drone is or which way it faces; query it.\n"
    "CONNECTIVITY HONESTY (critical): NEVER say a drone is connected, online, ready, communicating, "
    "armed, or disarmed unless get_status/telemetry shows connected:true for THAT drone. If a vehicle "
    "has NO LINK (connected:false), it is OFFLINE — say plainly it is offline / not connected and you "
    "have no telemetry for it. Report each drone's link state individually; never assume readiness.\n"
    "GREETING HONESTY (critical): your very first turn / opening greeting MUST reflect the actual link "
    "status of each drone given to you in the 'CURRENT FLEET STATUS' line of your instructions. Do NOT "
    "invent or assume connectivity in the greeting. If a drone is OFFLINE there, your greeting must say "
    "it is offline / no link (e.g. 'STADO online. Overwatch: no link. Outrider: no link. What's the "
    "command?'). NEVER greet with 'both connected', 'both online', 'connected', or 'ready' for any drone "
    "that is OFFLINE. Only state a drone is connected if the fleet status shows it CONNECTED.\n"
    "ARMING vs TAKEOFF (do not confuse these): ARM just energizes/spins up the motors and readies the "
    "drone on the ground — it does NOT climb. TAKEOFF arms AND climbs to an altitude. So 'arm' → call "
    "arm (stays armed on the ground); 'take off' → call takeoff. An ARM CHECK is a brief motor/arming "
    "test: 'do an arm check' / 'arm check' / 'test the arming' / 'spin up test' → call arm_check — it "
    "arms the motor(s) for a few seconds then automatically disarms, and NEVER takes off. Do not climb "
    "during an arm check. For 'arm check both' / 'test arming on all drones', pass vehicle='all'.\n"
    "Translate the operator's spoken commands into tool calls and execute them, in order for "
    "multi-step requests. For relative movement ('move forward 20 metres', 'go up 20', 'back up "
    "10 and climb 5') call `move` with body-frame metres — forward is along the current heading, "
    "which the system handles, so DO NOT ask for a bearing or direction. Altitudes are metres "
    "above the launch point (AGL). Only ask a clarifying question if a command is genuinely "
    "ambiguous or unsafe; otherwise act.\n"
    "BREVITY (important): respond like a military radio operator — terse, a few words, no narration. "
    "Confirm an action in the fewest words possible (e.g. 'Overwatch climbing to 50.', 'Holding.', "
    "'Outrider orbiting.'). For status, give ONLY the key figures, clipped (e.g. 'Both nominal, 5 "
    "metres per second.' or just '37 metres, heading 80.') — never a sentence describing what each "
    "drone is doing. Do NOT restate the whole situation or list every value. Give a full/detailed "
    "readout ONLY when the operator explicitly asks ('full status', 'give me details').\n"
    "To track, follow or orbit something: call track_target with a plain-language description — it "
    "works for ANY object including people, e.g. 'track that man' / 'follow the person in the red "
    "shirt' as readily as 'track the white pickup truck'. Then `follow` or `orbit_target` act on "
    "whatever was acquired. While a vehicle is tracked its plate is read automatically; the operator "
    "can ask 'what's the plate?' / 'who owns the car?' → call get_vehicle_id (anonymized info; owner "
    "is masked).\n"
    "You CAN see through the drone camera: when the operator asks 'what do you see?' / 'describe the "
    "view' / 'tell me what you see', call describe_view and report what comes back. Never say you "
    "lack a camera feed — call the tool.\n"
    "SURVEY — PLAN, THEN CONFIRM (a survey is a big autonomous flight, so it is the second "
    "exception to the no-confirmation rule). For 'survey this area' / 'find a perimeter to survey': "
    "call find_survey_perimeters. The candidate areas are drawn on the operator's map, each with a "
    "colored outline and label. Briefly tell them how many you found and that they're shown on the "
    "map — they can TAP one to pick it (and drag its vertices to refine it), or tell you the "
    "number. To propose surveys AROUND a named marker (e.g. 'plan potential surveys around point "
    "A', 'find perimeters near the LZ'), call find_survey_perimeters with `point` set to that "
    "marker's name — the detection centers there instead of on the drone. When they pick one (by "
    "voice number, or say 'plan/create the mission for area N'), call plan_survey_mission with that "
    "number — this PLANS and PREVIEWS the lawnmower flight path on the map but does NOT fly. After "
    "it returns, tell them the mission is planned and ASK them to confirm before flying (e.g. "
    "'Survey planned, N waypoints — confirm to fly?'). Do NOT call execute_survey until the "
    "operator confirms (yes / fly it / confirm / go). On confirmation, call execute_survey (no "
    "arguments — it flies what was just previewed). If they decline or say cancel / never mind, "
    "call cancel_survey and hold off. If the operator presses 'Create mission' or 'Confirm & fly' "
    "on the map themselves, the GCS handles the plan/fly, so just acknowledge.\n"
    "SURVEY A NAMED SEARCH AREA — PLAN, THEN CONFIRM. For 'survey Sector 1' / 'search Sector 1' "
    "(a NAMED operator-drawn region), call survey_region with that name. This DIVIDES the region "
    "into one zone per drone, plans each drone's lawnmower grid, and PREVIEWS the divided zones + "
    "each drone's path on the map in its fleet color — it does NOT fly. After it returns, briefly "
    "name each drone + its zone/altitude and ASK the operator to confirm before flying (e.g. "
    "'Sector 1 split between Overwatch and Outrider — confirm to fly?'). Do NOT fly until they "
    "confirm; on confirmation call execute_survey (no arguments — it uploads + flies the staged "
    "per-drone missions). On cancel / never mind, call cancel_survey.\n"
    "For 'survey this area with both drones' / 'split the area between the drones' (NO named "
    "region — a square centered on the drone): call survey_area_with_fleet with the area size in "
    "metres. The fleet DIVIDES the square area into one zone per connected drone (a separation "
    "corridor keeps the zones apart so the drones stay >=5 m apart horizontally) and flies them at "
    "staggered altitudes (Overwatch higher) as a vertical backup. Confirm by naming each drone, "
    "its zone and altitude.\n"
    "MULTI-DRONE COORDINATION — coordinated fleet behaviors, all of which keep OVERWATCH HIGHER "
    "with at least 5 m of vertical separation:\n"
    "• 'both orbit this point' / 'circle that spot with both drones' → call coordinated_orbit "
    "(optional lat/lon — default is the active drone's position — plus radius_m and an Overwatch "
    "high-band altitude). Both drones circle the SAME point at staggered altitudes (Overwatch on "
    "top, >=5 m above Outrider) and offset radii so they never collide. Confirm the centre and "
    "each drone's altitude.\n"
    "• 'fly in formation' / 'Outrider follow Overwatch' → call formation_flight with enable=true "
    "(default 12 m behind, bearing 180°); Outrider continuously holds that offset from Overwatch "
    "and stays >=5 m below it. 'break formation' / 'stop following' → enable=false. Both drones "
    "must be armed and airborne first.\n"
    "• 'pair the drones on the <target>' / 'scout that with both drones' → call "
    "pair_overwatch_scout with the target description: Outrider follows it low, Overwatch orbits it "
    "high. This needs a live camera feed; if there's none (SITL) say a camera feed is required.\n"
    "• 'search this area' / 'sweep the area with both drones' → call search_area with the side in "
    "metres: the fleet flies a divided lawnmower SEARCH pattern (Overwatch higher) and vision "
    "flags anything detected along the path. Say it's a search pattern.\n"
    "• 'stop coordination' / 'abort the formation' / 'break off' → call stop_coordination: it "
    "cancels every running coordinated behavior and puts all drones into HOLD.\n"
    "For ALL of these, reaffirm the safety rule when you confirm: Overwatch flies higher, at least "
    "5 m of vertical separation.\n"
    "RECORDING — the operator can record a drone's video to a LOCAL MP4 file via the `record` tool. "
    "'record Overwatch' / 'start recording Outrider' → record with action='start' and that drone as "
    "`vehicle`. 'record this' / 'start recording' with no drone named → action='start', omit `vehicle` "
    "(records the active drone). 'record both drones' / 'record everything' / 'record the fleet' → "
    "action='start', vehicle='both'. 'stop recording Outrider' → action='stop' with that `vehicle`; "
    "'stop recording' (no drone) → action='stop', omit `vehicle`; 'stop all recording' / 'stop "
    "recording everything' → action='stop', vehicle='both'. For 'are we recording?' / 'what's "
    "recording?' just report from the last record tool result (or start/stop as asked). Recording ONLY "
    "saves a local MP4 — it does NOT change or interrupt the live video feed and does NOT affect the "
    "flight; never imply otherwise. Confirm tersely using the tool's note (e.g. 'Recording Overwatch.', "
    "'Stopped Outrider recording.')."
)


async def dispatch(name: str, args: dict) -> dict:
    """Execute a tool call against the vehicle / vision pipeline."""
    # The survey plan/confirm tools read+write the module-level staged missions.
    global _pending_survey, _pending_fleet_survey
    # select_vehicle changes the active drone and never targets a link itself.
    if name == "select_vehicle":
        vid = _resolve_vehicle_id(str(args.get("name", "")))
        if vid is None:
            return {"ok": False, "error": f"unknown vehicle {args.get('name')}"}
        v = registry.set_active(vid)
        return {"ok": True, "active": v.id, "name": v.name}

    # Ready-for-Flight fleet gate. Applied to voice tools that operate on multiple
    # vehicles at once (survey_area, survey_region, coordinated_orbit, formation_flight,
    # pair_overwatch_scout, search_area). Refuses if any CONNECTED vehicle's gate is
    # OFF — matches the HTTP fleet-takeoff policy.
    if name in _VOICE_FLEET_TOOLS:
        for _v in registry.list():
            if not _v.link.snapshot().get("connected"):
                continue
            if not safety.is_ready(_v.id):
                return safety.refusal_not_ready(_v.id)

    # Fleet-wide coordinated survey: divide a square (centered on the active
    # drone) among ALL connected drones. Operates over the whole fleet, so it
    # has no per-vehicle link to resolve.
    if name in ("survey_area", "survey_area_with_fleet"):
        s = get_link().snapshot()
        if s.get("lat") is None:
            return {"ok": False, "error": "no GPS fix"}
        side = float(args.get("size_m", 100))
        # Divide the square among CONNECTED drones ONLY: 1 connected → 1 zone (a
        # single lawnmower path over the WHOLE area); 2 → a zone each. Never more
        # zones than connected drones, so the mission always matches the live fleet.
        # H2: EXCLUDE mission-incapable drones (Outrider — DDS bridge can't run the
        # MISSION_* upload) from the zone split, so a 2-drone fleet survey with
        # Outrider present surveys with the mission-capable drone(s) only rather
        # than assigning Outrider a zone whose upload would silently time out.
        connected_ids = [v.id for v in registry.list() if v.link.snapshot().get("connected")]
        excluded = [vid for vid in connected_ids if not registry.supports_missions(vid)]
        vehicles = [vid for vid in connected_ids if registry.supports_missions(vid)]
        if not vehicles:
            if excluded:
                names = ", ".join(registry.get(e).name for e in excluded)
                return {"ok": False, "error": f"no mission-capable drones to survey with",
                        "excluded": excluded,
                        "note": f"{names} cannot run survey/mission uploads — no other drone is connected."}
            return {"ok": False, "error": "no connected drones"}
        # SAFETY: refuse a fleet survey whose DERIVED top altitude (Overwatch's
        # high band = base + 15 m sep) exceeds the max-altitude ceiling. No clamp;
        # the operator can lower the ceiling or override via a direct command.
        _survey_alts = coordinated.assign_altitudes(vehicles, 30.0, 15.0)
        _survey_top = max(_survey_alts.values()) if _survey_alts else 30.0
        if safety.exceeds(_survey_top):
            return safety.refusal(_survey_top)
        # gap_m=5.0 is the HORIZONTAL zone corridor (kept small so the lawnmower
        # lanes don't waste coverage); sep_m=15.0 is the VERTICAL altitude floor
        # (preflight-02 F2: operator signed off on >= 15 m). These are decoupled.
        zones = coordinated.split_rect(
            s["lat"], s["lon"], side, side, 0.0, n=len(vehicles), gap_m=5.0
        )
        assignments = await coordinated.plan_and_fly(
            vehicles, zones, base_alt=30.0, line_spacing_m=25.0, sep_m=15.0
        )
        # Push each drone's zone + planned lawnmower path to the GCS map so the
        # operator SEES one path per connected drone (flying:true = a live mission).
        hub.publish_threadsafe({
            "type": "fleet_zones",
            "label": f"Survey {int(side)} m",
            "flying": True,
            "zones": [
                {"vehicle": a["vehicle"], "name": a.get("name"),
                 "polygon": a.get("polygon"), "path": a.get("path", [])}
                for a in assignments if not a.get("error")
            ],
        })
        # Top-level ok = at least one zone launched; failed_zones surfaces partial
        # failure so STADO doesn't report a blanket success when zones were rejected.
        failed = sum(1 for a in assignments if a.get("error"))
        launched = sum(1 for a in assignments if not a.get("error"))
        excluded_note = None
        if excluded:
            names = ", ".join(registry.get(e).name for e in excluded)
            excluded_note = f"{names} excluded (cannot run survey/mission)."
        return {"ok": launched > 0, "vehicles_used": len(vehicles), "preview_on_map": True,
                "launched_zones": launched, "failed_zones": failed,
                "excluded": excluded, "excluded_note": excluded_note,
                "assignments": [
                    {"vehicle": a["vehicle"], "name": a.get("name"),
                     "altitude": a.get("altitude"), "polygon": a.get("polygon"),
                     "error": a.get("error")}
                    for a in assignments]}

    # Survey a NAMED search area (operator-drawn region) with the fleet — PLAN +
    # PREVIEW only, never fly here. Divide the region into per-drone zones, plan
    # each zone's lawnmower grid WITHOUT uploading, STAGE the fleet plan, and push
    # the zones to the GCS so the divided zones + each drone's path render in its
    # fleet color (flying:false). STADO then asks the operator to confirm; the
    # existing execute_survey flies the staged fleet survey.
    if name == "survey_region":
        r = regions.find(str(args.get("name", "")))
        if not r:
            return {"ok": False, "error": f"no search area named '{args.get('name')}'"}
        # Divide the region into ONE zone per CONNECTED drone: 1 connected → 1 zone
        # → a single lawnmower path over the WHOLE region; 2 connected → 2 zones.
        # If NONE are connected, preview a single full-area zone for one drone (the
        # active one, else the first registered) rather than splitting among every
        # registered drone — never split into more zones than connected drones.
        # H2: only mission-capable drones (Overwatch) get a survey zone; a DDS-bridge
        # drone (Outrider) is EXCLUDED from the split — it can't run a MISSION_*
        # upload. So a 2-drone region survey with Outrider present is flown by the
        # mission-capable drone(s) only, not silently failed on Outrider's zone.
        connected = [v.id for v in registry.list()
                     if v.link.snapshot().get("connected") and registry.supports_missions(v.id)]
        region_excluded = [v.id for v in registry.list()
                           if v.link.snapshot().get("connected") and not registry.supports_missions(v.id)]
        if connected:
            vehicles = connected
        else:
            # No connected mission-capable drone: preview a single full-area zone
            # for the active mission-capable drone (else the first mission-capable
            # one registered) — never preview a survey on a mission-incapable drone.
            active = registry.active_id()
            preview = active if registry.supports_missions(active) else None
            if preview is None:
                preview = next((v.id for v in registry.list()
                                if registry.supports_missions(v.id)), None)
            vehicles = [preview] if preview else []
        if not vehicles:
            if region_excluded:
                names = ", ".join(registry.get(e).name for e in region_excluded)
                return {"ok": False, "error": "no mission-capable drones to survey with",
                        "excluded": region_excluded,
                        "note": f"{names} cannot run survey/mission uploads."}
            return {"ok": False, "error": "no drones registered"}
        # gap_m=5.0 keeps the HORIZONTAL zone corridor tight; sep_m=15.0 is the
        # VERTICAL altitude floor (preflight-02 F2, operator-approved >= 15 m).
        planned = plan_fleet_survey(
            r["name"], r["center"][0], r["center"][1], r["width_m"], r["height_m"],
            r.get("heading_deg", 0.0), vehicles, base_alt=30.0,
            line_spacing_m=25.0, gap_m=5.0, sep_m=15.0)
        if not planned.get("ok"):
            return planned
        zones = planned["zones"]
        # plan_fleet_survey already dropped any single-region pending survey under
        # the lock, so a confirm is unambiguous.
        # Preview the divided zones + per-drone paths in their fleet colors. The
        # frontend renders this via the existing fleetZones path (flying:false).
        hub.publish_threadsafe({
            "type": "fleet_zones",
            "label": r["name"],
            "flying": False,
            "zones": zones,
        })
        excl_note = ""
        if region_excluded:
            names = ", ".join(registry.get(e).name for e in region_excluded)
            excl_note = f" {names} excluded (cannot run survey/mission)."
        return {"ok": True, "region": r["name"], "planned": True,
                "preview_on_map": True,
                "excluded": region_excluded,
                "assignments": [
                    {"vehicle": z["vehicle"], "name": z["name"],
                     "altitude": z["altitude"]} for z in zones],
                "note": ("Fleet survey planned and previewed — each drone's zone + path "
                         "is shown on the map. Ask the operator to confirm before flying." + excl_note)}

    # ── Fleet COORDINATION behaviors (operate over the whole fleet, no
    #    per-vehicle link to resolve). All enforce Overwatch-higher / >=5 m sep.
    if name == "coordinated_orbit":
        # Prefer a named marker — resolve its LIVE coordinates from the POI store
        # (the spoken context can be stale, but the store is synced from the UI), so
        # 'both orbit B' centres on B exactly instead of a guessed lat/lon.
        lat, lon = args.get("lat"), args.get("lon")
        pname = str(args.get("point", "")).strip()
        if pname:
            p = pois.find(pname)
            if not p:
                return {"ok": False, "error": f"no marker named '{pname}'"}
            lat, lon = p["lat"], p["lng"]
        return await coord.coordinated_orbit(
            lat, lon, float(args.get("radius_m", 25)), args.get("altitude"),
            override=bool(args.get("override", False)))
    if name == "formation_flight":
        return await coord.formation_flight(
            float(args.get("offset_m", 12)),
            float(args.get("bearing_deg", 180)),
            bool(args.get("enable", True)))
    if name == "pair_overwatch_scout":
        return await coord.pair_overwatch_scout(str(args.get("target_description", "")))
    if name == "search_area":
        return await coord.search_area(float(args.get("size_m", 100)))
    if name == "stop_coordination":
        return await coord.stop_coordination()

    # ── Fleet MAX-ALTITUDE CEILING (process-global; no per-vehicle link) ───────
    if name == "set_max_altitude":
        try:
            alt = safety.set_max_altitude(float(args.get("altitude_m")))
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "max_altitude_m": alt,
                "note": f"Max-altitude ceiling set to {round(alt)} m AGL."}
    if name == "clear_max_altitude":
        safety.clear_max_altitude()
        return {"ok": True, "max_altitude_m": None,
                "note": "Max-altitude ceiling cleared — no altitude limit."}

    # ARM CHECK — a brief arming/motor test: ARM, wait, then auto-DISARM. This is
    # NOT a takeoff (the vehicle never climbs). Resolves its target(s) the same way
    # the arm path does — an explicit `vehicle`, or 'all'/'both' for the whole fleet
    # (multiple links), or the active drone. Uses the truthful commands.arm: if a
    # target's arming is denied (preflight/precondition), report the reason and STOP
    # — never sleep/disarm a vehicle that didn't actually arm.
    if name == "arm_check":
        spoken_ac = str(args.get("vehicle", "")).strip().lower()
        if spoken_ac in ("all", "both"):
            targets = [v for v in registry.list() if v.link.snapshot().get("connected")]
            if not targets:
                return {"ok": False, "error": "no connected drones"}
        elif spoken_ac:
            ac_vid = _resolve_vehicle_id(spoken_ac)
            if ac_vid is None:
                return {"ok": False, "error": f"unknown vehicle {args.get('vehicle')}"}
            veh = registry.get(ac_vid)
            # Connectivity-check the named target too (not just 'all'/'both') so an
            # offline named drone returns a clean "offline" instead of a multi-second
            # arm timeout + a confusing "arm denied".
            if not veh.link.snapshot().get("connected"):
                return {"ok": False, "vehicle": veh.name,
                        "error": f"{veh.name} is offline — no link"}
            targets = [veh]
        else:
            veh = registry.active_vehicle()
            if not veh.link.snapshot().get("connected"):
                return {"ok": False, "vehicle": veh.name,
                        "error": f"{veh.name} is offline — no link"}
            targets = [veh]
        # ARM every target first; bail with the denial reason if any arm fails so we
        # don't leave a vehicle armed (or sleep+disarm one that never armed).
        armed_ok: list = []
        for veh in targets:
            res = await commands.arm(veh.link)
            if not res.get("ok"):
                # Disarm any we already armed in this check, then report the denial.
                for done in armed_ok:
                    await commands.disarm(done.link)
                return {"ok": False, "vehicle": veh.name,
                        "result": res.get("result_name") or res.get("result"),
                        "reason": res.get("reason"),
                        "note": f"Arm check aborted — {veh.name} arm denied: {res.get('reason')}"}
            armed_ok.append(veh)
        # All armed — hold the spin-up briefly, then auto-disarm the same targets.
        await asyncio.sleep(4)
        for veh in armed_ok:
            await commands.disarm(veh.link)
        names = ", ".join(v.name for v in armed_ok)
        return {"ok": True, "vehicles": [v.name for v in armed_ok],
                "note": f"Arm check complete — {names} armed then disarmed"}

    # RECORD — start/stop local MP4 capture of a drone's go2rtc restream. Resolves
    # its own go2rtc stream(s) from the vehicle arg ('all'/'both' → both streams,
    # omitted → the active drone) and drives recorder.start/stop per stream. This
    # only writes a local file; it does NOT touch the live feed or the flight.
    if name == "record":
        action = str(args.get("action", "")).strip().lower()
        targets, err = _resolve_record_streams(args.get("vehicle"))
        if err:
            return {"ok": False, "error": err}
        if action == "start":
            started, already = [], []
            for stream, vname in targets:
                res = recorder.start(stream)
                if not res.get("ok"):
                    return {"ok": False, "error": f"{vname}: {res.get('error', 'could not start recording')}"}
                (already if res.get("already") else started).append(vname)
            parts = []
            if started:
                parts.append("Recording " + " and ".join(started))
            if already:
                parts.append("already recording " + " and ".join(already))
            return {"ok": True, "started": started, "already_recording": already,
                    "note": "; ".join(parts) if parts else "Nothing to record"}
        if action == "stop":
            stopped, not_recording = [], []
            for stream, vname in targets:
                was = recorder.is_recording(stream)
                res = recorder.stop(stream)
                if was:
                    fname = Path(res["file"]).name if res.get("file") else None
                    stopped.append((vname, fname))
                else:
                    not_recording.append(vname)
            parts = []
            for vname, fname in stopped:
                parts.append(f"Stopped {vname} recording — saved {fname}" if fname
                             else f"Stopped {vname} recording")
            if not_recording:
                parts.append(("not recording: " if stopped else "Not recording ")
                             + " and ".join(not_recording))
            return {"ok": True,
                    "stopped": [{"vehicle": v, "file": f} for v, f in stopped],
                    "not_recording": not_recording,
                    "note": "; ".join(parts) if parts else "Nothing was recording"}
        return {"ok": False, "error": f"unknown record action '{action}' — use 'start' or 'stop'"}

    # AUTOTUNE — an IN-FLIGHT axis-oscillation maneuver, so it is gated behind the
    # SAME explicit override-confirm flow as a too-high takeoff: the FIRST call
    # (confirm absent/false) does NOT fire — it returns the safety preconditions and
    # confirm_required so STADO states them and asks; only confirm=true (after the
    # operator's explicit yes) actually starts. Resolves its own target (named or
    # active) and checks connectivity, like arm_check — never a silent wrong drone.
    if name == "run_autotune":
        spoken_at = str(args.get("vehicle", "")).strip()
        if spoken_at:
            at_vid = _resolve_vehicle_id(spoken_at)
            if at_vid is None:
                return {"ok": False, "error": f"unknown vehicle {args.get('vehicle')}"}
            veh = registry.get(at_vid)
        else:
            try:
                veh = registry.active_vehicle()
            except RuntimeError:
                return {"ok": False, "error": "no active vehicle"}
        if not registry.supports_autotune(veh.id):
            # Outrider (DDS bridge): cmd 212 → PX4 Commander UNSUPPORTED → false-start
            # that leaves the drone armed/hovering. Refuse cleanly; NEVER send.
            return {"ok": False, "vehicle": veh.name,
                    "error": (f"{veh.name} can't autotune over its command link — its PX4 is "
                              f"reached via the DDS bridge, where autotune's command returns "
                              f"UNSUPPORTED. Tune it with the temporary MAVLink-on-TELEM2 "
                              f"procedure instead. Nothing was sent.")}
        if not veh.link.snapshot().get("connected"):
            return {"ok": False, "vehicle": veh.name,
                    "error": f"{veh.name} is offline — no link"}
        # Ready-for-Flight gate: autotune is in-flight oscillation.
        if not safety.is_ready(veh.id):
            return safety.refusal_not_ready(veh.id)
        if not bool(args.get("confirm", False)):
            # Safety gate — surface the preconditions and ask; do NOT start.
            return {"ok": False, "vehicle": veh.name, "state": "IDLE",
                    "confirm_required": True, "reason": AUTOTUNE_SAFETY,
                    "note": (f"Autotune on {veh.name} is an in-flight maneuver. "
                             f"{AUTOTUNE_SAFETY} Confirm to begin.")}
        res = await autotune_manager.start(veh.id, veh.link)
        # Speak the real outcome when the tune reaches a terminal state (driven by
        # PX4's ACK; reached only by autotune-capable vehicles, after the guard above).
        completion.autotune(veh.name, veh.id, queue_voice_event)
        return {"ok": True, "vehicle": veh.name, **res,
                "note": (f"{veh.name} autotune started — oscillating each axis "
                         f"(~40 s). New gains apply on landing/disarm.")}
    if name == "cancel_autotune":
        spoken_at = str(args.get("vehicle", "")).strip()
        if spoken_at:
            at_vid = _resolve_vehicle_id(spoken_at)
            if at_vid is None:
                return {"ok": False, "error": f"unknown vehicle {args.get('vehicle')}"}
            veh = registry.get(at_vid)
        else:
            try:
                veh = registry.active_vehicle()
            except RuntimeError:
                return {"ok": False, "error": "no active vehicle"}
        res = await autotune_manager.cancel(veh.id, veh.link)
        return {"ok": True, "vehicle": veh.name, **res,
                "note": f"{veh.name} autotune cancelled."}

    # Resolve which drone this command targets: an explicit `vehicle` arg (mapped
    # case-insensitively to a registry id) or, when omitted, the active vehicle.
    spoken = args.get("vehicle")
    vid = None
    if spoken:
        vid = _resolve_vehicle_id(str(spoken))
        if vid is None:
            return {"ok": False, "error": f"unknown vehicle {spoken}"}

    try:
        # Resolve the target INSIDE the try: registry.active_vehicle() raises
        # RuntimeError when the registry is empty, and that must NOT propagate out
        # of dispatch — it's awaited in the voice session's receive loop with no
        # guard, so an unhandled raise here would tear down the live voice session
        # mid-turn. Return ok:false instead so the session survives.
        _target = registry.get(vid) if vid else registry.active_vehicle()
        link = _target.link
        _vname = _target.name  # human name for action-completion reports
        # Ready-for-Flight gate: refuse flight-authorizing tools when this
        # vehicle's gate is OFF. Recovery / read-only / bench-only tools bypass
        # (see _VOICE_RECOVERY_TOOLS). set_mode gets a nested exemption for
        # LAND/RTL/HOLD (the recovery modes) inside its branch.
        if name not in _VOICE_RECOVERY_TOOLS:
            if name == "set_mode":
                _target_mode = str(args.get("mode", "")).upper()
                _mode_bypass = _target_mode in ("LAND", "RTL", "HOLD")
            else:
                _mode_bypass = False
            if not _mode_bypass and not safety.is_ready(_target.id):
                return safety.refusal_not_ready(_target.id)
        if name == "arm":
            # Wait for PX4's real arm outcome. PX4 rejects arming on a failed
            # preflight/precondition check (e.g. no GPS lock indoors); surface
            # ok:false + the reason so STADO says "Outrider arm denied: …"
            # rather than falsely reporting it armed.
            res = await commands.arm(link)
            if res.get("ok"):
                return {"ok": True, "armed": True, "vehicle": _vname,
                        "note": f"{_vname} armed."}
            return {"ok": False, "armed": False, "vehicle": _vname,
                    "result": res.get("result_name") or res.get("result"),
                    "reason": res.get("reason"),
                    "note": f"{_vname} arm denied: {res.get('reason')}"}
        elif name == "disarm":
            res = await commands.disarm(link)
            if res.get("ok"):
                return {"ok": True, "armed": False, "vehicle": _vname,
                        "note": f"{_vname} disarmed."}
            return {"ok": False, "vehicle": _vname,
                    "result": res.get("result_name") or res.get("result"),
                    "reason": res.get("reason"),
                    "note": f"{_vname} disarm failed: {res.get('reason')}"}
        elif name == "takeoff":
            _alt = float(args.get("altitude_m", 10))
            _override = bool(args.get("override", False))
            # SAFETY: refuse a too-high takeoff (max-altitude ceiling) unless the
            # operator explicitly overrode. Returns a structured refusal so STADO
            # states the conflict and asks before retrying with override=true.
            if safety.exceeds(_alt) and not _override:
                return safety.refusal(_alt, _vname)
            # commands.takeoff gates NAV_TAKEOFF on a confirmed arm and returns
            # {ok, reason}: if arming was DENIED/timed out it did NOT launch.
            # Honour that contract so we never report a false takeoff success (and
            # never start a completion watcher for a drone that never armed) — the
            # same truthful pattern the arm handler above uses.
            res = await commands.takeoff(link, _alt, override=_override)
            if isinstance(res, dict) and not res.get("ok", True):
                return {"ok": False, "armed": False, "vehicle": _vname,
                        "result": res.get("result_name") or res.get("result"),
                        "reason": res.get("reason"),
                        "note": f"{_vname} takeoff aborted — arm denied: {res.get('reason')}"}
            completion.takeoff(_vname, link, _alt, queue_voice_event)
            _ovr = " (altitude override applied)" if (_override and safety.exceeds(_alt)) else ""
            return {"ok": True, "vehicle": _vname,
                    "override_applied": bool(_override and safety.exceeds(_alt)),
                    "note": f"{_vname} lifting off to {round(_alt)} m{_ovr}."}
        elif name == "land":
            await commands.land(link)
            completion.land(_vname, link, queue_voice_event)
        elif name == "return_to_launch":
            await commands.rtl(link)
            completion.rtl(_vname, link, queue_voice_event)
        elif name == "hold":
            await commands.hold(link)
        elif name == "set_mode":
            # H1: never command a non-offboard-capable vehicle (Outrider) into
            # OFFBOARD — the DDS bridge can't stream setpoints, so PX4 would drop
            # OFFBOARD within ~0.5 s and fail safe, stranding a flying drone.
            if str(args["mode"]).strip().upper() == "OFFBOARD":
                refusal = _offboard_refusal(_target.id, _vname, "OFFBOARD mode")
                if refusal is not None:
                    return refusal
            await commands.set_mode(link, str(args["mode"]))
        elif name == "track_target":
            desc = str(args["description"])
            # Outrider tracks ONBOARD its Jetson (low-latency follow). The GCS only
            # seeds the initial region: grab an Outrider frame, ground a box with Qwen,
            # and hand it to the onboard tracker over the WORKING UDP :8771 channel
            # (onboard_track.seed), which then holds + follows it locally.
            if (vid or registry.active_id()) == "outrider":
                jpeg = await grab_outrider_frame()
                if not jpeg:
                    return {"ok": False, "error": "Outrider video not reachable (Jetson)"}
                box = await grounding.resolve_target(jpeg, desc)
                if not box:
                    return {"ok": False, "error": "target not found in Outrider view"}
                # grounding.resolve_target returns [x0,y0,x1,y1] normalized corners;
                # onboard_track.seed wants top-left + size. Convert + clamp 0..1 and
                # guard against a degenerate/tiny box.
                x0, y0, x1, y1 = box
                x = clamp01(min(x0, x1))
                y = clamp01(min(y0, y1))
                w = clamp01(abs(x1 - x0))
                h = clamp01(abs(y1 - y0))
                if w < 0.02 or h < 0.02:
                    return {"ok": False, "error": "resolved box too small to seed"}
                res = onboard_track.seed(x, y, w, h)
                # AUTO-select the follow speed envelope from the target CLASS in
                # the operator's words ("follow the car" → car profile). Remember
                # it so a subsequent `follow` (no description) flies that envelope,
                # and pre-arm the controller's PROFILE now. Unknown class → leave
                # the controller on its default (set_profile no-ops a bad name).
                global _outrider_follow_profile
                prof = onboard_track.normalize_profile(desc)
                if prof:
                    _outrider_follow_profile = prof
                    pres = onboard_track.set_profile(prof)
                    if isinstance(res, dict):
                        res = {**res, "profile": pres.get("profile", prof)}
                return res
            pipe = _ensure_pipeline()  # auto-start the tracker if it's off
            jpeg = await grab_live_frame()
            box = await grounding.resolve_target(jpeg, desc) if jpeg else None
            if not jpeg:
                return {"ok": False, "error": "no camera frame — feed not delivering video"}
            if not box:
                return {"ok": False, "error": "target not found in view"}
            pipe.seed_tracker(box, desc[:18])
            pipe.track_description = desc
            return {"ok": True}
        elif name == "follow":
            enable = bool(args.get("enable", True))
            # Outrider follows ONBOARD: enable/disable the onboard follow controller
            # over the WORKING UDP :8771 channel (FOLLOW 1 / FOLLOW 0). The onboard
            # FollowController closes the loop on the locked target.
            if (vid or registry.active_id()) == "outrider":
                # Fly the speed envelope inferred from the tracked target's class
                # (set by the last track_target on Outrider); falls back to the
                # configured default profile in onboard_track when none was inferred.
                prof = _outrider_follow_profile if enable else None
                res = onboard_track.follow(enable, prof)
                return {**res, "follow": enable,
                        "note": ("Outrider following the locked target"
                                 + (f" ({res.get('profile')})" if res.get("profile") else "")
                                 if enable else "Outrider follow disabled")}
            # H1: a GCS-side follow streams OFFBOARD setpoints. Refuse it for a
            # vehicle that can't take GCS OFFBOARD (Outrider's onboard path is
            # handled above; this is the non-onboard, GCS-OFFBOARD follow).
            if enable:
                refusal = _offboard_refusal(_target.id, _vname, "GCS follow")
                if refusal is not None:
                    return refusal
            pipe = _ensure_pipeline()  # auto-start the tracker if it's off
            pipe.set_follow(enable)
            # Engage offboard so the streamed follow setpoints actually take effect.
            if enable and link.snapshot().get("connected"):
                asyncio.create_task(commands.start_offboard(link))
            return {"ok": True, "follow": enable}
        elif name == "orbit_target":
            pipe = get_pipeline()
            s = link.snapshot()
            if not pipe or pipe.target_box() is None:
                return {"ok": False, "error": "no target selected"}
            geo = geolocate_target(pipe.target_box(), s.get("lat"), s.get("lon"),
                                   s.get("alt_rel"), s.get("heading") or 0.0, settings.camera_hfov_deg,
                                   settings.camera_pitch_deg)
            if not geo:
                return {"ok": False, "error": "cannot geolocate target"}
            radius = float(args.get("radius_m", 25))
            _override = bool(args.get("override", False))
            # Floor at MIN_ORBIT_ALT_M (10 m) — same reason as orbit_point.
            _orbit_alt = max(s.get("alt_rel") or 20, commands.MIN_ORBIT_ALT_M)
            # SAFETY: refuse orbiting above the ceiling (derived altitude) unless overridden.
            if safety.exceeds(_orbit_alt) and not _override:
                return safety.refusal(_orbit_alt, _vname)
            await commands.orbit(link, geo[0], geo[1], _orbit_alt, radius, 4, override=_override)
            completion.orbit(_vname, link, geo[0], geo[1], radius, queue_voice_event, label="the target")
            return {"ok": True, "radius_m": round(radius, 1)}
        elif name == "move":
            _up = float(args.get("up_m", 0))
            _override = bool(args.get("override", False))
            # SAFETY: a climb above the ceiling (derived from current alt + up) is
            # refused unless overridden. Check the derived target before sending.
            _derived = (link.snapshot().get("alt_rel") or 0.0) + _up
            if safety.exceeds(_derived) and not _override:
                return safety.refusal(_derived, _vname)
            res = await commands.move_relative(
                link, float(args.get("forward_m", 0)), float(args.get("right_m", 0)),
                _up, override=_override)
            return {"ok": True, **res}
        elif name == "turn":
            _deg = float(args.get("degrees", 90))
            _dir = str(args.get("direction", "left"))
            # Per-vehicle turn. Overwatch (GCS-offboard) uses the precise OFFBOARD
            # yaw-RATE turn (direction guaranteed by the rate sign). Outrider (DDS,
            # no GCS offboard — the OFFBOARD turn would strand it) yaws via
            # DO_REPOSITION to current±degrees, which rides the same command path as
            # goto/orbit over the bridge. Shortest-path direction is exact for <=180°.
            if registry.supports_offboard(_target.id):
                res = await commands.turn(link, _deg, _dir)
            else:
                res = await commands.turn_to_heading(link, _deg, _dir)
            return {"ok": True, **res}
        elif name == "set_speed":
            res = await commands.set_speed(link, float(args.get("speed_ms", 5)))
            return {"ok": True, **res}
        elif name == "list_points":
            return {"ok": True, "points": pois.get_pois()}
        elif name == "goto_point":
            p = pois.find(str(args.get("name", "")))
            if not p:
                return {"ok": False, "error": f"no marker named '{args.get('name')}'"}
            _override = bool(args.get("override", False))
            _alt = (float(args["altitude_m"]) if args.get("altitude_m") is not None
                    else (link.snapshot().get("alt_rel") or 30))
            # SAFETY: refuse a goto above the ceiling unless overridden.
            if safety.exceeds(_alt) and not _override:
                return safety.refusal(_alt, _vname)
            await commands.goto(link, p["lat"], p["lng"], _alt, override=_override)
            completion.goto(_vname, link, p["lat"], p["lng"], queue_voice_event, label=p["name"])
            return {"ok": True, "point": p["name"]}
        elif name == "orbit_point":
            p = pois.find(str(args.get("name", "")))
            if not p:
                return {"ok": False, "error": f"no marker named '{args.get('name')}'"}
            radius = float(args.get("radius_m", 25))
            _override = bool(args.get("override", False))
            # Floor at MIN_ORBIT_ALT_M (10 m) so a no-altitude "orbit X" never
            # inherits a sagging current altitude (2026-05-27 incident); commands.orbit
            # floors too, but mirror it here so the spoken altitude matches what's flown.
            _alt = (float(args["altitude_m"]) if args.get("altitude_m") is not None
                    else (link.snapshot().get("alt_rel") or 30))
            _alt = max(_alt, commands.MIN_ORBIT_ALT_M)
            # SAFETY: refuse orbiting above the ceiling unless overridden.
            if safety.exceeds(_alt) and not _override:
                return safety.refusal(_alt, _vname)
            await commands.orbit(link, p["lat"], p["lng"], _alt, radius, 4, override=_override)
            completion.orbit(_vname, link, p["lat"], p["lng"], radius, queue_voice_event, label=p["name"])
            return {"ok": True, "point": p["name"], "radius_m": round(radius, 1)}
        elif name == "get_status":
            s = link.snapshot()
            # The fleet max-altitude ceiling is process-global (one for all drones).
            # Echo it on EVERY status so STADO/the operator can see the active limit.
            _ceiling = safety.get_max_altitude()
            if not s.get("connected"):
                # Offline: return NO stale telemetry — just an unmissable verdict so
                # the model can't infer "online" from leftover fields.
                return {"connected": False, "link": "NO LINK",
                        "max_altitude_m": _ceiling,
                        "note": ("This vehicle is OFFLINE / not connected — there is NO live "
                                 "telemetry. Do NOT claim it is connected, online, ready, armed, "
                                 "or disarmed; report that it has no link.")}
            return {"connected": True, "link": "connected", "armed": s["armed"], "mode": s["mode"],
                    "lat": s["lat"], "lon": s["lon"], "alt_agl_m": s["alt_rel"],
                    "heading_deg": s["heading"], "groundspeed_ms": s["groundspeed"],
                    "battery_pct": s["battery_pct"], "gps_fix": s["gps_fix"], "satellites": s["satellites"],
                    "max_altitude_m": _ceiling}
        elif name == "get_vehicle_id":
            pipe = get_pipeline()
            vi = pipe.vehicle_info if pipe else None
            if not vi:
                return {"ok": True, "identified": False,
                        "note": "no plate read yet — must be tracking a vehicle with a legible plate"}
            return {"ok": True, "identified": True, **vi}
        elif name == "describe_view":
            # Pick the feed: Overwatch (default) or Outrider (= the second feed / Feed 2).
            feed = str(args.get("feed", "")).strip().lower()
            outrider = feed in ("outrider", "feed2", "feed 2", "second", "two", "2")
            jpeg = await (grab_outrider_frame() if outrider else grab_live_frame())
            if not jpeg:
                which = "Outrider" if outrider else "Overwatch"
                return {"ok": False, "error": f"no decodable {which} camera frame — is that feed live?"}
            desc = await grounding.describe_scene(jpeg, str(args.get("question", "")))
            return {"ok": True, "feed": "outrider" if outrider else "overwatch",
                    "description": desc or "I can't make out the view clearly."}
        elif name == "find_survey_perimeters":
            # Detection CENTER: a named marked point if given (e.g. "surveys around
            # point A"), else the drone's current position. Threading the center
            # into the detector makes it propose candidate areas THERE.
            from .survey_vision import PerimetersReq, perimeters as detect
            global _last_perimeters
            pname = str(args.get("point", "")).strip()
            if pname:
                p = pois.find(pname)
                if not p:
                    return {"ok": False, "error": f"no marker named '{pname}'"}
                center_lat, center_lon, center_label = p["lat"], p["lng"], p["name"]
            else:
                s = link.snapshot()
                if s.get("lat") is None:
                    return {"ok": False, "error": "no GPS fix"}
                center_lat, center_lon, center_label = s["lat"], s["lon"], None
            resp = await detect(PerimetersReq(lat=center_lat, lon=center_lon, zoom=18))
            _last_perimeters = resp.perimeters
            if not _last_perimeters:
                return {"ok": False, "error": "no perimeters found in the imagery"}
            # Push the full candidates (with polygons) to the GCS so they render on
            # the satellite map and the operator can tap to pick — see PerimeterPlanner.
            # `center` lets the map recenter on point A when surveying around a marker.
            hub.publish_threadsafe({
                "type": "survey_perimeters",
                "center": [float(center_lat), float(center_lon)],
                "around": center_label,
                "perimeters": [
                    {"label": p.label, "description": p.description,
                     "polygon": [[float(a), float(b)] for a, b in p.polygon]}
                    for p in _last_perimeters]})
            return {"ok": True, "shown_on_map": True,
                    "around": center_label,
                    "perimeters": [
                        {"number": i + 1, "label": p.label, "description": p.description}
                        for i, p in enumerate(_last_perimeters)]}
        elif name == "plan_survey_mission":
            # PLAN + PREVIEW only — never fly here. Stage the cleaned mission and
            # push a preview (clean polygon + lawnmower path) to the GCS so the
            # operator sees exactly what would fly, then confirms.
            idx = int(args.get("choice", 1)) - 1
            if not _last_perimeters or not (0 <= idx < len(_last_perimeters)):
                return {"ok": False, "error": "no such option — run find_survey_perimeters first"}
            chosen = _last_perimeters[idx]
            try:
                clean = clean_polygon([(lat, lon) for lat, lon in chosen.polygon])
            except ValueError as exc:
                return {"ok": False, "error": f"polygon invalid: {exc}"}
            grid = plan_survey(clean, altitude=30, line_spacing_m=_DEFAULT_LINE_SPACING_M)
            if not grid:
                return {"ok": False, "error": "polygon too small for the survey line spacing"}
            wps = missions.survey_mission(grid, 30)
            with _pending_survey_lock:
                _pending_survey = {
                    "id": _next_survey_id(),
                    "label": chosen.label, "polygon": clean,
                    "vehicle": vid, "altitude": 30.0,
                    "line_spacing_m": _DEFAULT_LINE_SPACING_M,
                }
                _pending_fleet_survey = None
            hub.publish_threadsafe({"type": "survey_selected", "choice": idx})
            hub.publish_threadsafe({
                "type": "survey_pending",
                "label": chosen.label,
                "choice": idx,
                "polygon": [[float(a), float(b)] for a, b in clean],
                "path": [[w.lat, w.lon] for w in grid],
                "waypoints": len(wps),
            })
            return {"ok": True, "planned": chosen.label, "waypoints": len(wps),
                    "preview_on_map": True,
                    "note": "Mission planned and previewed — ask the operator to confirm before flying."}
        elif name == "execute_survey":
            # Confirm + FLY the staged survey. This handles BOTH:
            #  • a coordinated FLEET/region survey (staged by survey_region) —
            #    upload + start each per-drone mission; and
            #  • a single-region survey (staged by plan_survey_mission / the map).
            # Confirmation gating lives in the SYSTEM_PROMPT — the model only calls
            # this after the operator confirms. A `choice` arg forces the
            # single-region path (re-plan that perimeter first).
            # M9: CLAIM the staged plan atomically under the lock and clear the slot
            # in the same critical section, so a concurrent stage (map racing voice)
            # can't change what we fly mid-execution and a double-confirm can't
            # double-fly. We fly the captured copy below.
            fleet_choice = args.get("choice") is None
            with _pending_survey_lock:
                fleet = _pending_fleet_survey if fleet_choice else None
                if fleet is not None:
                    _pending_fleet_survey = None
            if fleet:
                results = []
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
                    spacing = float(z.get("line_spacing_m", _DEFAULT_LINE_SPACING_M))
                    grid = plan_survey(
                        [(float(a), float(b)) for a, b in z["polygon"]],
                        altitude=alt, line_spacing_m=spacing)
                    wps = missions.survey_mission(grid, alt)
                    if not await missions.upload(veh.link, wps):
                        results.append({"vehicle": z["vehicle"], "name": z.get("name"),
                                        "error": "mission rejected by vehicle"})
                        continue
                    await missions.start(veh.link)
                    flew = True
                    results.append({"vehicle": z["vehicle"], "name": z.get("name"),
                                    "altitude": alt, "waypoints": len(wps)})
                failed = sum(1 for r in results if r.get("error"))
                if not flew:
                    return {"ok": False, "error": "no drone accepted the survey mission",
                            "failed_zones": failed, "assignments": results}
                label = fleet["label"]
                # Mark the previewed zones as flying so the GCS recolors them solid.
                hub.publish_threadsafe({
                    "type": "fleet_zones", "label": label, "flying": True,
                    "zones": fleet["zones"]})
                hub.publish_threadsafe({"type": "survey_committed", "label": label})
                # ok=True = at least one zone launched; failed_zones surfaces partial
                # success so the agent can report it honestly (not a blanket success).
                return {"ok": True, "surveying": label,
                        "failed_zones": failed, "assignments": results}
            # Single-region path: claim the staged single-region plan under the lock.
            with _pending_survey_lock:
                staged = _pending_survey
            if args.get("choice") is not None:
                idx = int(args["choice"]) - 1
                if not _last_perimeters or not (0 <= idx < len(_last_perimeters)):
                    return {"ok": False, "error": "no such option — run find_survey_perimeters first"}
                chosen = _last_perimeters[idx]
                try:
                    clean = clean_polygon([(lat, lon) for lat, lon in chosen.polygon])
                except ValueError as exc:
                    return {"ok": False, "error": f"polygon invalid: {exc}"}
                staged = {"label": chosen.label, "polygon": clean,
                          "vehicle": vid, "altitude": 30.0,
                          "line_spacing_m": _DEFAULT_LINE_SPACING_M}
                hub.publish_threadsafe({"type": "survey_selected", "choice": idx})
            if not staged:
                return {"ok": False, "error": "no planned survey to fly — plan one first"}
            # Connectivity check before the blocking upload handshake, matching the
            # fleet branch — an offline drone returns a clear error instead of a slow
            # per-item timeout. Resolve the staged drone's link (staging may target a
            # specific vehicle); fall back to the dispatch-resolved link.
            fly_link = link
            if staged.get("vehicle"):
                try:
                    fly_link = registry.get(staged["vehicle"]).link
                except KeyError:
                    return {"ok": False, "error": f"unknown vehicle {staged['vehicle']}"}
            if not fly_link.snapshot().get("connected"):
                return {"ok": False, "error": "target drone is offline — no link"}
            alt = float(staged.get("altitude", 30.0))
            spacing = float(staged.get("line_spacing_m", _DEFAULT_LINE_SPACING_M))
            grid = plan_survey(staged["polygon"], altitude=alt, line_spacing_m=spacing)
            wps = missions.survey_mission(grid, alt)
            if not await missions.upload(fly_link, wps):
                return {"ok": False, "error": "mission rejected by the vehicle"}
            await missions.start(fly_link)
            # Clear the slot only if it's STILL the plan we flew (identity check by
            # id) — a concurrent re-stage between our claim and here must survive.
            with _pending_survey_lock:
                if _pending_survey is not None and (
                    staged.get("id") is None
                    or _pending_survey.get("id") == staged.get("id")
                ):
                    _pending_survey = None
            hub.publish_threadsafe({"type": "survey_committed", "label": staged["label"]})
            return {"ok": True, "surveying": staged["label"], "waypoints": len(wps)}
        elif name == "cancel_survey":
            # Discard whatever survey is staged — a single-region OR a fleet/region
            # pending survey — and clear its preview on the map.
            with _pending_survey_lock:
                had = _pending_survey is not None or _pending_fleet_survey is not None
                had_fleet = _pending_fleet_survey is not None
                _pending_survey = None
                _pending_fleet_survey = None
            hub.publish_threadsafe({"type": "survey_cancelled"})
            if had_fleet:
                # Clear the previewed fleet zones too (the single-region path's
                # survey_cancelled already drops surveyPreview on the frontend).
                hub.publish_threadsafe({"type": "fleet_zones", "flying": False, "zones": []})
            return {"ok": True, "cancelled": had}
        else:
            return {"ok": False, "error": f"unknown tool {name}"}
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        log.exception("dispatch %s failed", name)
        return {"ok": False, "error": str(exc)}


def _fleet_status_line() -> str:
    """Live, ground-truth fleet connectivity computed AT SESSION-CONNECT TIME.

    Read straight from each vehicle's MAVLink link snapshot so the model KNOWS
    the real state before it ever greets — this kills the greeting that
    hallucinated "both connected". Embedded in the system_instruction (NOT a
    separate context turn), so it can't poison turn handling like prime_context
    did. The model still calls get_status for fresh state mid-session.
    """
    parts: list[str] = []
    for v in registry.list():
        try:
            connected = bool(v.link.snapshot().get("connected"))
        except Exception:  # noqa: BLE001 — a flaky snapshot must never break setup
            connected = False
        parts.append(f"{v.name} = {'CONNECTED (live link)' if connected else 'OFFLINE (NO LINK)'}")
    if not parts:
        return ""
    return (
        "\nCURRENT FLEET STATUS (ground truth, as of session start): "
        + "; ".join(parts) + ". "
        "Your OPENING GREETING must report each drone's link status from THIS line "
        "verbatim in meaning: an OFFLINE drone has no link — say it is offline / no link / "
        "not connected. NEVER say an OFFLINE drone is connected, online, ready, communicating, "
        "armed, or disarmed. You may say a drone is connected ONLY if it shows CONNECTED here. "
        "This is the live state at startup; for fresh state later, call get_status."
    )


async def voice_ws(ws: WebSocket) -> None:
    """Bridge a browser WebSocket to a Qwen Realtime voice session."""
    # C1 AUTH: if a shared token is configured, the voice WS — which can speak
    # flight commands — must present it. Browsers can't set headers on a WS, so
    # accept it as a `?token=` query param (header also honored). Reject the
    # handshake BEFORE accept() when it's missing/wrong. No token configured =
    # open (a prominent startup WARNING is logged in main.py).
    from .api import token_ok
    supplied = ws.query_params.get("token") or ws.headers.get("x-api-token")
    if not token_ok(supplied):
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept()
    # vad=manual (default) → push-to-talk with client activity markers;
    # vad=auto → open-mic, the server segments turns (semantic VAD).
    manual_vad = ws.query_params.get("vad", "manual") != "auto"
    from .voice_qwen import qwen_voice_session
    await qwen_voice_session(ws, manual_vad)
