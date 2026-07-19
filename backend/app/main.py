from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from . import flights
from . import recorder
from . import safety
from .ally_overlay import ally_overlay_loop
from .api import router as api_router
from .gcs_beacon import gcs_beacon_loop
from .config import fleet, settings
from .logbus import logbus
from .logs_api import router as logs_router
from .mavlink.link import get_link
from .mavlink.registry import registry
from .survey_vision import router as survey_vision_router
from .vision_api import router as vision_router
from .voice import voice_ws, queue_voice_alert
from .ws.hub import hub

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
)
log = logging.getLogger("gcs")

TELEMETRY_HZ = 10

# Smart-RTL latch: True once we've warned about a vehicle's low battery, so the
# alert fires ONCE per descent below the floor (not 10×/sec). Reset on the ground.
_LOW_BATT_WARNED: dict[str, bool] = {}


async def _check_low_battery(vid: str, name: str, snap: dict[str, Any]) -> None:
    """Smart RTL: when an ARMED, connected vehicle's battery reaches the floor,
    notify the operator (hub `low_battery` event) and ask STADO to confirm an RTL
    (it does NOT auto-RTL — confirmation is required). Latches per-vehicle and
    resets on disarm / unknown battery / recovery (battery swap) so the next
    flight can warn again."""
    if not settings.smart_rtl_enabled:
        return
    bp = snap.get("battery_pct")
    thr = settings.low_battery_pct
    if not snap.get("armed") or bp is None or bp > thr + 5:
        _LOW_BATT_WARNED[vid] = False
        return
    if not snap.get("connected") or not (0 <= bp <= thr) or _LOW_BATT_WARNED.get(vid):
        return
    _LOW_BATT_WARNED[vid] = True
    log.warning("LOW BATTERY: %s at %s%% (floor %s%%)", name, bp, thr)
    await hub.publish({"type": "low_battery", "vehicle": vid, "name": name,
                       "battery_pct": bp, "threshold": thr})
    queue_voice_alert(
        f"{name} battery is at {int(bp)} percent, at or below the {int(thr)} percent floor. "
        f"Warn the operator and ask whether to return to launch now. On confirmation, call "
        f"return_to_launch with vehicle '{vid}'.")


async def _telemetry_loop() -> None:
    """Publish telemetry for every vehicle, tagged with its id. The existing
    frontend reads the active vehicle's telemetry; adding the `vehicle` field
    is additive and safe."""
    period = 1.0 / TELEMETRY_HZ
    while True:
        # Fleet-global max-altitude ceiling (metres AGL; None = no ceiling). Echoed
        # on every telemetry frame so the UI can show the active limit.
        ceiling = safety.get_max_altitude()
        for v in registry.list():
            snap = v.link.snapshot()
            # Feed the flight recorder so it can detect arm→disarm flights and
            # accumulate stats/path from the same telemetry the UI sees.
            flights.feed_telemetry(v.id, v.name, snap)
            await _check_low_battery(v.id, v.name, snap)
            # Ready-for-Flight: restart safety. On the FIRST telemetry frame per
            # vehicle, if the drone is already armed+airborne, seed the gate ON.
            # Idempotent — only fires on the first frame per vehicle.
            armed = bool(snap.get("armed"))
            alt_rel = snap.get("alt_rel")
            safety.seed_from_telemetry(v.id, armed, alt_rel)
            await hub.publish({
                "type": "telemetry", "vehicle": v.id, "data": snap,
                "max_altitude_m": ceiling,
                # Per-vehicle gate state echoed on every frame so the UI can
                # react instantly to auto-lock transitions without polling.
                "ready_for_flight": safety.is_ready(v.id),
                "ready_for_flight_locked": safety.is_locked(armed, alt_rel),
            })
        await asyncio.sleep(period)


