from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import cv2
from ultralytics import YOLO

from ..ws.hub import hub
from .follow import FollowController, Setpoint, setpoint_dict

log = logging.getLogger("gcs.vision")

ACCENT = (196, 227, 34)  # cyan-ish (BGR)
BLUE = (255, 200, 120)
RED = (94, 77, 255)


class VisionPipeline:
    """Reads a video source, runs YOLO + ByteTrack, publishes tracks, and (when
    engaged) drives a follow controller. Annotated frames are exposed as JPEG
    for the MJPEG endpoint."""

    def __init__(
        self, source: str, model_path: str = "yolo11n.pt", device: str = "mps", imgsz: int = 640
    ) -> None:
        self.source = source
        self.model = YOLO(model_path)
        self.device = device
        self.imgsz = imgsz

        self._running = False
        self._thread: threading.Thread | None = None
        self._jpeg_lock = threading.Lock()
        self._jpeg: bytes | None = None
        # Latest raw (un-annotated) frame, for cropping the target to read its
        # plate without the overlay/box drawing baked in.
        self._last_raw_frame = None

        self.tracks: list[dict] = []
        self.frame_w = 0
        self.frame_h = 0
        self.fps = 0.0
        self.selected_id: int | None = None
        self.follow_engaged = False
        self.follow = FollowController()
        self._last_follow_pub = 0.0
        # Wired by the API to push offboard setpoints to MAVLink.
        self.on_setpoint: Callable[[Setpoint, dict], None] | None = None
        # The vehicle whose camera owns this pipeline / whose link the follow
        # setpoints must be commanded to. None ⇒ caller falls back to active.
        self.target_vehicle_id: str | None = None
        # Lost-lock watchdog: timestamp of the last frame we had a real lock
        # while follow was engaged. The follow loop uses it to disengage follow
        # if the target is lost longer than `lost_lock_timeout_s`.
        self._last_lock_ts: float = 0.0
        self.lost_lock_timeout_s: float = 8.0

        # CSRT "lock" — a VLM/click-seeded visual tracker that holds a target
        # frame-to-frame even when the detector can't see it (aerial/occlusion).
        self._pending_seed: list[float] | None = None  # [x0,y0,x1,y1] normalized
        self._csrt = None
        self._csrt_box: dict | None = None
        self.target_label = "TARGET"
        self.track_description: str | None = None  # for VLM re-acquisition on loss

        # Proactive vehicle-ID: anonymized record for the currently locked
        # target, keyed to the lock "generation" so it resets when the target
        # changes or the lock is lost. Written by the async plate-read loop,
        # read (only) by the capture thread when drawing the overlay.
        self._lock_gen = 0
        self.vehicle_info: dict | None = None
        self._vehicle_info_gen = -1
        # Label of the target the current vehicle-ID generation belongs to. The
        # lock-keeper re-seeds the SAME target every reanchor_s to correct drift;
        # we only reset vehicle-ID when this label actually changes.
        self._vehicle_info_label: str | None = None

    @property
    def has_lock(self) -> bool:
        return self._csrt_box is not None

    # ── control surface ──────────────────────────────────────────────────
    def select(self, track_id: int | None) -> None:
        self.selected_id = track_id

    def set_follow(self, on: bool) -> None:
        self.follow_engaged = on
        # Reset the lost-lock watchdog clock on (re)engage so a stale prior-loss
        # timestamp can't immediately trip the timeout.
        if on:
            self._last_lock_ts = time.time()

    def _follow_watchdog(self, has_target: bool, now: float) -> bool:
        """Lost-lock watchdog (H4). While follow is engaged, remember when we
        last had a real target; if the lock has been gone longer than
        ``lost_lock_timeout_s``, DISENGAGE follow (and notify) instead of holding
        a zero-velocity setpoint forever on a target that's never coming back.

        Returns True when it disengaged follow this call. Pure decision logic so
        it's unit-testable without the capture thread."""
        if not self.follow_engaged:
            return False
        if has_target:
            self._last_lock_ts = now
            return False
        if self._last_lock_ts and (now - self._last_lock_ts) > self.lost_lock_timeout_s:
            self.follow_engaged = False
            log.warning(
                "follow disengaged: target '%s' lost for >%.0fs",
                self.target_label, self.lost_lock_timeout_s,
            )
            hub.publish_threadsafe({
                "type": "follow_lost",
                "label": self.target_label,
                "timeout_s": self.lost_lock_timeout_s,
            })
            return True
        return False

    def seed_tracker(self, box_norm: list[float], label: str = "TARGET") -> None:
        """Lock a CSRT visual tracker onto [x0,y0,x1,y1] (normalized 0-1)."""
        self._pending_seed = box_norm
        self.target_label = label
        self.selected_id = -1  # marks the CSRT lock as the active target

    def clear_lock(self) -> None:
        self._csrt = None
        self._csrt_box = None
        self._pending_seed = None
        self._reset_vehicle_info()
        self._vehicle_info_label = None

    def _reset_vehicle_info(self) -> None:
        """Drop any vehicle-ID record (target changed / lock lost) and advance
        the lock generation so a stale in-flight plate read is ignored."""
        self.vehicle_info = None
        self._vehicle_info_gen = -1
        self._lock_gen += 1

    def lock_gen(self) -> int:
        """Monotonic id of the current lock; changes on every new/lost lock."""
        return self._lock_gen

    def set_vehicle_info(self, info: dict | None, gen: int) -> bool:
        """Attach an anonymized vehicle record to the current lock. Ignored if
        the lock changed since the read started (``gen`` mismatch). Returns True
        if applied. Called from the async plate-read loop, not the capture loop."""
        if gen != self._lock_gen or self._csrt_box is None:
            return False
        self.vehicle_info = info
        self._vehicle_info_gen = gen
        return True

    def get_jpeg(self) -> bytes | None:
        with self._jpeg_lock:
            return self._jpeg

    def crop_target_jpeg(self, pad: float = 0.12, upscale: int = 2) -> bytes | None:
        """Crop the current locked target from the latest frame, upscale it, and
        return a JPEG — for plate reading. Returns None if there's no lock /
        frame. Reads the cached annotated-source via a fresh decode is avoided;
        instead we keep a copy of the last raw frame for cropping."""
        box = self._csrt_box
        frame = self._last_raw_frame
        if box is None or frame is None:
            return None
        import cv2 as _cv2  # local alias; module already imports cv2

        h, w = frame.shape[:2]
        px = box["w"] * pad
        py = box["h"] * pad
        x0 = max(0.0, box["x"] - px)
        y0 = max(0.0, box["y"] - py)
        x1 = min(1.0, box["x"] + box["w"] + px)
        y1 = min(1.0, box["y"] + box["h"] + py)
        ix0, iy0 = int(x0 * w), int(y0 * h)
        ix1, iy1 = int(x1 * w), int(y1 * h)
        if ix1 - ix0 < 4 or iy1 - iy0 < 4:
            return None
        crop = frame[iy0:iy1, ix0:ix1]
        if upscale > 1:
            crop = _cv2.resize(
                crop, (crop.shape[1] * upscale, crop.shape[0] * upscale),
                interpolation=_cv2.INTER_CUBIC,
            )
        ok, buf = _cv2.imencode(".jpg", crop, [_cv2.IMWRITE_JPEG_QUALITY, 92])
        return buf.tobytes() if ok else None

    def _draw_vehicle_card(self, frame, info: dict, box_x1: int, box_y0: int) -> None:
        """Render a compact anonymized vehicle-ID card next to the box.

        Anchored to the top-right corner of the bounding box; flips to the left
        side if it would run off-frame. Semi-transparent dark panel, cyan accent
        title, small multi-line text. Owner is already masked upstream."""
        plate = info.get("plate") or "—"
        state = info.get("state") or "?"
        rto = info.get("rto_code") or ""
        loc = f"{state}" + (f" - {rto}" if rto else "")
        lines = [
            (plate, ACCENT, 0.6, 2),
            (loc, (220, 220, 220), 0.45, 1),
            (info.get("maker_model") or "unknown model", (255, 255, 255), 0.45, 1),
        ]
        meta = " / ".join(
            v for v in (info.get("vehicle_class"), info.get("fuel"), info.get("reg_year")) if v
        )
        if meta:
            lines.append((meta, (200, 200, 200), 0.42, 1))
        owner = info.get("owner")
        if owner:
            lines.append((f"Owner: {owner}", (160, 220, 255), 0.42, 1))
        if info.get("source") == "mock":
            lines.append(("[mock - anonymized]", (140, 140, 140), 0.38, 1))

        pad = 8
        line_h = 22
        # Measure width.
        wmax = 0
        for text, _c, scale, thick in lines:
            (tw, _th), _b = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, thick)
            wmax = max(wmax, tw)
        cw = wmax + pad * 2
        ch = line_h * len(lines) + pad
        # Anchor top-right of the box; flip left if it overflows the frame.
        cx0 = box_x1 + 10
        cy0 = max(0, box_y0)
        if cx0 + cw > self.frame_w:
            cx0 = max(0, box_x1 - cw - 10)
        cy1 = min(self.frame_h, cy0 + ch)
        cx1 = min(self.frame_w, cx0 + cw)

        # Semi-transparent background panel.
        overlay = frame.copy()
        cv2.rectangle(overlay, (cx0, cy0), (cx1, cy1), (20, 20, 20), -1)
        cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)
        cv2.rectangle(frame, (cx0, cy0), (cx1, cy1), ACCENT, 1)

        ty = cy0 + pad + 12
        for text, color, scale, thick in lines:
            cv2.putText(frame, text, (cx0 + pad, ty),
                        cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)
            ty += line_h

    def selected_box(self) -> dict | None:
        return next((t for t in self.tracks if t["id"] == self.selected_id), None)

    def target_box(self) -> dict | None:
        """Active follow target: the CSRT lock if present, else a selected track."""
        return self._csrt_box if self._csrt_box is not None else self.selected_box()

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="vision", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _run(self) -> None:  # noqa: C901
        cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            log.error("cannot open video source: %s", self.source)
            self._running = False
            hub.publish_threadsafe({"type": "vision", "status": "error", "source": self.source})
            return
        is_file = not str(self.source).lower().startswith(("rtsp", "udp", "http"))
        src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        log.info("vision pipeline up: %s (%.0f fps, file=%s)", self.source, src_fps, is_file)
        hub.publish_threadsafe({"type": "vision", "status": "running"})

        last_emit = 0.0
        t_prev = time.time()
        while self._running:
            t_loop = time.time()
            ret, frame = cap.read()
            if not ret:
                if is_file:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                time.sleep(0.5)
                cap.open(self.source)
                continue

            self.frame_h, self.frame_w = frame.shape[:2]
            # Keep a clean copy for plate-cropping before we draw on `frame`.
            self._last_raw_frame = frame.copy()
            res = self.model.track(
                frame, persist=True, tracker="bytetrack.yaml",
                imgsz=self.imgsz, device=self.device, verbose=False,
            )[0]

            tracks: list[dict] = []
            if res.boxes is not None and res.boxes.id is not None:
                ids = res.boxes.id.cpu().numpy().astype(int)
                xyxy = res.boxes.xyxy.cpu().numpy()
                clss = res.boxes.cls.cpu().numpy().astype(int)
                confs = res.boxes.conf.cpu().numpy()
                for i in range(len(ids)):
                    x0, y0, x1, y1 = xyxy[i]
                    tid = int(ids[i])
                    label = self.model.names[int(clss[i])]
                    conf = float(confs[i])
                    tracks.append({
                        "id": tid, "label": label, "conf": conf,
                        "x": float(x0 / self.frame_w), "y": float(y0 / self.frame_h),
                        "w": float((x1 - x0) / self.frame_w), "h": float((y1 - y0) / self.frame_h),
                    })
                    sel = tid == self.selected_id
                    color = ACCENT if sel else BLUE
                    cv2.rectangle(frame, (int(x0), int(y0)), (int(x1), int(y1)), color, 3 if sel else 2)
                    cv2.putText(frame, f"{label} #{tid} {int(conf * 100)}%", (int(x0), int(y0) - 6),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
            self.tracks = tracks

            # ── CSRT lock: seed / update / draw ──────────────────────────
            if self._pending_seed is not None:
                x0, y0, x1, y1 = self._pending_seed
                bx, by = int(x0 * self.frame_w), int(y0 * self.frame_h)
                bw, bh = int((x1 - x0) * self.frame_w), int((y1 - y0) * self.frame_h)
                if bw > 2 and bh > 2:
                    self._csrt = cv2.TrackerCSRT_create()
                    self._csrt.init(frame, (bx, by, bw, bh))
                    self._csrt_box = {"id": -1, "label": self.target_label, "conf": 1.0,
                                      "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}
                    # A genuinely NEW target (label changed, or there was no lock)
                    # invalidates any prior vehicle-ID. Re-anchors of the SAME
                    # target (same label, lock-keeper drift correction) keep it.
                    if self._vehicle_info_label != self.target_label:
                        self._reset_vehicle_info()
                        self._vehicle_info_label = self.target_label
                    log.info("CSRT lock acquired: %s", self.target_label)
                self._pending_seed = None
            elif self._csrt is not None:
                ok, bb = self._csrt.update(frame)
                x, y, w, h = bb
                wn, hn = w / self.frame_w, h / self.frame_h
                cxn, cyn = (x + w / 2) / self.frame_w, (y + h / 2) / self.frame_h
                # CSRT can "succeed" with a degenerate/edge box when it loses the
                # target (e.g. under tree canopy). Reject those and declare loss.
                valid = ok and 0.01 < wn < 0.9 and 0.01 < hn < 0.9 \
                    and 0.02 < cxn < 0.98 and 0.02 < cyn < 0.98
                if valid:
                    self._csrt_box = {"id": -1, "label": self.target_label, "conf": 1.0,
                                      "x": x / self.frame_w, "y": y / self.frame_h,
                                      "w": wn, "h": hn}
                else:
                    self._csrt = None
                    self._csrt_box = None
                    self._reset_vehicle_info()
                    self._vehicle_info_label = None
                    hub.publish_threadsafe({"type": "target_lost", "label": self.target_label})

            if self._csrt_box is not None:
                b = self._csrt_box
                x0 = int(b["x"] * self.frame_w); y0 = int(b["y"] * self.frame_h)
                x1 = int((b["x"] + b["w"]) * self.frame_w); y1 = int((b["y"] + b["h"]) * self.frame_h)
                cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
                cv2.rectangle(frame, (x0, y0), (x1, y1), ACCENT, 3)
                cv2.drawMarker(frame, (cx, cy), ACCENT, cv2.MARKER_CROSS, 18, 2)
                cv2.putText(frame, f"LOCK: {b['label']}", (x0, y0 - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, ACCENT, 2)
                # Anonymized vehicle-ID card, next to the box (top-right).
                if self.vehicle_info:
                    self._draw_vehicle_card(frame, self.vehicle_info, x1, y0)

            tgt_box = self.target_box()
            if self._follow_watchdog(tgt_box is not None, time.time()):
                cv2.putText(
                    frame, "FOLLOW LOST — disengaged",
                    (12, self.frame_h - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.6, RED, 2,
                )

            if self.follow_engaged:
                # Keep streaming setpoints even when the lock is momentarily lost
                # (target under canopy etc.) — a zero-velocity hold keeps PX4
                # offboard alive instead of starving it into an RTL failsafe loop.
                sp = self.follow.compute(tgt_box) if tgt_box is not None else Setpoint(0.0, 0.0, 0.0, 0.0)
                tag = "FOLLOW" if tgt_box is not None else "FOLLOW (no lock — holding)"
                cv2.putText(
                    frame,
                    f"{tag}  vx={sp.vx:+.1f} vy={sp.vy:+.1f} vz={sp.vz:+.1f} yaw={sp.yaw_rate:+.2f}",
                    (12, self.frame_h - 16), cv2.FONT_HERSHEY_SIMPLEX, 0.6, ACCENT, 2,
                )
                if self.on_setpoint:
                    try:
                        self.on_setpoint(sp, tgt_box)
                    except Exception:
                        log.exception("setpoint callback failed")
                # Throttle the WS broadcast to ~4 Hz — publishing every frame
                # floods the asyncio loop and stalls the realtime voice socket.
                tnow = time.time()
                if tnow - self._last_follow_pub > 0.25:
                    self._last_follow_pub = tnow
                    hub.publish_threadsafe(
                        {"type": "follow", "setpoint": setpoint_dict(sp), "target": self.selected_id}
                    )

            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if ok:
                with self._jpeg_lock:
                    self._jpeg = buf.tobytes()

            now = time.time()
            if now - last_emit > 0.08:  # ~12 Hz
                hub.publish_threadsafe(
                    {"type": "tracks", "tracks": tracks, "frame_w": self.frame_w, "frame_h": self.frame_h}
                )
                last_emit = now

            dt = now - t_prev
            t_prev = now
            if dt > 0:
                self.fps = 0.9 * self.fps + 0.1 * (1.0 / dt)
            if is_file:  # pace file playback to the source frame rate
                time.sleep(max(0.0, 1.0 / src_fps - (time.time() - t_loop)))

        cap.release()
        hub.publish_threadsafe({"type": "vision", "status": "stopped"})
        log.info("vision pipeline stopped")


pipeline: VisionPipeline | None = None


def get_pipeline() -> VisionPipeline | None:
    return pipeline


def init_pipeline(source: str, model_path: str, device: str = "mps") -> VisionPipeline:
    global pipeline
    if pipeline is not None:
        pipeline.stop()
    pipeline = VisionPipeline(source, model_path=model_path, device=device)
    return pipeline
