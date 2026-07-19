"""Structured audit log for the GCS — captures EVERYTHING relevant flowing in
and out of the GCS as timestamped, structured JSON lines.

Design goals (strict, important to the operator):
  * Append-only JSON Lines to a ROTATING file (`logs/gcs-YYYYMMDD.jsonl`),
    rotated daily and by size, so the on-disk record is complete and replayable.
  * An in-memory ring buffer of the last N entries powers the query endpoints
    without touching disk.
  * Callable safely from BOTH the asyncio event loop AND the MAVLink reader
    THREAD — every public entrypoint is non-blocking and never raises into the
    caller. A dedicated writer thread drains a queue, so neither the reader
    thread nor the event loop ever blocks on disk I/O.
  * Per-vehicle 1 Hz throttle helper for high-rate telemetry so the log is
    complete but not flooded.

Logging is PURELY ADDITIVE — every entrypoint swallows its own errors (drop on
error) and must never crash a command path, the reader thread, or the loop.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("gcs.logbus")

# Rotate the active file once it grows past this size (bytes).
_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB
# Sentinel pushed onto the writer queue to ask it to stop cleanly.
_STOP = object()


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _jsonable(value: Any) -> Any:
    """Best-effort coercion to a JSON-serialisable value. Never raises."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN/Inf are not valid JSON — drop to None.
        return value if value == value and value not in (float("inf"), float("-inf")) else None
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace")
        except Exception:
            return repr(value)
    try:
        return str(value)
    except Exception:
        return None


