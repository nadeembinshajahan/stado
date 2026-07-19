"""Offline tests for backend/app/mavlink/link.py — MavlinkLink.

No hardware / no sockets: a `FakeMaster` stands in for the pymavlink connection
(`mavutil.mavlink_connection`) and `FakeMsg` for decoded messages. We import the
REAL `mavutil.mavlink` for the command-id / flag constants (pymavlink is already
a backend dep) but never open a socket.

Run (pytest is NOT a project dep — see review report):
    cd backend && PYTHONPATH=. uv run python -m pytest tests/test_mavlink_link.py -v
or as a script:
    cd backend && PYTHONPATH=. uv run python tests/test_mavlink_link.py
"""
from __future__ import annotations

import threading
import time

from pymavlink import mavutil

from app.mavlink.link import MavlinkLink

M = mavutil.mavlink


# ── fakes ──────────────────────────────────────────────────────────────────
class FakeMsg:
    """A decoded MAVLink message stand-in. `_type` is what get_type() returns."""

    def __init__(self, _type, srcSystem=1, srcComponent=1, **fields):
        self._type = _type
        self._src_system = srcSystem
        self._src_component = srcComponent
        self.__dict__.update(fields)

    def get_type(self):
        return self._type

    def get_srcSystem(self):
        return self._src_system

    def get_srcComponent(self):
        return self._src_component


class FakeMav:
    """Stands in for master.mav — records every *_send call."""

    def __init__(self):
        self.sent: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        if name.endswith("_send"):
            def _rec(*args, **kwargs):
                self.sent.append((name, args))
            return _rec
        raise AttributeError(name)


class FakeMaster:
    """Stands in for the pymavlink connection. Feeds queued FakeMsgs to the
    reader thread; command sends are captured on `self.mav`."""

    def __init__(self, inbound=None):
        self.mav = FakeMav()
        self._inbound = list(inbound or [])
        self._lock = threading.Lock()
        self.closed = False

    def feed(self, msg):
        with self._lock:
            self._inbound.append(msg)

    def recv_match(self, blocking=True, timeout=1):
        for _ in range(int((timeout or 1) * 200)):
            with self._lock:
                if self._inbound:
                    return self._inbound.pop(0)
            if not blocking:
                return None
            time.sleep(0.005)
        return None

    def close(self):
        self.closed = True


def _link_with_master(master):
    """A MavlinkLink whose master is pre-set, with TX taps disabled (logbus off)."""
    link = MavlinkLink("udpin:0.0.0.0:0")
    link.master = master
    return link


# ── target latching / sysid resolution ──────────────────────────────────────
def test_command_long_targets_locked_system_component():
    """command_long must address the latched (target_system, target_component)."""
    master = FakeMaster()
    link = _link_with_master(master)
    link.target_system = 7
    link.target_component = 1
    link.command_long(M.MAV_CMD_COMPONENT_ARM_DISARM, 1.0)
    assert master.mav.sent, "no command was sent"
    name, args = master.mav.sent[-1]
    assert name == "command_long_send"
    # command_long_send(target_system, target_component, command, confirmation, p1..p7)
    assert args[0] == 7, f"target_system wrong: {args[0]}"
    assert args[1] == 1, f"target_component wrong: {args[1]}"
    assert args[2] == M.MAV_CMD_COMPONENT_ARM_DISARM
    assert args[4] == 1.0, "param1 (arm=1) not forwarded"
    print("OK command_long targets latched sys/comp and forwards params")