def _recorder_event_tap(event: dict[str, Any]) -> None:
    """Forward mode/statustext events to the per-vehicle flight recorder, then
    publish to the hub as before. Wraps hub.publish_threadsafe so the recorder
    sees the same event stream the frontend does (events are vehicle-tagged)."""
    vid = event.get("vehicle")
    if vid is not None:
        try:
            flights.feed_event(vid, event)
        except Exception:
            log.exception("flight recorder event tap failed")
    hub.publish_threadsafe(event)


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    hub.bind_loop(loop)

    # Bring up the structured audit log first so startup itself is captured.
    logbus.init(
        enabled=settings.audit_log_enabled,
        log_dir=settings.audit_log_dir,
        telemetry_hz=settings.audit_log_telemetry_hz,
    )
    logbus.log("lifecycle", None, None, kind="startup", event="backend_starting")

    # Seed the runtime max-altitude ceiling from config (None = no ceiling).
    safety.init_default(settings.max_altitude_m)

    # C1 AUTH: warn LOUDLY when the command surface is unauthenticated. With no
    # api_token set, anyone who can reach this host (we bind 0.0.0.0) can arm and
    # fly the aircraft via the REST command router or the voice WS. Set
    # GCS_API_TOKEN to require an X-API-Token header (?token= for WebSockets).
    if not settings.api_token:
        log.warning(
            "SECURITY: GCS_API_TOKEN is NOT set — the command API and voice/telemetry "
            "WebSockets are UNAUTHENTICATED. Anyone who can reach %s:%s can ARM and FLY "
            "the aircraft. Set GCS_API_TOKEN to lock the command surface down.",
            settings.gcs_host, settings.gcs_port,
        )
        logbus.log("lifecycle", None, None, kind="startup",
                   event="command_surface_unauthenticated", warning=True)
    else:
        log.info("command surface is token-protected (X-API-Token / ?token= required)")

    # Register the fleet — each vehicle gets its own MavlinkLink + reader thread.
    for vd in fleet():
        registry.add(
            vd["id"], vd["name"], vd["kind"], vd["connection"],
            active=vd.get("active", False),
            supports_offboard=vd.get("supports_offboard", True),
            supports_missions=vd.get("supports_missions", True),
            supports_autotune=vd.get("supports_autotune", True),
        )
        logbus.log(
            "lifecycle", None, vd["id"], kind="vehicle_registered",
            name=vd["name"], vehicle_kind=vd["kind"], connection=vd["connection"],
            active=vd.get("active", False),
        )
    # When a flight finalizes, notify the frontend over the hub.
    flights.set_on_complete(
        lambda summary: hub.publish_threadsafe({"type": "flight_complete", "flight": summary})
    )
    # Wire every vehicle's events through the recorder tap, then to the hub. The
    # tap lets the recorder see mode/statustext events (vehicle-tagged) too.
    registry.start_all(on_event=_recorder_event_tap)
    app.state.registry = registry

    # Ready-for-Flight unwind hooks. When any vehicle's gate transitions to OFF,
    # every registered hook runs (best-effort, failures logged). This tears down
    # live command loops so a gate close IS a full stop — no coordination goto
    # still firing, no follow setpoint stream still hitting PX4, no onboard
    # tracker still moving Outrider, no autotune still oscillating axes.
    def _hook_stop_coordination(vehicle_id: str) -> None:
        # coordination.stop_all() is fleet-wide (there's no per-vehicle stop),
        # but that's fine — a gate close on ANY drone should halt shared behaviors.
        from .coordination import coordination
        stopped = coordination.stop_all()
        if stopped:
            log.info("ready-for-flight OFF for %s → stopped coordination: %s",
                     vehicle_id, stopped)

    def _hook_stop_follow(vehicle_id: str) -> None:
        from .vision.pipeline import get_pipeline
        pipe = get_pipeline()
        if pipe is not None:
            pipe.set_follow(False)

    def _hook_stop_onboard_track(vehicle_id: str) -> None:
        # The onboard tracker only targets Outrider; a gate close on Overwatch is
        # a no-op here (harmless — onboard_track.follow sends a stop over UDP).
        if vehicle_id == "outrider":
            from . import onboard_track
            try:
                onboard_track.follow(False, None)
            except Exception:  # noqa: BLE001
                log.exception("failed to stop onboard track for %s", vehicle_id)

    def _hook_cancel_autotune(vehicle_id: str) -> None:
        from .autotune import manager as _atm
        try:
            link = registry.get(vehicle_id).link
        except KeyError:
            return
        # autotune_manager.cancel is async — schedule it on the loop.
        try:
            asyncio.create_task(_atm.cancel(vehicle_id, link))
        except RuntimeError:
            # No running loop (e.g. during test teardown). Best-effort only.
            pass

    safety.register_unwind_hook(_hook_stop_coordination)
    safety.register_unwind_hook(_hook_stop_follow)
    safety.register_unwind_hook(_hook_stop_onboard_track)
    safety.register_unwind_hook(_hook_cancel_autotune)

    task = asyncio.create_task(_telemetry_loop())
    # AR ally marker: project Outrider's GPS into Overwatch's camera frame and
    # publish `ally_overlay` events at ~6 Hz (degrades to nothing without fixes).
    ally_task = asyncio.create_task(ally_overlay_loop())
    # GCS-IP beacon: teach Outrider's Jetson our current IP so it can push its
    # video here even after the Mac's DHCP IP changes (IP-independent transport).
    beacon_task = asyncio.create_task(gcs_beacon_loop())
    log.info(
        "GCS backend up — %d vehicle(s), active=%s",
        len(registry.list()),
        registry.active_id(),
    )
    logbus.log(
        "lifecycle", None, None, kind="startup", event="backend_up",
        vehicles=len(registry.list()), active=registry.active_id(),
    )
    try:
        yield
    finally:
        logbus.log("lifecycle", None, None, kind="shutdown", event="backend_stopping")
        task.cancel()
        ally_task.cancel()
        beacon_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(asyncio.CancelledError):
            await ally_task
        with contextlib.suppress(asyncio.CancelledError):
            await beacon_task
        registry.stop_all()
        # Clean-stop any in-flight MP4 recordings so their moov atom is written
        # (a SIGKILL'd ffmpeg leaves an unplayable file).
        recorder.stop_all()
        logbus.stop()


app = FastAPI(title="GCS Backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)
app.include_router(vision_router)
app.include_router(survey_vision_router)
app.include_router(logs_router)


@app.get("/health")
async def health():
    return {"ok": True}


@app.websocket("/ws/voice")
async def voice_endpoint(ws: WebSocket):
    await voice_ws(ws)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    # C1 AUTH: gate the telemetry/event WS behind the shared token when set
    # (open when unset). Browsers can't set WS headers → accept ?token= too.
    from .api import token_ok
    supplied = ws.query_params.get("token") or ws.headers.get("x-api-token")
    if not token_ok(supplied):
        await ws.close(code=1008)
        return
    await hub.connect(ws)
    try:
        # Push current state immediately so a fresh client isn't blank. Use the
        # active vehicle's snapshot to preserve the existing single-vehicle UX.
        active = registry.active_vehicle()
        await ws.send_json(
            {"type": "telemetry", "vehicle": active.id, "data": active.link.snapshot()}
        )
        while True:
            # We don't expect inbound messages yet, but keep the socket alive
            # and drain anything the client sends (e.g. future track-select).
            await ws.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(ws)
    except Exception:
        hub.disconnect(ws)