class LogBus:
    """Thread-safe, async-safe append-only structured logger.

    Use the module-level singleton `logbus`. Call `init()` once at startup; if
    never initialised, every `log()` is a cheap no-op (so importing this module
    has zero side effects).
    """

    def __init__(self) -> None:
        self._enabled = False
        self._dir: str | None = None
        self._telemetry_hz = 1.0
        self._ring: deque[dict[str, Any]] = deque(maxlen=2000)
        self._ring_lock = threading.Lock()
        self._q: "queue.Queue[Any]" = queue.Queue(maxsize=10000)
        self._writer: threading.Thread | None = None
        self._seq = 0
        self._seq_lock = threading.Lock()
        # Per-(vehicle, key) last-emit timestamps for throttling high-rate logs.
        self._throttle: dict[tuple[str, str], float] = {}
        self._throttle_lock = threading.Lock()
        # Open file handle + current day, owned by the writer thread only.
        self._fh = None
        self._cur_day: str | None = None
        self._cur_path: str | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────
    def init(
        self,
        *,
        enabled: bool = True,
        log_dir: str = "logs",
        telemetry_hz: float = 1.0,
        ring_size: int = 2000,
    ) -> None:
        """Idempotent startup. Spawns the writer thread if enabled."""
        self._enabled = bool(enabled)
        self._telemetry_hz = float(telemetry_hz) if telemetry_hz and telemetry_hz > 0 else 1.0
        with self._ring_lock:
            if ring_size and ring_size != self._ring.maxlen:
                self._ring = deque(self._ring, maxlen=ring_size)
        if not self._enabled:
            log.info("audit log disabled (audit_log_enabled=false)")
            return
        # Resolve the log dir relative to the backend root (this file's parent's
        # parent) so it works regardless of the process CWD.
        if not os.path.isabs(log_dir):
            backend_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_dir = os.path.join(backend_root, log_dir)
        self._dir = log_dir
        try:
            os.makedirs(self._dir, exist_ok=True)
        except Exception:
            log.exception("could not create audit log dir %s", self._dir)
        if self._writer is None or not self._writer.is_alive():
            self._writer = threading.Thread(
                target=self._writer_loop, name="logbus-writer", daemon=True
            )
            self._writer.start()
        log.info("audit log enabled → %s (telemetry %.2f Hz)", self._dir, self._telemetry_hz)

    def stop(self) -> None:
        if self._writer is not None and self._writer.is_alive():
            try:
                self._q.put_nowait(_STOP)
            except Exception:
                pass

    # ── public logging API ────────────────────────────────────────────────
    def log(
        self,
        category: str,
        direction: str | None,
        vehicle: str | None,
        **fields: Any,
    ) -> None:
        """Append one structured entry. Safe from any thread / the loop; never
        raises, never blocks (drops the entry if the queue is full)."""
        if not self._enabled:
            return
        try:
            with self._seq_lock:
                self._seq += 1
                seq = self._seq
            entry: dict[str, Any] = {
                "seq": seq,
                "ts": _utc_iso(),
                "category": category,
                "dir": direction,
                "vehicle": vehicle,
            }
            for k, v in fields.items():
                entry[k] = _jsonable(v)
            # Ring buffer first (cheap, in-memory) so queries always see it even
            # if the disk writer is backed up.
            with self._ring_lock:
                self._ring.append(entry)
            try:
                self._q.put_nowait(entry)
            except queue.Full:
                # Disk is backed up — keep the in-memory record, drop the write.
                pass
        except Exception:
            # Logging must never break the caller.
            pass

    def log_telemetry(self, vehicle: str | None, snapshot: dict[str, Any]) -> None:
        """Throttled (per-vehicle, ~telemetry_hz) compact telemetry snapshot.

        High-rate telemetry must NOT flood the log, but the record should still
        be complete — so we sample at telemetry_hz per vehicle.
        """
        if not self._enabled:
            return
        if not self._allow(vehicle or "?", "telemetry", self._telemetry_hz):
            return
        try:
            s = snapshot or {}
            self.log(
                "telemetry",
                "in",
                vehicle,
                kind="telemetry",
                connected=s.get("connected"),
                armed=s.get("armed"),
                mode=s.get("mode"),
                lat=s.get("lat"),
                lon=s.get("lon"),
                alt_rel=s.get("alt_rel"),
                alt_msl=s.get("alt_msl"),
                heading=s.get("heading"),
                groundspeed=s.get("groundspeed"),
                battery_pct=s.get("battery_pct"),
                battery_voltage=s.get("battery_voltage"),
                gps_fix=s.get("gps_fix"),
                satellites=s.get("satellites"),
            )
        except Exception:
            pass

    def allow(self, vehicle: str, key: str, hz: float | None = None) -> bool:
        """Public per-(vehicle,key) rate-limit gate. Returns True at most `hz`
        times per second; subsequent calls within the window return False."""
        return self._allow(vehicle, key, hz if hz is not None else self._telemetry_hz)

    # ── query API (ring buffer) ───────────────────────────────────────────
    def recent(
        self,
        *,
        limit: int = 200,
        since: int | None = None,
        category: str | None = None,
        vehicle: str | None = None,
        direction: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent ring-buffer entries, oldest→newest, with filters.

        `since` filters to entries with `seq > since` (monotonic cursor for
        incremental polling).
        """
        with self._ring_lock:
            items = list(self._ring)
        out: list[dict[str, Any]] = []
        for e in items:
            if since is not None and e.get("seq", 0) <= since:
                continue
            if category is not None and e.get("category") != category:
                continue
            if vehicle is not None and e.get("vehicle") != vehicle:
                continue
            if direction is not None and e.get("dir") != direction:
                continue
            out.append(e)
        if limit and limit > 0:
            out = out[-limit:]
        return out

    def last_seq(self) -> int:
        with self._seq_lock:
            return self._seq

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def current_path(self) -> str | None:
        return self._cur_path

    # ── internals ─────────────────────────────────────────────────────────
    def _allow(self, vehicle: str, key: str, hz: float) -> bool:
        if hz <= 0:
            return True
        period = 1.0 / hz
        now = time.monotonic()
        k = (vehicle, key)
        with self._throttle_lock:
            last = self._throttle.get(k, 0.0)
            if now - last >= period:
                self._throttle[k] = now
                return True
        return False

    def _target_path(self, day: str) -> str:
        assert self._dir is not None
        return os.path.join(self._dir, f"gcs-{day}.jsonl")

    def _rotate_for_size(self) -> None:
        """If the active file exceeds _MAX_BYTES, roll it to a numbered suffix."""
        if self._fh is None or self._cur_path is None:
            return
        try:
            if os.path.getsize(self._cur_path) < _MAX_BYTES:
                return
        except OSError:
            return
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None
        # Find the next free numbered sibling: gcs-YYYYMMDD.N.jsonl
        base = self._cur_path[: -len(".jsonl")]
        n = 1
        while os.path.exists(f"{base}.{n}.jsonl"):
            n += 1
        try:
            os.rename(self._cur_path, f"{base}.{n}.jsonl")
        except Exception:
            log.exception("size rotation rename failed")

    def _ensure_file(self) -> None:
        """Open / re-open the active file on day change or after rotation."""
        if self._dir is None:
            return
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        self._rotate_for_size()
        if self._fh is not None and self._cur_day == day:
            return
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        self._cur_day = day
        self._cur_path = self._target_path(day)
        try:
            self._fh = open(self._cur_path, "a", encoding="utf-8")
        except Exception:
            self._fh = None
            log.exception("could not open audit log file %s", self._cur_path)

    def _writer_loop(self) -> None:
        while True:
            try:
                item = self._q.get()
            except Exception:
                continue
            if item is _STOP:
                break
            try:
                self._ensure_file()
                if self._fh is not None:
                    self._fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                    self._fh.flush()
            except Exception:
                # Drop on error — never let disk problems propagate.
                pass
        try:
            if self._fh is not None:
                self._fh.flush()
                self._fh.close()
        except Exception:
            pass


# Module-level singleton.
logbus = LogBus()