def test_command_int_scales_and_frames():
    """command_int sends x/y as the caller's already-scaled ints with the frame."""
    master = FakeMaster()
    link = _link_with_master(master)
    link.target_system = 2
    link.target_component = 1
    link.command_int(
        M.MAV_CMD_DO_REPOSITION, int(47.0 * 1e7), int(8.0 * 1e7), 123.0,
        p1=-1.0, frame=M.MAV_FRAME_GLOBAL_INT,
    )
    name, args = master.mav.sent[-1]
    assert name == "command_int_send"
    # command_int_send(tsys, tcomp, frame, command, current, autocontinue, p1,p2,p3,p4, x,y,z)
    assert args[0] == 2 and args[1] == 1
    assert args[2] == M.MAV_FRAME_GLOBAL_INT
    assert args[3] == M.MAV_CMD_DO_REPOSITION
    assert args[10] == int(47.0 * 1e7), "lat (x) not the scaled int"
    assert args[11] == int(8.0 * 1e7), "lon (y) not the scaled int"
    assert args[12] == 123.0, "alt (z) not forwarded"
    print("OK command_int forwards scaled x/y, frame, and z")


def test_read_loop_does_not_latch_target_on_255_source():
    """_read_loop must NOT latch target_system on a 0/255 source (our own GCS
    heartbeat is 255). This part already works correctly."""
    master = FakeMaster()
    link = _link_with_master(master)
    link._running = True
    master.feed(FakeMsg("HEARTBEAT", srcSystem=255, srcComponent=1,
                        base_mode=0, custom_mode=0))
    t = threading.Thread(target=link._read_loop, daemon=True)
    t.start()
    time.sleep(0.1)
    link._running = False
    t.join(timeout=1)
    assert link.target_system == 1, "target must remain default, not 255"
    print("OK _read_loop does not latch a target from a 255 source")


def test_regression_255_heartbeat_should_not_mark_connected():
    """GUARD for H6 (link.py _handle HEARTBEAT branch) — FIXED.

    The GCS's OWN 255-sourced heartbeat, looped back on a UDP link (or a second
    GCS), must NOT mark the vehicle connected — otherwise a phantom 'connected'
    appears with no real vehicle present. _handle now ignores srcSystem in (0,255)
    before touching connected/arm/mode.
    """
    master = FakeMaster()
    link = _link_with_master(master)
    msg = FakeMsg("HEARTBEAT", srcSystem=255, srcComponent=1,
                  base_mode=0, custom_mode=0)
    link._handle(msg, "HEARTBEAT")
    assert not link.state["connected"], (
        "BUG H6: a 255-sourced (GCS-origin) heartbeat marked the link connected — "
        "phantom vehicle from our own heartbeat echo"
    )
    print("OK 255-sourced heartbeat does not mark connected")


def test_regression_gimbal_component_should_not_be_latched_as_target():
    """GUARD for C2 (link.py _handle / _maybe_latch_target) — FIXED.

    A gimbal (compid 154, same sysid) arriving first must NOT win the command
    target — otherwise every arm/mode/goto is addressed to component 154 and PX4's
    commander ignores it. The target is now latched ONLY from an autopilot-bearing
    HEARTBEAT (srcComponent in (0,1), autopilot != INVALID), so target_component
    must be the AUTOPILOT (1) even when the gimbal's heartbeat arrives first.
    """
    master = FakeMaster()
    link = _link_with_master(master)
    link._running = True
    # Gimbal heartbeat first (sysid 1, component 154), then the autopilot.
    master.feed(FakeMsg("HEARTBEAT", srcSystem=1, srcComponent=154,
                        base_mode=0, custom_mode=0))
    master.feed(FakeMsg("HEARTBEAT", srcSystem=1, srcComponent=1,
                        base_mode=0, custom_mode=0))
    t = threading.Thread(target=link._read_loop, daemon=True)
    t.start()
    time.sleep(0.15)
    link._running = False
    t.join(timeout=1)
    assert link.target_component == 1, (
        f"BUG C2: target_component latched to {link.target_component} "
        "(gimbal) instead of the autopilot (1) — commands go to the wrong "
        "component and PX4 ignores them"
    )
    print("OK target_component is the autopilot, not a gimbal")


