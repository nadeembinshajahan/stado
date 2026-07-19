from __future__ import annotations

import asyncio
import logging
import math

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import onboard_track
from . import safety
from . import vehicle_lookup
from .config import settings
from .mavlink import commands
from .mavlink.link import get_link
from .mavlink.registry import registry
from .vision import grounding
from .vision.follow import Setpoint, geolocate_box, geolocate_target
from .vision.pipeline import get_pipeline, init_pipeline
from .ws.hub import hub

log = logging.getLogger("gcs.vision_api")
router = APIRouter(prefix="/api/vision")

# YOLO/COCO labels we geolocate onto the map, normalized to a stable class set
# the frontend renders an icon for. Any label not in this map is skipped (the
# detector still tracks it; it just isn't pinned to the ground map).
MAP_OBJECT_CLASSES: dict[str, str] = {
    "car": "car",
    "person": "person",
    "bicycle": "bicycle",
    "bike": "bicycle",
    "motorcycle": "motorcycle",
    "motorbike": "motorcycle",
    "truck": "truck",
    "bus": "bus",
}


def _classify_lock(label: str) -> str:
    """Map a CSRT/VLM lock's `label` to a renderable MAP_OBJECT_CLASSES class.

    A YOLO-track lock carries a real COCO class ("person"/"car"/…) that maps
    directly. A VOICE/VLM lock (selected_id == -1) instead carries the truncated
    free-text description (e.g. "the man in the bla"), which is NOT a class — so it
    would fall through to "car" and a tracked person would wrongly show as a car
    glyph. Classify the free text via onboard_track.normalize_profile (it already
    maps "man/woman/pedestrian…" → person and "truck/van/…" → car); fall back to
    "car" only when nothing resolves (preserving the prior default)."""
    direct = MAP_OBJECT_CLASSES.get(str(label or "").lower())
    if direct is not None:
        return direct
    prof = onboard_track.normalize_profile(label)  # "person" | "car" | "custom" | None
    if prof in MAP_OBJECT_CLASSES:
        return prof
    return "car"


class StartReq(BaseModel):
    source: str | None = None


class SelectReq(BaseModel):
    track_id: int | None = None


class FollowReq(BaseModel):
    enable: bool = True


class TrackReq(BaseModel):
    description: str


class AcquireReq(BaseModel):
    description: str
    backend: str | None = None  # "qwen" (default) | "moondream"


class SeedBoxReq(BaseModel):
    """A manual selection rectangle to seed the CSRT tracker, normalized 0-1 of
    the CURRENT frame. (x, y) is the top-left corner; w, h the size. Optional
    label for the on-screen LOCK tag."""

    x: float
    y: float
    w: float
    h: float
    label: str | None = None


def _follow_link():
    """Resolve the link the follow setpoints must be commanded to.

    The pipeline is vehicle-EXPLICIT: if it carries a `target_vehicle_id`
    (pinned by e.g. `pair_overwatch_scout` to the scout), command THAT vehicle's
    link — never the active vehicle, which may be a different drone. Falls back
    to the active vehicle's link when no explicit target is set."""
    pipe = get_pipeline()
    vid = getattr(pipe, "target_vehicle_id", None) if pipe is not None else None
    if vid is not None:
        try:
            return registry.get(vid).link
        except KeyError:
            log.warning("follow target vehicle '%s' not registered — falling back", vid)
    return get_link()


def _on_setpoint(sp: Setpoint, _box: dict) -> None:
    """Forward follow setpoints to the EXPLICIT follow vehicle.

    Gates on the vehicle being connected AND actually in OFFBOARD and armed — a
    setpoint sent to a drone that isn't in offboard/armed is inert (or worse,
    fights the operator), so we only stream once the follow handshake has taken.

    Ready-for-Flight is enforced at the parent /follow endpoint (which starts
    this stream) AND by safety.py's registered unwind hook which calls
    `pipe.set_follow(False)` on any OFF-transition — so a closed gate can't
    leak setpoints even if it's toggled mid-follow. No re-check needed here."""
    link = _follow_link()
    s = link.snapshot()
    if not s.get("connected"):
        return
    mode = str(s.get("mode") or "").upper()
    if mode != "OFFBOARD" or not s.get("armed"):
        return
    commands.send_velocity_body(link, sp.vx, sp.vy, sp.vz, sp.yaw_rate)


_keeper_task: asyncio.Task | None = None
_plate_task: asyncio.Task | None = None
_map_objects_task: asyncio.Task | None = None


