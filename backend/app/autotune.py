"""PX4 multicopter AUTOTUNE — vehicle-agnostic controller.

Autotune is an IN-FLIGHT axis-oscillation maneuver: PX4 excites roll/pitch/yaw,
identifies the rate-controller plant, and computes new PID gains. It is triggered
by a single MAVLink command and reports progress in a NON-STANDARD way that this
controller drives.

MECHANISM (build to this — see reviews/autotune-param-stadonet-plan.md §8):
  * Trigger: MAV_CMD_DO_AUTOTUNE_ENABLE (command id 212) as a COMMAND_LONG with
    param1=1.0 (enable), param2=0.0 (all axes), params 3-7=0.0.
  * Progress: PX4 does NOT stream progress. The GCS must RE-SEND cmd 212 at ~1 Hz
    to poll; PX4 answers each with COMMAND_ACK result=MAV_RESULT_IN_PROGRESS(5)
    and a `progress` byte (0-100) while running, then result=MAV_RESULT_ACCEPTED(0)
    when complete (or a failure result).
  * STATUSTEXT: PX4 also emits "Autotune: roll/pitch/yaw" / "Autotune: done" lines
    DURING the run. On Overwatch (classic MAVLink) these arrive; on Outrider
    (DDS→MAVLink bridge) STATUSTEXT is NOT a DDS topic and likely never arrives.
    So this controller treats STATUSTEXT as SUPPLEMENTARY ONLY — progress is
    driven by the ACK alone, and the feature degrades gracefully without it.
  * Cancel: re-send cmd 212 with param1=0.0 (disable).
  * Gains auto-apply on disarm when MC_AT_APPLY=1 (PX4 default). No param read/write
    is needed for the basic flow.

TRANSPORT — WORKS ON CLASSIC MAVLINK ONLY (Overwatch). ⚠ It does NOT work on
Outrider over the DDS bridge: cmd 212 is relayed to PX4's vehicle_command topic,
but PX4's Commander has NO handler for it and returns MAV_RESULT_UNSUPPORTED — so
autotune never starts (verified in reviews/autotune-over-dds-verdict.md; the real
trigger is mavlink_receiver-only, which sets MC_AT_START). The GCS therefore
REFUSES autotune for a vehicle with supports_autotune=False (see config/registry);
Outrider is tuned via the temporary MAVLink-on-TELEM2 path
(reviews/autotune-mavlink-mode.md). The only other difference is STATUSTEXT (not a
DDS topic), which this controller never requires.
"""
from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable

from pymavlink import mavutil

from .mavlink.link import MavlinkLink

log = logging.getLogger("gcs.autotune")

# MAV_CMD_DO_AUTOTUNE_ENABLE. Use the dialect constant when present, else the
# canonical id 212 (it's a standard MAVLink command but absent from some
# pymavlink dialects, same defensive pattern as commands.MAV_CMD_DO_ORBIT).
MAV_CMD_DO_AUTOTUNE_ENABLE = getattr(
    mavutil.mavlink, "MAV_CMD_DO_AUTOTUNE_ENABLE", 212
)

_RESULT_ACCEPTED = 0
_RESULT_IN_PROGRESS = 5

# How often to re-send cmd 212 to poll PX4 for progress (~1 Hz, per PX4's
# non-standard polling protocol).
POLL_INTERVAL_S = 1.0
# Per-poll ACK wait. Kept under POLL_INTERVAL_S so a missed ack doesn't stretch
# the cadence; the next poll re-sends regardless.
POLL_ACK_TIMEOUT_S = 0.8
# Abort to FAILED if progress hasn't advanced (and no terminal ack arrived) for
# this long — the backstop for a stalled/lost tune (the spec's ~60s no-progress).
NO_PROGRESS_TIMEOUT_S = 60.0