# ── COMMAND_ACK correlation ──────────────────────────────────────────────────
def test_command_long_ack_accepts_on_result_zero():
    """command_long_ack returns accepted=True when a matching ACK(result=0) arrives."""
    master = FakeMaster()
    link = _link_with_master(master)
    link.target_system = 1
    link.target_component = 1

    result_holder = {}

    def caller():
        result_holder["res"] = link.command_long_ack(
            M.MAV_CMD_COMPONENT_ARM_DISARM, 1.0, timeout=2.0
        )

    t = threading.Thread(target=caller)
    t.start()
    time.sleep(0.05)
    # Reader-thread side: deliver the matching ACK.
    link._notify_ack(M.MAV_CMD_COMPONENT_ARM_DISARM, 0)
    t.join(timeout=3)
    res = result_holder["res"]
    assert res["accepted"] is True, res
    assert res["result"] == 0
    assert res["result_name"] == "ACCEPTED"
    print("OK command_long_ack reports ACCEPTED on result=0")


def test_command_long_ack_surfaces_reject_reason():
    """A DENIED ack + an 'Arming denied' statustext must surface the reason."""
    master = FakeMaster()
    link = _link_with_master(master)

    result_holder = {}

    def caller():
        result_holder["res"] = link.command_long_ack(
            M.MAV_CMD_COMPONENT_ARM_DISARM, 1.0, timeout=2.0
        )

    t = threading.Thread(target=caller)
    t.start()
    time.sleep(0.05)
    link._notify_statustext(4, "Arming denied: GPS fix required")
    link._notify_ack(M.MAV_CMD_COMPONENT_ARM_DISARM, 2)  # DENIED
    t.join(timeout=3)
    res = result_holder["res"]
    assert res["accepted"] is False
    assert res["result"] == 2
    assert "denied" in (res["reason"] or "").lower(), res
    print("OK command_long_ack surfaces the PX4 reject reason")


def test_command_long_ack_times_out_to_none():
    """No ACK → result None, accepted False, 'no ... acknowledgement' upstream."""
    master = FakeMaster()
    link = _link_with_master(master)
    res = link.command_long_ack(M.MAV_CMD_COMPONENT_ARM_DISARM, 1.0, timeout=0.3)
    assert res["result"] is None
    assert res["accepted"] is False
    print("OK command_long_ack times out to result=None")


def test_regression_in_progress_should_not_be_final_result():
    """GUARD for H2 (link.py _notify_ack) — FIXED.

    PX4 emits MAV_RESULT_IN_PROGRESS (5) before the terminal ACCEPTED for
    long-running commands. _notify_ack now ignores IN_PROGRESS and keeps waiting,
    so command_long_ack returns the terminal ACCEPTED (result=0) rather than
    reporting IN_PROGRESS as a (false) failure.
    """
    master = FakeMaster()
    link = _link_with_master(master)

    result_holder = {}

    def caller():
        result_holder["res"] = link.command_long_ack(
            M.MAV_CMD_NAV_TAKEOFF, timeout=1.5
        )

    t = threading.Thread(target=caller)
    t.start()
    time.sleep(0.05)
    link._notify_ack(M.MAV_CMD_NAV_TAKEOFF, 5)   # IN_PROGRESS (should be ignored)
    time.sleep(0.1)
    link._notify_ack(M.MAV_CMD_NAV_TAKEOFF, 0)   # ACCEPTED (the real outcome)
    t.join(timeout=3)
    res = result_holder["res"]
    assert res["result"] == 0 and res["accepted"] is True, (
        f"BUG H2: IN_PROGRESS taken as final result {res['result']!r} instead of "
        "waiting for the terminal ACCEPTED"
    )
    print("OK IN_PROGRESS is not treated as the final result")