def start_map_objects_task() -> None:
    """(Re)start the map-objects geolocation loop for the current pipeline.

    Shared by the REST /api/vision/start handler AND the voice auto-start path
    (voice._ensure_pipeline), so a target acquired by voice gets geolocated onto
    the map exactly like one started via REST. Mirrors the original inline guard:
    cancel a still-running loop and create a fresh one (one loop per pipeline run).
    MUST be called from the asyncio event loop (it uses create_task)."""
    global _map_objects_task
    if _map_objects_task and not _map_objects_task.done():
        _map_objects_task.cancel()
    _map_objects_task = asyncio.create_task(_map_objects_loop())


async def _plate_reader(description: str | None) -> None:
    """Proactive vehicle-ID: while a target is LOCKED, periodically crop it, ask
    the vision model to read its plate, look up ANONYMIZED vehicle info, and
    attach it to the locked target (publishing a `vehicle_id` event).

    Runs as its own asyncio task — it only reads cached frames via the pipeline,
    so the capture thread is never blocked by the VLM round-trip. Self-gates
    on NONE (keeps trying next interval) and exits when the description changes
    or the pipeline stops. ``description`` is None for a manual box seed (no text
    target); the loop keys its work to the lock generation, not the description."""
    if not settings.plate_id_enabled:
        return
    log.info("plate reader started for '%s'", description or "manual lock")
    interval = settings.plate_read_interval_s if settings.plate_read_interval_s > 0 else 4.0
    confirm_plate, confirm_gen = None, -1  # require two consecutive matching reads
    while True:
        await asyncio.sleep(interval)
        pipe = get_pipeline()
        if pipe is None or not pipe._running or pipe.track_description != description:  # noqa: SLF001
            break
        if not pipe.has_lock:
            continue
        # Already identified this lock generation → nothing to do.
        gen = pipe.lock_gen()
        if pipe.vehicle_info is not None:
            continue
        jpeg = pipe.crop_target_jpeg()
        if jpeg is None:
            continue
        plate = await grounding.read_plate(jpeg)
        if not plate:
            continue  # not legible yet — retry next interval
        # Misread guard: only commit a plate that two consecutive reads agree on
        # (per lock generation), so a single bad OCR can't stick to the target.
        if gen != confirm_gen:
            confirm_gen, confirm_plate = gen, None
        if plate != confirm_plate:
            confirm_plate = plate
            continue  # first sighting — wait for a confirming read
        info = await vehicle_lookup.lookup(plate)
        if not info.get("valid"):
            confirm_plate = None
            continue  # not a plausible Indian plate — keep trying
        # Attach to the current lock (ignored if the target changed meanwhile).
        if pipe.set_vehicle_info(info, gen):
            hub.publish_threadsafe({"type": "vehicle_id", "plate": info["plate"], "info": info})
            log.info("vehicle identified: %s (%s)", info["plate"], info.get("state"))


