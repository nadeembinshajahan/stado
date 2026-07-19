"""Tests for the PX4 multicopter AUTOTUNE controller (app/autotune).

NO HARDWARE: a FakeLink stands in for MavlinkLink, exposing exactly the three
seams the controller uses — command_long (the cancel/disable send),
command_long_ack_progress (the 1 Hz poll's blocking ACK read), and
subscribe_statustext (the supplementary feed). Each test scripts a COMMAND_ACK
sequence the FakeLink returns and asserts the resulting state machine, the 1 Hz
re-send cadence, graceful degradation WITHOUT STATUSTEXT (the Outrider case),
cancel, idempotent double-start, and the no-progress → FAILED backstop.

The controller's poll loop sleeps POLL_INTERVAL_S between polls; tests shrink
that (and the no-progress timeout) via monkeypatch so they run in milliseconds.

Run: cd backend && .venv/bin/python -m pytest tests/test_autotune.py -q
"""
from __future__ import annotations

import asyncio
import threading

import pytest

import app.autotune as autotune
from app.autotune import AutotuneController, AutotuneState, MAV_CMD_DO_AUTOTUNE_ENABLE


class FakeLink:
    """Mimics the MavlinkLink seams the AutotuneController touches.

    `ack_script` is a list of dicts shaped like command_long_ack_progress's return
    ({result, result_name, progress, timed_out}); each poll pops the next one (the
    last is repeated once exhausted, modelling a steady-state ACK). Records every
    command_long_ack_progress call (the 1 Hz poll re-sends) and every command_long
    call (cancel sends p1=0)."""

    def __init__(self, ack_script=None, connected=True):
        self.ack_script = list(ack_script or [])
        self._connected = connected
        self.poll_calls: list[tuple] = []
        self.command_calls: list[tuple] = []
        self._statustext_subs: list = []
        self._lock = threading.Lock()

    def snapshot(self):
        return {"connected": self._connected}

    def command_long(self, command, *params, confirmation=0):
        self.command_calls.append((command, params))

    def command_long_ack_progress(self, command, *params, timeout=0.8):
        self.poll_calls.append((command, params))
        with self._lock:
            if self.ack_script:
                if len(self.ack_script) == 1:
                    return dict(self.ack_script[0])
                return dict(self.ack_script.pop(0))
        # Default: nothing scripted → a timed-out poll (no ack this cycle).
        return {"result": None, "result_name": None, "progress": 0, "timed_out": True}

    def subscribe_statustext(self, cb):
        self._statustext_subs.append(cb)

        def _unsub():
            try:
                self._statustext_subs.remove(cb)
            except ValueError:
                pass

        return _unsub

    # Test helper: push a STATUSTEXT to every subscriber (the reader-thread path).
    def emit_statustext(self, severity, text):
        for cb in list(self._statustext_subs):
            cb(severity, text)