def test_command_ack_from_foreign_source_is_ignored():
    """GUARD for H1 (link.py _handle COMMAND_ACK + _ack_from_autopilot).

    An ACK from a different system id (a looped-back GCS ACK or a second vehicle)
    must NOT satisfy a waiter — only our latched autopilot's ACK counts. Here a
    foreign ACK arrives first and must be ignored; the real autopilot ACK resolves
    the waiter.
    """
    master = FakeMaster()
    link = _link_with_master(master)
    link.target_system = 1
    link.target_component = 1

    result_holder = {}

    def caller():
        result_holder["res"] = link.command_long_ack(
            M.MAV_CMD_COMPONENT_ARM_DISARM, 1.0, timeout=2.0
        )

    t = threading.Thread(target=caller)
    t.start()
    time.sleep(0.05)
    # Foreign ACK (sysid 99) routed through _handle — must be filtered out.
    foreign = FakeMsg("COMMAND_ACK", srcSystem=99, srcComponent=1,
                      command=M.MAV_CMD_COMPONENT_ARM_DISARM, result=0)
    link._handle(foreign, "COMMAND_ACK")
    time.sleep(0.1)
    assert result_holder.get("res") is None, "foreign ACK wrongly resolved the waiter"
    # Real autopilot ACK (sysid 1, comp 1) — must resolve it.
    real = FakeMsg("COMMAND_ACK", srcSystem=1, srcComponent=1,
                   command=M.MAV_CMD_COMPONENT_ARM_DISARM, result=0)
    link._handle(real, "COMMAND_ACK")
    t.join(timeout=3)
    res = result_holder["res"]
    assert res["accepted"] is True and res["result"] == 0, res
    print("OK COMMAND_ACK filtered by source system; foreign ACK ignored")


def test_notify_ack_resolves_oldest_waiter_first():
    """GUARD for H1: two overlapping waiters for the same command id — the OLDEST
    open waiter is resolved first (FIFO), not an arbitrary one."""
    master = FakeMaster()
    link = _link_with_master(master)
    order = []

    def caller(tag):
        link.command_long_ack(M.MAV_CMD_DO_SET_MODE, timeout=2.0)
        order.append(tag)

    t1 = threading.Thread(target=caller, args=("first",))
    t1.start()
    time.sleep(0.05)
    t2 = threading.Thread(target=caller, args=("second",))
    t2.start()
    time.sleep(0.05)
    link._notify_ack(M.MAV_CMD_DO_SET_MODE, 0)  # should release the FIRST waiter
    time.sleep(0.1)
    assert order == ["first"], f"oldest waiter should resolve first, got {order}"
    link._notify_ack(M.MAV_CMD_DO_SET_MODE, 0)  # release the second
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert order == ["first", "second"], order
    print("OK _notify_ack resolves the oldest matching waiter first")


def test_reset_link_clears_target_and_connected():
    """GUARD for H5 (link.py _reset_link): a reconnect must close the socket and
    reset target_*/connected so a moved/replaced vehicle re-detects cleanly."""
    master = FakeMaster()
    link = _link_with_master(master)
    link.target_system = 7
    link.target_component = 3
    link.state["connected"] = True
    link._observed_msgs = {"ATTITUDE"}
    link._reset_link()
    assert master.closed, "old socket should be closed on reset"
    assert link.master is None
    assert link.target_system == 1 and link.target_component == 1, (
        "target_* must reset to defaults so a new autopilot re-latches"
    )
    assert link.state["connected"] is False
    assert link._observed_msgs == set(), "observed-msg tracking must reset"
    print("OK _reset_link closes socket and resets detection state")


