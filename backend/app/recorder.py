"""Local MP4 recording of go2rtc restreams.

Captures a go2rtc stream (RTSP at rtsp://127.0.0.1:8554/<stream>) to an MP4 on
the Mac via an ffmpeg subprocess — one per stream. The stream is already a
browser-safe H.264 restream, so we COPY the bitstream (-c copy): no re-encode,
near-zero CPU, frame-accurate to what go2rtc emits.

CLEAN STOP is the whole game for a playable file: MP4 needs its moov atom
written on close. We send ffmpeg `q` on stdin (graceful), fall back to SIGINT,
and WAIT for it to finalize — we NEVER SIGKILL, which would truncate the file
and leave it unplayable (no moov atom).
"""
from __future__ import annotations

import logging
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("gcs.recorder")

# <repo>/recordings  (recorder.py → app → backend → <repo>)
RECORDINGS_DIR = Path(__file__).resolve().parents[2] / "recordings"

RTSP_BASE = "rtsp://127.0.0.1:8554"

# Seconds to wait for ffmpeg to flush + write the moov atom after a graceful
# stop request before escalating (q → SIGINT → SIGINT again). We never SIGKILL.
_FINALIZE_TIMEOUT = 10.0


class _Recording:
    __slots__ = ("proc", "path", "since")

    def __init__(self, proc: subprocess.Popen, path: Path, since: float) -> None:
        self.proc = proc
        self.path = path
        self.since = since


# stream name → active recording
_active: dict[str, _Recording] = {}


def _ffmpeg_cmd(stream: str, out_path: Path) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-rtsp_transport", "tcp",
        "-i", f"{RTSP_BASE}/{stream}",
        "-an",
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]


def is_recording(stream: str) -> bool:
    rec = _active.get(stream)
    return rec is not None and rec.proc.poll() is None


def start(stream: str) -> dict:
    """Start recording <stream> to a timestamped MP4. Guards double-start."""
    # Reap a process that died on its own (e.g. RTSP source vanished).
    rec = _active.get(stream)
    if rec is not None and rec.proc.poll() is not None:
        _active.pop(stream, None)
        rec = None
    if rec is not None:
        return {"ok": True, "file": str(rec.path), "already": True}

    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = RECORDINGS_DIR / f"{stream}_{stamp}.mp4"

    cmd = _ffmpeg_cmd(stream, out_path)
    log.info("recorder start: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,   # so we can send 'q' for a clean stop
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return {"ok": False, "error": "ffmpeg not found on PATH"}

    _active[stream] = _Recording(proc, out_path, time.time())
    return {"ok": True, "file": str(out_path)}


def stop(stream: str) -> dict:
    """Stop recording <stream> CLEANLY so the MP4 is playable. No-op if not
    recording. Sends 'q' on stdin, escalates to SIGINT, and WAITS for ffmpeg to
    finalize the moov atom — never SIGKILL."""
    rec = _active.pop(stream, None)
    if rec is None:
        return {"ok": True, "recording": False}

    proc = rec.proc
    if proc.poll() is not None:
        # Already exited (crashed / source dropped). Nothing to finalize.
        return {"ok": True, "file": str(rec.path), "recording": False}

    # 1) Graceful: ffmpeg quits and writes the moov atom when it reads 'q'.
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.write(b"q")
            proc.stdin.flush()
            proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass

    try:
        proc.wait(timeout=_FINALIZE_TIMEOUT)
    except subprocess.TimeoutExpired:
        # 2) Escalate to SIGINT — ffmpeg also finalizes on this.
        log.warning("recorder %s: 'q' timed out, sending SIGINT", stream)
        try:
            proc.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=_FINALIZE_TIMEOUT)
        except subprocess.TimeoutExpired:
            # 3) One more SIGINT and a final wait. We deliberately do NOT
            #    SIGKILL — that truncates the MP4 (missing moov = unplayable).
            log.error("recorder %s: SIGINT timed out, retrying SIGINT", stream)
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=_FINALIZE_TIMEOUT)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                log.error("recorder %s: ffmpeg did not exit; file may be incomplete", stream)

    log.info("recorder stop: %s (rc=%s)", rec.path, proc.poll())
    return {"ok": True, "file": str(rec.path), "recording": False}


def status() -> dict:
    """Current recordings: {<stream>: {file, since_unix}} for live processes."""
    out: dict[str, dict] = {}
    for stream, rec in list(_active.items()):
        if rec.proc.poll() is not None:
            _active.pop(stream, None)  # reap dead process
            continue
        out[stream] = {"file": str(rec.path), "since_unix": rec.since}
    return {"recording": out}


def stop_all() -> None:
    """Clean-stop every active recording (shutdown hook). Best-effort."""
    for stream in list(_active.keys()):
        try:
            stop(stream)
        except Exception:  # noqa: BLE001
            log.exception("recorder stop_all failed for %s", stream)