class AutotuneState(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# STATUSTEXT phrases PX4 uses during a tune; only "Autotune:" lines are relevant.
_AXES = ("roll", "pitch", "yaw")


class AutotuneController:
    """Runs (and tracks) one vehicle's autotune. One instance per vehicle id.

    The 1 Hz poll task lives on the asyncio loop; the blocking ACK wait per poll
    runs in a worker thread (asyncio.to_thread) so neither the event loop nor the
    link's reader thread is blocked — the same discipline arm/takeoff use.
    """

    def __init__(
        self,
        vehicle_id: str,
        link: MavlinkLink,
        emit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.vehicle_id = vehicle_id
        self.link = link
        self._emit = emit
        self.state: AutotuneState = AutotuneState.IDLE
        self.progress: int = 0
        self.axis: str | None = None  # last axis reported by STATUSTEXT (if any)
        self.reason: str | None = None  # failure / completion detail for the UI
        # Recent "Autotune:" STATUSTEXT lines (supplementary; may stay empty on
        # Outrider). Capped so a long run can't grow unboundedly.
        self.statustexts: list[dict[str, Any]] = []
        self._task: asyncio.Task | None = None
        self._unsub_statustext: Callable[[], None] | None = None
        self._started_at: float = 0.0

    # ── public API ────────────────────────────────────────────────────────
    def is_running(self) -> bool:
        return self.state == AutotuneState.RUNNING

    def snapshot(self) -> dict[str, Any]:
        return {
            "vehicle": self.vehicle_id,
            "state": self.state.value,
            "progress": self.progress,
            "axis": self.axis,
            "reason": self.reason,
            "statustexts": list(self.statustexts),
            "running": self.is_running(),
        }

    async def start(self) -> dict[str, Any]:
        """Begin autotune: send cmd 212 (p1=1) and spin up the 1 Hz poll.

        IDEMPOTENT: a call while already RUNNING returns the current state and
        does NOT send a second enable (no enable-storm) — the poll's 1 Hz re-send
        is the only repeated 212. The safety/connectivity/confirm gates live in the
        API + voice layers; this assumes it's cleared to fly the maneuver."""
        if self.is_running():
            return {"ok": True, **self.snapshot(),
                    "note": "autotune already running — returning current state"}
        # Fresh run: reset the record.
        self.state = AutotuneState.RUNNING
        self.progress = 0
        self.axis = None
        self.reason = None
        self.statustexts = []
        self._started_at = time.monotonic()
        # Subscribe to STATUSTEXT (supplementary). On Outrider/DDS this never fires.
        try:
            self._unsub_statustext = self.link.subscribe_statustext(self._on_statustext)
        except Exception:  # noqa: BLE001 — never block the tune on the optional feed
            self._unsub_statustext = None
        self._publish()
        log.info("autotune START on %s (cmd %d)", self.vehicle_id, MAV_CMD_DO_AUTOTUNE_ENABLE)
        self._task = asyncio.create_task(self._poll_loop())
        return {"ok": True, **self.snapshot()}

    async def cancel(self) -> dict[str, Any]:
        """Cancel a running autotune: send cmd 212 with p1=0 (disable) and stop the
        poll. A cancel when not running is a no-op that reports the current state."""
        if not self.is_running():
            return {"ok": True, **self.snapshot(),
                    "note": "autotune was not running"}
        log.info("autotune CANCEL on %s", self.vehicle_id)
        # Disable on PX4: cmd 212 with param1=0.0.
        try:
            self.link.command_long(MAV_CMD_DO_AUTOTUNE_ENABLE, 0.0, 0.0)
        except Exception:  # noqa: BLE001 — still transition locally even if the send fails
            log.exception("autotune cancel send failed on %s", self.vehicle_id)
        self._finish(AutotuneState.CANCELLED, reason="cancelled by operator")
        await self._stop_task()
        return {"ok": True, **self.snapshot()}

    # ── poll loop ─────────────────────────────────────────────────────────
    async def _poll_loop(self) -> None:
        """Re-send cmd 212 at ~1 Hz; map each COMMAND_ACK to state/progress.

        ACCEPTED(0)        → COMPLETE (or progress 100).
        IN_PROGRESS(5)     → RUNNING, update progress from the ack byte.
        any other result   → FAILED (PX4 refused / aborted the tune).
        no progress 60s    → FAILED (stalled/lost — the backstop).
        """
        last_progress_at = time.monotonic()
        try:
            while self.is_running():
                ack = await asyncio.to_thread(
                    self.link.command_long_ack_progress,
                    MAV_CMD_DO_AUTOTUNE_ENABLE,
                    1.0,  # param1 = enable / keep-running (re-sent each poll)
                    0.0,  # param2 = 0 → all axes
                    timeout=POLL_ACK_TIMEOUT_S,
                )
                result = ack.get("result")
                if result == _RESULT_IN_PROGRESS:
                    new_progress = int(ack.get("progress", 0) or 0)
                    if new_progress != self.progress:
                        if new_progress > self.progress:
                            last_progress_at = time.monotonic()
                        self.progress = new_progress
                        self._publish()
                    if self.progress >= 100:
                        self._finish(AutotuneState.COMPLETE, progress=100)
                        break
                elif result == _RESULT_ACCEPTED:
                    self._finish(AutotuneState.COMPLETE, progress=100)
                    break
                elif result is not None:
                    self._finish(
                        AutotuneState.FAILED,
                        reason=f"PX4 returned {ack.get('result_name') or result}",
                    )
                    break
                # result is None → this poll's ack timed out; that's normal (PX4
                # may not answer every poll). Fall through to the no-progress check.

                if time.monotonic() - last_progress_at > NO_PROGRESS_TIMEOUT_S:
                    self._finish(
                        AutotuneState.FAILED,
                        reason=f"no progress for {int(NO_PROGRESS_TIMEOUT_S)}s — "
                               f"autotune stalled or no acknowledgement",
                    )
                    break
                await asyncio.sleep(POLL_INTERVAL_S)
        except asyncio.CancelledError:
            # Task cancelled out from under us (shutdown / cancel()): leave state as
            # set by the caller and stop quietly.
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("autotune poll loop crashed on %s", self.vehicle_id)
            if self.is_running():
                self._finish(AutotuneState.FAILED, reason=f"internal error: {exc}")
        finally:
            self._teardown_statustext()

    # ── STATUSTEXT (supplementary) ──────────────────────────────────────────
    def _on_statustext(self, severity: int, text: str) -> None:
        """Reader-thread callback: capture PX4's "Autotune: …" progress lines. Pure
        supplementary signal — it refines `axis` and feeds the live UI feed, but it
        NEVER drives the state machine (which runs off the ACK). Outrider/DDS never
        delivers these, and the tune works identically without them."""
        low = (text or "").lower()
        if "autotune" not in low:
            return
        entry = {"severity": int(severity), "text": text.strip(), "ts": time.time()}
        self.statustexts.append(entry)
        if len(self.statustexts) > 50:
            self.statustexts = self.statustexts[-50:]
        # Best-effort axis extraction ("Autotune: roll" → axis="roll").
        for ax in _AXES:
            if ax in low:
                self.axis = ax
                break
        if "done" in low or "complete" in low or "finished" in low:
            self.axis = "done"
        self._publish(statustext=entry)

    def _teardown_statustext(self) -> None:
        unsub = self._unsub_statustext
        self._unsub_statustext = None
        if unsub is not None:
            try:
                unsub()
            except Exception:  # noqa: BLE001
                pass

    # ── transitions / events ────────────────────────────────────────────────
    def _finish(self, state: AutotuneState, *, progress: int | None = None,
                reason: str | None = None) -> None:
        self.state = state
        if progress is not None:
            self.progress = progress
        if reason is not None:
            self.reason = reason
        self._teardown_statustext()
        log.info("autotune %s on %s (progress=%d%s)", state.value, self.vehicle_id,
                 self.progress, f", {reason}" if reason else "")
        self._publish()

    async def _stop_task(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    def _publish(self, statustext: dict[str, Any] | None = None) -> None:
        """Emit an `autotune` hub event on every state/progress change so the
        frontend tracks it live. Carries the full snapshot plus the latest
        statustext line (when one prompted this emit)."""
        if self._emit is None:
            return
        event = {"type": "autotune", **self.snapshot()}
        if statustext is not None:
            event["statustext"] = statustext
        try:
            self._emit(event)
        except Exception:  # noqa: BLE001 — emitting must never break the tune
            log.exception("autotune event emit failed")


class AutotuneManager:
    """One AutotuneController per vehicle id, created on demand.

    Resolves a vehicle's link via the registry and wires each controller's events
    to the hub (vehicle-tagged) so the frontend sees per-drone autotune progress.
    """

    def __init__(self) -> None:
        self._controllers: dict[str, AutotuneController] = {}

    def _emit_for(self, vehicle_id: str) -> Callable[[dict[str, Any]], None]:
        def _emit(event: dict[str, Any]) -> None:
            try:
                from .ws.hub import hub

                hub.publish_threadsafe({**event, "vehicle": vehicle_id})
            except Exception:  # noqa: BLE001
                log.exception("autotune hub publish failed")

        return _emit

    def controller(self, vehicle_id: str, link: MavlinkLink) -> AutotuneController:
        """Get-or-create the controller for `vehicle_id`, bound to `link`. If the
        vehicle's link object changed (reconnect/replace) the controller is rebound
        so a future tune addresses the live link."""
        ctrl = self._controllers.get(vehicle_id)
        if ctrl is None:
            ctrl = AutotuneController(vehicle_id, link, emit=self._emit_for(vehicle_id))
            self._controllers[vehicle_id] = ctrl
        else:
            ctrl.link = link
        return ctrl

    async def start(self, vehicle_id: str, link: MavlinkLink) -> dict[str, Any]:
        return await self.controller(vehicle_id, link).start()

    async def cancel(self, vehicle_id: str, link: MavlinkLink) -> dict[str, Any]:
        return await self.controller(vehicle_id, link).cancel()

    def status(self, vehicle_id: str) -> dict[str, Any] | None:
        ctrl = self._controllers.get(vehicle_id)
        return ctrl.snapshot() if ctrl is not None else None

    def status_all(self) -> list[dict[str, Any]]:
        return [c.snapshot() for c in self._controllers.values()]


# Module-level singleton — the API + voice layers drive autotune through this.
manager = AutotuneManager()