def test_observed_msgs_records_requested_telemetry_types():
    """GUARD for H4: handling a requested telemetry type marks it observed so the
    stream-reassertion worker stops re-requesting it."""
    master = FakeMaster()
    link = _link_with_master(master)
    msg = FakeMsg("ATTITUDE", roll=0.0, pitch=0.0, yaw=0.0)
    link._handle(msg, "ATTITUDE")
    assert "ATTITUDE" in link._observed_msgs
    # _request_streams must then skip the already-observed type.
    link.target_system = 1
    link.target_component = 1
    link._request_streams()
    set_intervals = [
        args for (name, args) in master.mav.sent
        if name == "command_long_send" and args[2] == M.MAV_CMD_SET_MESSAGE_INTERVAL
    ]
    att_id = float(M.MAVLINK_MSG_ID_ATTITUDE)
    assert not any(a[4] == att_id for a in set_intervals), (
        "ATTITUDE was re-requested despite being observed"
    )
    print("OK observed telemetry type is not re-requested")


def test_command_not_sent_when_master_is_none_after_reset():
    """PRE-FLIGHT: after _reset_link (a reconnect), master is None until _run
    reopens it. A command sent in that window must be DROPPED (not crash, not sent
    to a stale socket) — and target_* are back to defaults so it can't address a
    half-remembered vehicle. Guards against commanding a phantom during reconnect."""
    master = FakeMaster()
    link = _link_with_master(master)
    link.target_system = 2
    link.target_component = 1
    link.state["connected"] = True
    link._reset_link()
    assert link.master is None
    # Sending now must be a safe no-op (master is None).
    link.command_long(M.MAV_CMD_COMPONENT_ARM_DISARM, 1.0)
    link.command_int(M.MAV_CMD_DO_REPOSITION, 0, 0, 0.0)
    # Nothing was sent (the old master was closed; no new one yet).
    assert master.mav.sent == [], "command leaked to a closed/stale socket during reconnect"
    print("OK no command is sent while the link is mid-reconnect (master None)")


def test_command_targets_only_an_autopilot_latched_sysid():
    """PRE-FLIGHT (C2): a command must address the sysid latched from the AUTOPILOT
    heartbeat — never a default/gimbal. Drive a full detection: a gimbal (comp 154)
    then the real autopilot (sysid 2 = Outrider, comp 1); a subsequent arm must
    target sysid 2 / comp 1, proving commands hit the intended vehicle."""
    master = FakeMaster()
    link = _link_with_master(master)
    link._running = True
    master.feed(FakeMsg("HEARTBEAT", srcSystem=2, srcComponent=154,
                        base_mode=0, custom_mode=0))   # gimbal — must NOT latch
    master.feed(FakeMsg("HEARTBEAT", srcSystem=2, srcComponent=1,
                        base_mode=0, custom_mode=0))   # Outrider autopilot
    t = threading.Thread(target=link._read_loop, daemon=True)
    t.start()
    time.sleep(0.15)
    link._running = False
    t.join(timeout=1)
    assert (link.target_system, link.target_component) == (2, 1), (
        f"latched the wrong target {(link.target_system, link.target_component)} — "
        "a command would hit the wrong vehicle/component"
    )
    link.command_long(M.MAV_CMD_COMPONENT_ARM_DISARM, 1.0)
    name, args = master.mav.sent[-1]
    assert args[0] == 2 and args[1] == 1, "arm did not address the latched autopilot"
    print("OK command addresses the autopilot-latched sysid/comp (Outrider sysid 2)")


def test_ack_from_wrong_vehicle_sysid_does_not_resolve_waiter():
    """PRE-FLIGHT (H1): with Outrider latched (sysid 2), an ACK from Overwatch
    (sysid 1) or any other vehicle on a shared/looped topology must NOT satisfy an
    Outrider arm waiter — that would be a false 'armed' for the wrong drone."""
    master = FakeMaster()
    link = _link_with_master(master)
    link.target_system = 2  # Outrider latched
    link.target_component = 1
    result_holder = {}

    def caller():
        result_holder["res"] = link.command_long_ack(
            M.MAV_CMD_COMPONENT_ARM_DISARM, 1.0, timeout=1.5
        )

    t = threading.Thread(target=caller)
    t.start()
    time.sleep(0.05)
    # ACK from the OTHER vehicle (sysid 1) — must be ignored.
    link._handle(FakeMsg("COMMAND_ACK", srcSystem=1, srcComponent=1,
                         command=M.MAV_CMD_COMPONENT_ARM_DISARM, result=0),
                 "COMMAND_ACK")
    time.sleep(0.1)
    assert result_holder.get("res") is None, "an ACK from the wrong vehicle resolved the waiter"
    # The real Outrider ACK (sysid 2) resolves it.
    link._handle(FakeMsg("COMMAND_ACK", srcSystem=2, srcComponent=1,
                         command=M.MAV_CMD_COMPONENT_ARM_DISARM, result=0),
                 "COMMAND_ACK")
    t.join(timeout=2)
    assert result_holder["res"]["accepted"] is True
    print("OK ACK from the wrong vehicle is ignored; only the latched vehicle's ACK counts")