async def _map_objects_loop() -> None:
    """Geolocate detected objects onto the ground and publish a `map_objects`
    event at ~3-4 Hz. Reads CACHED tracks + the active vehicle's snapshot, so it
    never touches/blocks the capture thread (which owns YOLO + the frame grab).

    For each detection whose label maps to one of our MAP_OBJECT_CLASSES, we
    project the box BOTTOM-CENTER (ground-contact point) onto a flat ground plane
    at the drone's launch elevation using lat/lon/alt_rel/heading + the camera
    HFOV + the (assumed) mount pitch folded with the live drone pitch. The locked
    follow target is flagged tracked=true and is republished every cycle so the
    map can move it in real time. Skips everything when there's no GPS/altitude."""
    hz = settings.map_objects_hz if settings.map_objects_hz > 0 else 3.5
    interval = 1.0 / hz
    log.info("map-objects geolocation loop started (%.1f Hz)", hz)
    try:
        while True:
            await asyncio.sleep(interval)
            pipe = get_pipeline()
            if pipe is None or not pipe._running:  # noqa: SLF001
                break

            # Active vehicle pose. No registry / GPS / altitude → emit nothing
            # (the frontend TTL fades any stale objects on its own).
            try:
                link = get_link()
            except Exception:
                continue
            s = link.snapshot()
            lat, lon, alt_rel = s.get("lat"), s.get("lon"), s.get("alt_rel")
            if lat is None or lon is None or alt_rel is None or alt_rel < 1:
                continue
            heading = s.get("heading") or 0.0
            # ATTITUDE pitch is radians, +nose-up; our projection wants +nose-down.
            pitch_rad = s.get("pitch")
            drone_pitch_deg = -math.degrees(pitch_rad) if pitch_rad is not None else 0.0
            vid = registry.active_id()

            # The locked target's box (CSRT lock) identifies the tracked object so
            # we can flag it. Match it to a track by center distance if it's a YOLO
            # track, else publish it as its own tracked entry.
            tgt = pipe.target_box()

            objects: list[dict] = []
            seen_tracked = False
            for t in list(pipe.tracks):
                cls = MAP_OBJECT_CLASSES.get(str(t.get("label", "")).lower())
                if cls is None:
                    continue
                geo = geolocate_box(
                    t, lat, lon, alt_rel, heading,
                    hfov_deg=settings.camera_hfov_deg,
                    cam_pitch_deg=settings.camera_pitch_deg,
                    drone_pitch_deg=drone_pitch_deg,
                )
                if geo is None:
                    continue
                tlat, tlon = geo
                is_tracked = bool(tgt is not None and t.get("id") == pipe.selected_id)
                if is_tracked:
                    seen_tracked = True
                objects.append({
                    "id": int(t["id"]),
                    "label": cls,
                    "lat": round(tlat, 7),
                    "lon": round(tlon, 7),
                    "conf": round(float(t.get("conf", 0.0)), 3),
                    "tracked": is_tracked,
                })

            # A CSRT lock (id == -1) isn't in `tracks`; geolocate it directly so
            # the followed target always pins to the map even when YOLO can't see
            # it (the aerial case the lock keeper exists for).
            if tgt is not None and not seen_tracked:
                geo = geolocate_box(
                    tgt, lat, lon, alt_rel, heading,
                    hfov_deg=settings.camera_hfov_deg,
                    cam_pitch_deg=settings.camera_pitch_deg,
                    drone_pitch_deg=drone_pitch_deg,
                )
                if geo is not None:
                    tlat, tlon = geo
                    # A VLM/voice lock's label is the free-text description, not a
                    # class — classify it (so a tracked person renders as `person`,
                    # not the "car" fallback). A YOLO-track lock maps directly.
                    cls = _classify_lock(str(tgt.get("label", "")))
                    objects.append({
                        "id": int(tgt.get("id", -1)),
                        "label": cls,
                        "lat": round(tlat, 7),
                        "lon": round(tlon, 7),
                        "conf": round(float(tgt.get("conf", 1.0)), 3),
                        "tracked": True,
                    })

            await hub.publish({"type": "map_objects", "vehicle": vid, "objects": objects})
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("map-objects loop crashed")
    finally:
        log.info("map-objects geolocation loop stopped")


async def _lock_keeper(description: str, backend: str) -> None:
    """Keep the lock true: periodically re-anchor it with the VLM (corrects CSRT
    drift on aerial footage) and re-acquire immediately whenever it's lost."""
    log.info("lock keeper started for '%s' (%s)", description, backend)
    interval = settings.reanchor_s if settings.reanchor_s > 0 else 0.7
    while True:
        await asyncio.sleep(interval)
        pipe = get_pipeline()
        if pipe is None or not pipe._running or pipe.track_description != description:  # noqa: SLF001
            break
        # When periodic re-anchor is off, only act on loss.
        if settings.reanchor_s <= 0 and pipe.has_lock:
            continue
        jpeg = pipe.get_jpeg()
        if jpeg is None:
            continue
        box = await grounding.resolve_target(jpeg, description, backend)
        if box:
            pipe.seed_tracker(box, label=description[:18])


@router.post("/start")
async def start(req: StartReq):
    source = req.source or settings.video_source or settings.rtsp_url
    if not source:
        raise HTTPException(400, "no video source (set VIDEO_SOURCE or RTSP_URL)")
    pipe = init_pipeline(source, settings.yolo_model)
    pipe.on_setpoint = _on_setpoint
    pipe.start()
    # Geolocate detected objects onto the ground map at ~3-4 Hz, off cached
    # tracks (never blocks the capture thread). One loop per pipeline run.
    start_map_objects_task()
    return {"ok": True, "source": source}


@router.post("/stop")
async def stop():
    global _keeper_task, _plate_task, _map_objects_task
    for t in (_keeper_task, _plate_task, _map_objects_task):
        if t and not t.done():
            t.cancel()
    _keeper_task = _plate_task = _map_objects_task = None
    pipe = get_pipeline()
    if pipe:
        pipe.stop()
    return {"ok": True}