def _ack(result, progress=0, name=None):
    names = {0: "ACCEPTED", 5: "IN_PROGRESS", 4: "FAILED", 2: "DENIED"}
    return {"result": result, "result_name": name or names.get(result),
            "progress": progress, "timed_out": result is None}


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    """Shrink the poll cadence + no-progress backstop so tests run fast."""
    monkeypatch.setattr(autotune, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(autotune, "NO_PROGRESS_TIMEOUT_S", 0.3)
    yield


def _events():
    """Collect every hub event the controller emits."""
    seen: list[dict] = []
    return seen, lambda ev: seen.append(ev)


async def _run_to_terminal(ctrl: AutotuneController, timeout=3.0):
    """Start the tune and wait until the poll task finishes (terminal state)."""
    await ctrl.start()
    task = ctrl._task
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


# ── happy path: IN_PROGRESS+progress → ACCEPTED → COMPLETE ────────────────────
def test_progresses_then_completes_on_accepted():
    seen, emit = _events()
    link = FakeLink(ack_script=[
        _ack(5, 10), _ack(5, 40), _ack(5, 80), _ack(0, 100),
    ])
    ctrl = AutotuneController("overwatch", link, emit=emit)

    asyncio.run(_run_to_terminal(ctrl))

    assert ctrl.state == AutotuneState.COMPLETE
    assert ctrl.progress == 100
    # Every poll re-sent cmd 212 with param1=1.0 (enable / keep-running).
    assert all(c[0] == MAV_CMD_DO_AUTOTUNE_ENABLE for c in link.poll_calls)
    assert all(c[1][0] == 1.0 and c[1][1] == 0.0 for c in link.poll_calls)
    assert len(link.poll_calls) >= 4, "must re-send 212 once per poll (~1 Hz)"
    # A RUNNING (initial) event and a terminal COMPLETE event were both published.
    states = [e.get("state") for e in seen if e.get("type") == "autotune"]
    assert "RUNNING" in states and "COMPLETE" in states


def test_completes_when_progress_hits_100_even_before_accepted():
    """progress==100 on an IN_PROGRESS ack is treated as done (the tune finished
    but PX4 hasn't sent the terminal ACCEPTED yet)."""
    link = FakeLink(ack_script=[_ack(5, 50), _ack(5, 100)])
    ctrl = AutotuneController("overwatch", link, emit=lambda e: None)
    asyncio.run(_run_to_terminal(ctrl))
    assert ctrl.state == AutotuneState.COMPLETE and ctrl.progress == 100


# ── graceful degradation WITHOUT STATUSTEXT (Outrider/DDS) ────────────────────
def test_completes_without_statustext_outrider():
    """Outrider's DDS bridge never delivers STATUSTEXT. The state machine is driven
    by the ACK alone, so the tune still progresses + completes; axis stays None and
    the statustext feed stays empty."""
    link = FakeLink(ack_script=[_ack(5, 30), _ack(5, 70), _ack(0, 100)])
    ctrl = AutotuneController("outrider", link, emit=lambda e: None)
    asyncio.run(_run_to_terminal(ctrl))
    assert ctrl.state == AutotuneState.COMPLETE
    assert ctrl.axis is None, "no STATUSTEXT ⇒ axis never set"
    assert ctrl.statustexts == [], "no STATUSTEXT ⇒ empty feed"


# ── STATUSTEXT supplementary (Overwatch) refines axis + feeds UI ──────────────
def test_statustext_refines_axis_but_does_not_drive_state():
    seen, emit = _events()
    # Hold in IN_PROGRESS so the STATUSTEXT can land mid-run, then complete.
    link = FakeLink(ack_script=[_ack(5, 20), _ack(5, 50), _ack(5, 60), _ack(0, 100)])
    ctrl = AutotuneController("overwatch", link, emit=emit)

    async def scenario():
        await ctrl.start()
        # Simulate PX4's progress lines arriving on the reader thread mid-tune.
        link.emit_statustext(6, "Autotune: roll")
        link.emit_statustext(6, "Autotune: pitch")
        task = ctrl._task
        await asyncio.wait_for(asyncio.shield(task), timeout=3.0)

    asyncio.run(scenario())
    assert ctrl.state == AutotuneState.COMPLETE
    # axis came from STATUSTEXT; the feed captured the "Autotune:" lines.
    assert any(s["text"].startswith("Autotune") for s in ctrl.statustexts)
    assert ctrl.axis in ("roll", "pitch", "done")
    # A statustext-bearing event was emitted (live feed for the UI).
    assert any("statustext" in e for e in seen)


def test_statustext_ignores_non_autotune_lines():
    link = FakeLink(ack_script=[_ack(5, 50), _ack(0, 100)])
    ctrl = AutotuneController("overwatch", link, emit=lambda e: None)

    async def scenario():
        await ctrl.start()
        link.emit_statustext(6, "EKF2 IMU0 is using GPS")  # unrelated noise
        task = ctrl._task
        await asyncio.wait_for(asyncio.shield(task), timeout=3.0)

    asyncio.run(scenario())
    assert ctrl.statustexts == [], "only 'Autotune:' lines are captured"


# ── failure: a non-accepted, non-in-progress result → FAILED ──────────────────
def test_failed_result_transitions_to_failed():
    link = FakeLink(ack_script=[_ack(5, 20), _ack(4)])  # FAILED
    ctrl = AutotuneController("overwatch", link, emit=lambda e: None)
    asyncio.run(_run_to_terminal(ctrl))
    assert ctrl.state == AutotuneState.FAILED
    assert "FAILED" in (ctrl.reason or "")


# ── no-progress backstop → FAILED ─────────────────────────────────────────────
def test_no_progress_timeout_fails():
    """All polls time out (PX4 silent — e.g. lost link). With no progress for the
    backstop window the tune is declared FAILED, not left RUNNING forever."""
    link = FakeLink(ack_script=[])  # every poll → timed_out
    ctrl = AutotuneController("overwatch", link, emit=lambda e: None)
    asyncio.run(_run_to_terminal(ctrl, timeout=3.0))
    assert ctrl.state == AutotuneState.FAILED
    assert "no progress" in (ctrl.reason or "")


# ── cancel: sends 212 p1=0 and transitions to CANCELLED ───────────────────────
def test_cancel_sends_disable_and_transitions():
    # Keep the tune RUNNING (steady IN_PROGRESS) so we can cancel it mid-flight.
    link = FakeLink(ack_script=[_ack(5, 30)])
    ctrl = AutotuneController("overwatch", link, emit=lambda e: None)

    async def scenario():
        await ctrl.start()
        await asyncio.sleep(0.05)  # let a few polls run
        assert ctrl.is_running()
        res = await ctrl.cancel()
        return res

    res = asyncio.run(scenario())
    assert ctrl.state == AutotuneState.CANCELLED
    assert res["ok"] is True
    # cancel sent cmd 212 with param1=0.0 (disable).
    assert link.command_calls, "cancel must send the disable command"
    cmd, params = link.command_calls[-1]
    assert cmd == MAV_CMD_DO_AUTOTUNE_ENABLE and params[0] == 0.0


def test_cancel_when_not_running_is_noop():
    link = FakeLink()
    ctrl = AutotuneController("overwatch", link, emit=lambda e: None)
    res = asyncio.run(ctrl.cancel())
    assert res["ok"] is True
    assert ctrl.state == AutotuneState.IDLE
    assert link.command_calls == [], "no disable sent when nothing is running"


# ── idempotent double-start: no enable-storm ──────────────────────────────────
def test_double_start_is_idempotent_no_second_enable():
    link = FakeLink(ack_script=[_ack(5, 10)])  # steady running
    ctrl = AutotuneController("overwatch", link, emit=lambda e: None)

    async def scenario():
        first = await ctrl.start()
        polls_after_first = len(link.poll_calls)
        second = await ctrl.start()  # while already RUNNING
        return first, second, polls_after_first

    first, second, _ = asyncio.run(scenario())
    assert first["ok"] and second["ok"]
    assert second.get("note") and "already running" in second["note"]
    assert ctrl.state == AutotuneState.RUNNING
    # Cleanup: cancel the lingering poll task.
    asyncio.run(ctrl.cancel())


# ── snapshot shape (drives the REST + UI contract) ────────────────────────────
def test_snapshot_shape():
    link = FakeLink()
    ctrl = AutotuneController("outrider", link, emit=lambda e: None)
    snap = ctrl.snapshot()
    assert snap["vehicle"] == "outrider"
    assert snap["state"] == "IDLE"
    assert snap["progress"] == 0
    assert snap["running"] is False
    assert snap["statustexts"] == []
    assert set(snap) >= {"vehicle", "state", "progress", "axis", "reason",
                         "statustexts", "running"}


# ── manager: per-vehicle controllers, rebind link, status_all ─────────────────
def test_manager_per_vehicle_and_status():
    mgr = autotune.AutotuneManager()
    link_a = FakeLink(ack_script=[_ack(0, 100)])
    link_b = FakeLink(ack_script=[_ack(0, 100)])

    async def scenario():
        await mgr.start("overwatch", link_a)
        await mgr.start("outrider", link_b)
        # Let both finish.
        for vid in ("overwatch", "outrider"):
            c = mgr._controllers[vid]
            if c._task:
                await asyncio.wait_for(asyncio.shield(c._task), timeout=3.0)

    asyncio.run(scenario())
    assert mgr.status("overwatch")["state"] == "COMPLETE"
    assert mgr.status("outrider")["state"] == "COMPLETE"
    assert mgr.status("ghost") is None
    assert len(mgr.status_all()) == 2


def test_manager_rebinds_link_on_reconnect():
    mgr = autotune.AutotuneManager()
    link_a = FakeLink()
    link_b = FakeLink()
    c1 = mgr.controller("overwatch", link_a)
    c2 = mgr.controller("overwatch", link_b)  # same vehicle, new link object
    assert c1 is c2, "one controller per vehicle id"
    assert c2.link is link_b, "controller rebinds to the live link"