def test_reopened_link_not_connected_until_autopilot_heartbeat():
    """PRE-FLIGHT: after a reconnect, the link must report DISCONNECTED until a real
    autopilot heartbeat arrives — a reopened socket alone must never show a phantom
    'connected' to the operator."""
    master = FakeMaster()
    link = _link_with_master(master)
    link.state["connected"] = True
    link._reset_link()
    assert link.state["connected"] is False
    # A NON-autopilot heartbeat (our own 255, or a gimbal) must not reconnect it.
    link._handle(FakeMsg("HEARTBEAT", srcSystem=255, srcComponent=1,
                         base_mode=0, custom_mode=0), "HEARTBEAT")
    assert link.state["connected"] is False
    # Only the autopilot heartbeat brings it back.
    link._handle(FakeMsg("HEARTBEAT", srcSystem=2, srcComponent=1,
                         base_mode=0, custom_mode=0), "HEARTBEAT")
    assert link.state["connected"] is True
    print("OK reopened link stays disconnected until a real autopilot heartbeat")


def test_read_loop_returns_on_long_telemetry_timeout():
    """GUARD for H5: with no telemetry, _read_loop returns (so _run can reopen the
    socket) rather than looping forever on a dead UDP socket. We shrink the
    threshold via monkeypatching the module constant for a fast test."""
    import app.mavlink.link as linkmod

    master = FakeMaster()  # never feeds anything
    link = _link_with_master(master)
    link._running = True
    orig = linkmod._RECONNECT_TIMEOUT_S
    linkmod._RECONNECT_TIMEOUT_S = 0.2
    try:
        t = threading.Thread(target=link._read_loop, daemon=True)
        t.start()
        t.join(timeout=3)
        assert not t.is_alive(), "_read_loop should return on a long telemetry timeout"
    finally:
        linkmod._RECONNECT_TIMEOUT_S = orig
        link._running = False
    print("OK _read_loop returns on a long telemetry timeout (enables reconnect)")


# ── telemetry decode / unit scaling ──────────────────────────────────────────
def test_global_position_int_unit_scaling():
    """GLOBAL_POSITION_INT decodes 1e7 deg, mm alt, cm/s velocity, cdeg heading."""
    master = FakeMaster()
    link = _link_with_master(master)
    msg = FakeMsg(
        "GLOBAL_POSITION_INT",
        lat=int(47.397 * 1e7), lon=int(8.545 * 1e7),
        alt=123456, relative_alt=20000,
        vx=150, vy=-50, vz=10, hdg=9000,
    )
    link._handle(msg, "GLOBAL_POSITION_INT")
    s = link.snapshot()
    assert abs(s["lat"] - 47.397) < 1e-6
    assert abs(s["lon"] - 8.545) < 1e-6
    assert abs(s["alt_msl"] - 123.456) < 1e-6, "alt should be mm→m"
    assert abs(s["alt_rel"] - 20.0) < 1e-6
    assert abs(s["vx"] - 1.5) < 1e-6, "vx should be cm/s→m/s"
    assert abs(s["heading"] - 90.0) < 1e-6, "hdg should be cdeg→deg"
    print("OK GLOBAL_POSITION_INT unit scaling (1e7/mm/cm-s/cdeg)")