@router.get("/status")
async def status():
    pipe = get_pipeline()
    if not pipe:
        return {"running": False}
    return {
        "running": pipe._running,  # noqa: SLF001
        "fps": round(pipe.fps, 1),
        "tracks": len(pipe.tracks),
        "selected": pipe.selected_id,
        "follow": pipe.follow_engaged,
        "frame": [pipe.frame_w, pipe.frame_h],
        "vehicle_info": pipe.vehicle_info,
    }


@router.post("/select")
async def select(req: SelectReq):
    pipe = get_pipeline()
    if not pipe:
        raise HTTPException(409, "vision not running")
    pipe.select(req.track_id)
    return {"ok": True, "selected": req.track_id}


@router.post("/follow")
async def follow(req: FollowReq):
    pipe = get_pipeline()
    if not pipe:
        raise HTTPException(409, "vision not running")
    # H1: a vision follow streams GCS-side OFFBOARD setpoints. Refuse it for a
    # vehicle that can't take GCS OFFBOARD (a DDS-bridge vehicle like Outrider)
    # rather than engage start_offboard → DO_SET_MODE OFFBOARD with no setpoint
    # stream and strand it. The command is never sent. Outrider's follow is closed
    # ONBOARD (see /api/outrider/follow + voice 'follow' onboard path).
    link = _follow_link()
    if req.enable:
        vid = registry.link_to_id(link)
        if not registry.supports_offboard(vid):
            name = registry.get(vid).name if vid else "this vehicle"
            raise HTTPException(
                422,
                f"{name} does not support GCS-side OFFBOARD follow — command refused "
                f"(nothing sent). Its follow is closed onboard.",
            )
        # Ready-for-Flight gate: enabling follow is flight-authorizing.
        if not safety.is_ready(vid):
            name = registry.get(vid).name if vid else "this vehicle"
            raise HTTPException(
                422,
                {
                    "error": "ready_for_flight_off",
                    "vehicle": vid,
                    "message": (
                        f"Ready-for-Flight is OFF for {name}. Enable it in the "
                        f"status bar before starting vision follow."
                    ),
                },
            )
    pipe.set_follow(req.enable)
    # If a real vehicle is connected, engage offboard so setpoints take effect.
    # Prime offboard on the SAME link the setpoints will be commanded to (the
    # pipeline's explicit follow target, else the active vehicle).
    if req.enable and link.snapshot().get("connected"):
        asyncio.create_task(commands.start_offboard(link))
    return {"ok": True, "follow": req.enable}


@router.post("/acquire")
async def acquire(req: AcquireReq):
    """Acquire a target by description via the vision model (Qwen), then hold it
    with the CSRT visual tracker — robust when the detector can't see it
    (aerial/occluded)."""
    pipe = get_pipeline()
    if not pipe:
        raise HTTPException(409, "vision not running")
    jpeg = pipe.get_jpeg()
    if jpeg is None:
        raise HTTPException(409, "no frame yet")
    backend = req.backend or "qwen"
    box = await grounding.resolve_target(jpeg, req.description, backend)
    if box is None:
        return {"ok": False, "reason": f"'{req.description}' not found"}
    pipe.seed_tracker(box, label=req.description[:18])
    pipe.track_description = req.description
    # (Re)start the keeper that re-acquires via the VLM whenever the lock drops.
    global _keeper_task, _plate_task
    if _keeper_task and not _keeper_task.done():
        _keeper_task.cancel()
    _keeper_task = asyncio.create_task(_lock_keeper(req.description, backend))
    # (Re)start the proactive vehicle-ID plate reader for this target. Runs as a
    # separate task off the cached frames, so it never stalls the capture loop.
    if _plate_task and not _plate_task.done():
        _plate_task.cancel()
    if settings.plate_id_enabled:
        _plate_task = asyncio.create_task(_plate_reader(req.description))
    return {"ok": True, "box": box, "backend": backend}