def test_heading_65535_is_treated_as_unknown():
    """hdg == 65535 means 'unknown' and must not overwrite a known heading."""
    master = FakeMaster()
    link = _link_with_master(master)
    link.state["heading"] = 42.0
    msg = FakeMsg("GLOBAL_POSITION_INT", lat=0, lon=0, alt=0, relative_alt=0,
                  vx=0, vy=0, vz=0, hdg=65535)
    link._handle(msg, "GLOBAL_POSITION_INT")
    assert link.snapshot()["heading"] == 42.0, "65535 heading must be ignored"
    print("OK heading 65535 sentinel ignored")


def test_heartbeat_decodes_arm_and_mode():
    """HEARTBEAT updates armed + decoded mode; non-autopilot component ignored."""
    master = FakeMaster()
    link = _link_with_master(master)
    # autopilot heartbeat, armed, custom_mode = AUTO(4)<<16 | LOITER(3)<<24 = HOLD
    custom = (4 << 16) | (3 << 24)
    msg = FakeMsg("HEARTBEAT", srcComponent=1,
                  base_mode=M.MAV_MODE_FLAG_SAFETY_ARMED, custom_mode=custom)
    link._handle(msg, "HEARTBEAT")
    s = link.snapshot()
    assert s["armed"] is True
    assert s["mode"] == "HOLD", s["mode"]

    # gimbal heartbeat (component 154) must not clobber arm/mode state
    g = FakeMsg("HEARTBEAT", srcComponent=154, base_mode=0, custom_mode=0)
    link._handle(g, "HEARTBEAT")
    assert link.snapshot()["armed"] is True, "gimbal heartbeat clobbered arm state"
    print("OK HEARTBEAT decodes arm/mode; non-autopilot component ignored")


def test_snapshot_strips_nonfinite_floats():
    """snapshot() must replace NaN/Inf with None (PX4 sends them; not JSON-safe)."""
    master = FakeMaster()
    link = _link_with_master(master)
    link.state["airspeed"] = float("nan")
    link.state["climb"] = float("inf")
    link.state["groundspeed"] = 3.2
    s = link.snapshot()
    assert s["airspeed"] is None and s["climb"] is None
    assert s["groundspeed"] == 3.2
    print("OK snapshot strips NaN/Inf to None")


def test_request_streams_uses_set_message_interval_with_correct_period():
    """_request_streams must request SYS_STATUS via SET_MESSAGE_INTERVAL with the
    period in MICROSECONDS (1e6/hz), not Hz — a unit mismatch would silently set
    a wrong rate."""
    master = FakeMaster()
    link = _link_with_master(master)
    link.target_system = 1
    link.target_component = 1
    link._request_streams()
    set_intervals = [
        args for (name, args) in master.mav.sent
        if name == "command_long_send" and args[2] == M.MAV_CMD_SET_MESSAGE_INTERVAL
    ]
    assert set_intervals, "no SET_MESSAGE_INTERVAL was sent"
    # find the SYS_STATUS (msg id 1) request, requested at 2 Hz → 500000 us
    sys_status = [a for a in set_intervals if a[4] == float(M.MAVLINK_MSG_ID_SYS_STATUS)]
    assert sys_status, "SYS_STATUS interval not requested"
    period_us = sys_status[0][5]
    assert abs(period_us - 500000.0) < 1.0, (
        f"SET_MESSAGE_INTERVAL period should be 500000 us (2 Hz), got {period_us}"
    )
    print("OK SET_MESSAGE_INTERVAL period is microseconds (1e6/hz)")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failed.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - len(failed)}/{len(fns)} link tests passed "
          f"({len(failed)} failures — C2/H1/H2/H4/H5/H6 guards should all pass)")