@router.post("/seed_box")
async def seed_box(req: SeedBoxReq):
    """Seed the CSRT visual tracker DIRECTLY from a manual selection rectangle
    (the operator drag-drew a box on the live feed), bypassing the VLM. Locks
    onto that ROI in the current frame exactly like a successful VLM acquire.

    The box is normalized 0-1 of the frame: (x, y) top-left, (w, h) size."""
    pipe = get_pipeline()
    if not pipe:
        raise HTTPException(409, "vision not running")
    if pipe.get_jpeg() is None:
        raise HTTPException(409, "no frame yet")
    # Clamp to the frame and require a non-degenerate box (CSRT needs > 2px, and
    # the pipeline rejects boxes that are too tiny anyway).
    x = min(max(req.x, 0.0), 1.0)
    y = min(max(req.y, 0.0), 1.0)
    x1 = min(max(req.x + req.w, 0.0), 1.0)
    y1 = min(max(req.y + req.h, 0.0), 1.0)
    if x1 - x < 0.01 or y1 - y < 0.01:
        return {"ok": False, "reason": "selection too small"}
    label = (req.label or "MANUAL")[:18]
    # seed_tracker expects [x0, y0, x1, y1] normalized — same contract the VLM
    # acquire uses. The capture thread inits CSRT on the next frame.
    pipe.seed_tracker([x, y, x1, y1], label=label)
    # A manual seed is NOT description-driven, so kill any running VLM
    # lock-keeper (it would re-anchor via text + stomp our box) and clear the
    # description so a stale keeper exits on its next tick.
    global _keeper_task, _plate_task
    if _keeper_task and not _keeper_task.done():
        _keeper_task.cancel()
    _keeper_task = None
    pipe.track_description = None
    # Still run the proactive vehicle-ID plate reader against this lock, keyed to
    # the manual label (it self-gates on the lock generation, not a description).
    if _plate_task and not _plate_task.done():
        _plate_task.cancel()
    if settings.plate_id_enabled:
        _plate_task = asyncio.create_task(_plate_reader(None))
    return {"ok": True, "box": [x, y, x1, y1], "label": label}


@router.post("/track")
async def track_by_description(req: TrackReq):
    """Acquire a target by text ('the white car') → seed the tracker."""
    pipe = get_pipeline()
    if not pipe:
        raise HTTPException(409, "vision not running")
    jpeg = pipe.get_jpeg()
    if jpeg is None:
        raise HTTPException(409, "no frame yet")
    box = await grounding.resolve_target(
        jpeg, req.description, settings.grounding_backend
    )
    if box is None:
        return {"ok": False, "reason": f"'{req.description}' not found"}
    # Match the grounded box to the nearest live track by centre distance.
    bx, by = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    best, best_d = None, 1e9
    for t in pipe.tracks:
        tx, ty = t["x"] + t["w"] / 2, t["y"] + t["h"] / 2
        d = (tx - bx) ** 2 + (ty - by) ** 2
        if d < best_d:
            best, best_d = t, d
    if best is None:
        return {"ok": False, "reason": "no live tracks to bind to"}
    pipe.select(best["id"])
    return {"ok": True, "selected": best["id"], "label": best["label"], "box": box}


@router.post("/orbit")
async def orbit_target():
    """Orbit the currently selected target (estimates its ground position)."""
    pipe = get_pipeline()
    if not pipe or pipe.selected_box() is None:
        raise HTTPException(409, "no target selected")
    link = get_link()
    # Ready-for-Flight gate.
    vid = registry.link_to_id(link)
    if not safety.is_ready(vid):
        name = registry.get(vid).name if vid else "this vehicle"
        raise HTTPException(
            422,
            {
                "error": "ready_for_flight_off",
                "vehicle": vid,
                "message": (
                    f"Ready-for-Flight is OFF for {name}. Enable it in the status "
                    f"bar before commanding an orbit."
                ),
            },
        )
    s = link.snapshot()
    geo = geolocate_target(
        pipe.selected_box(), s.get("lat"), s.get("lon"), s.get("alt_rel"),
        s.get("heading") or 0.0, settings.camera_hfov_deg, settings.camera_pitch_deg,
    )
    if geo is None:
        raise HTTPException(409, "cannot geolocate target (need GPS + altitude)")
    tlat, tlon = geo
    await commands.orbit(link, tlat, tlon, s.get("alt_rel") or 20, radius=25, velocity=4)
    return {"ok": True, "target": [tlat, tlon]}


@router.get("/stream.mjpg")
async def stream():
    pipe = get_pipeline()
    if not pipe:
        raise HTTPException(409, "vision not running")

    async def gen():
        boundary = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
        while pipe._running:  # noqa: SLF001
            jpeg = pipe.get_jpeg()
            if jpeg is not None:
                yield boundary + jpeg + b"\r\n"
            await asyncio.sleep(1 / 25)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")
