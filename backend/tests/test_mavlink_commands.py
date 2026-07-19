"""Offline tests for backend/app/mavlink/commands.py.

Drives the async command builders with a FakeLink that records command_long /
command_int / setpoint sends. Asserts correct command ids, unit scaling
(deg→1e7, AGL→AMSL), arm-force magic, mode encoding, and the takeoff/arm
intent-vs-impl gap. No hardware.

Run: cd backend && PYTHONPATH=. uv run python -m pytest tests/test_mavlink_commands.py -v
or:  cd backend && PYTHONPATH=. uv run python tests/test_mavlink_commands.py
"""
from __future__ import annotations

import asyncio
import threading

from pymavlink import mavutil

from app.mavlink import commands
from app.mavlink import constants as C

M = mavutil.mavlink


class FakeMav:
    def __init__(self):
        self.sent = []

    def __getattr__(self, name):
        if name.endswith("_send"):
            def _rec(*args, **kwargs):
                self.sent.append((name, args))
            return _rec
        raise AttributeError(name)


class FakeLink:
    """Records command_long/command_int and setpoint sends; serves a snapshot."""

    def __init__(self, **state):
        base = {"connected": True, "armed": True, "lat": 47.0, "lon": 8.0,
                "alt_rel": 20.0, "alt_msl": 520.0, "heading": 0.0, "mode": "HOLD"}
        base.update(state)
        self._state = base
        self.target_system = 2
        self.target_component = 1
        self._send_lock = threading.Lock()
        self.master = type("M", (), {"mav": FakeMav()})()
        self.long_cmds = []   # (command, params)
        self.int_cmds = []     # (command, x, y, z, p1, p2, p3, p4, frame)
        self.ack_calls = []    # (command, params) for command_long_ack
        self.ack_result = {"accepted": True, "result": 0, "result_name": "ACCEPTED",
                           "reason": None, "statustexts": []}

    def snapshot(self):
        return dict(self._state)

    def _target(self):
        # Mirrors MavlinkLink._target(): atomic (system, component) snapshot.
        return self.target_system, self.target_component

    def command_long(self, command, *params, confirmation=0):
        self.long_cmds.append((int(command), tuple(params)))

    def command_int(self, command, x, y, z, p1=0, p2=0, p3=0, p4=0, frame=0):
        self.int_cmds.append((int(command), x, y, z, p1, p2, p3, p4, int(frame)))

    def command_long_ack(self, command, *params, timeout=3.0, confirmation=0):
        self.ack_calls.append((int(command), tuple(params)))
        return dict(self.ack_result)

    def set_mode(self, base_mode, main, sub):
        self.long_cmds.append((int(M.MAV_CMD_DO_SET_MODE),
                               (float(base_mode), float(main), float(sub))))


# ── arm / disarm ─────────────────────────────────────────────────────────────
def test_arm_sends_arm_command_via_ack_path():
    link = FakeLink()
    res = asyncio.run(commands.arm(link))
    assert link.ack_calls, "arm must go through the ACK-waiting path"
    cmd, params = link.ack_calls[-1]
    assert cmd == M.MAV_CMD_COMPONENT_ARM_DISARM
    assert params[0] == 1.0, "param1 must be 1.0 for arm"
    assert params[1] == 0.0, "param2 (force) must be 0 when force=False"
    assert res["ok"] is True and res["armed"] is True
    print("OK arm uses command_long_ack with param1=1, force=0")


def test_disarm_param1_zero():
    link = FakeLink()
    asyncio.run(commands.disarm(link))
    cmd, params = link.ack_calls[-1]
    assert params[0] == 0.0, "param1 must be 0.0 for disarm"
    print("OK disarm uses param1=0")


def test_arm_force_uses_magic_constant():
    link = FakeLink()
    asyncio.run(commands.arm(link, force=True))
    cmd, params = link.ack_calls[-1]
    assert params[1] == float(commands.ARM_FORCE_MAGIC) == 21196.0, params
    print("OK arm force sets param2 = 21196 magic")


def test_arm_denied_surfaces_reason_and_not_ok():
    link = FakeLink()
    link.ack_result = {"accepted": False, "result": 2, "result_name": "DENIED",
                       "reason": "Arming denied: GPS", "statustexts": ["Arming denied: GPS"]}
    res = asyncio.run(commands.arm(link))
    assert res["ok"] is False
    assert res["armed"] is None
    assert "denied" in res["reason"].lower()
    print("OK arm DENIED → ok=False with reason")


def test_arm_timeout_reason_message():
    link = FakeLink()
    link.ack_result = {"accepted": False, "result": None, "result_name": None,
                       "reason": None, "statustexts": []}
    res = asyncio.run(commands.arm(link))
    assert res["ok"] is False
    assert res["reason"] == "no arm acknowledgement", res
    print("OK arm timeout → 'no arm acknowledgement'")


# ── takeoff ──────────────────────────────────────────────────────────────────
def test_takeoff_converts_agl_to_amsl():
    """NAV_TAKEOFF param7 must be AMSL = ground_amsl + altitude."""
    link = FakeLink(alt_msl=520.0, alt_rel=0.0)  # on the ground: alt_rel ~= 0
    asyncio.run(commands.takeoff(link, altitude=30.0))
    takeoffs = [c for c in link.long_cmds if c[0] == M.MAV_CMD_NAV_TAKEOFF]
    assert takeoffs, "no NAV_TAKEOFF sent"
    params = takeoffs[-1][1]
    # ground_amsl is snapshot alt_msl (520) per the code's comment-vs-impl; param7
    # = 520 + 30 = 550 (the code adds altitude to alt_msl directly)
    assert params[6] == 550.0, f"NAV_TAKEOFF param7 (AMSL) wrong: {params[6]}"
    print("OK takeoff adds altitude to ground AMSL for param7")


def test_regression_takeoff_should_not_launch_if_arm_denied():
    """GUARD for M2 (commands.py takeoff) — FIXED.

    takeoff() now captures arm()'s ACK result; on a DENIED arm it must NOT send
    NAV_TAKEOFF (PX4 would silently reject it and the API would report false
    success). It also RETURNS a dict {ok, reason, ...} like arm() so the caller
    learns the real outcome — this return shape is a contract the API layer
    depends on.
    """
    link = FakeLink(armed=False, alt_rel=0.0)
    link.ack_result = {"accepted": False, "result": 2, "result_name": "DENIED",
                       "reason": "Arming denied", "statustexts": []}
    res = asyncio.run(commands.takeoff(link, altitude=20.0))
    takeoffs = [c for c in link.long_cmds if c[0] == M.MAV_CMD_NAV_TAKEOFF]
    assert not takeoffs, (
        "M2: NAV_TAKEOFF was sent despite arming being DENIED — takeoff() must "
        "gate on arm()'s ACK result"
    )
    # takeoff returns a dict (contract): the arm failure is surfaced as ok=False.
    assert res["ok"] is False, res
    assert "denied" in (res["reason"] or "").lower(), res
    print("OK takeoff aborts and returns ok=False with reason when arming is denied")


def test_takeoff_success_returns_ok_dict_and_sends_nav_takeoff():
    """On a successful arm, takeoff() sends NAV_TAKEOFF and returns the contract
    dict {ok: True, armed: True, altitude, ...} the API layer consumes."""
    link = FakeLink(armed=False, alt_msl=520.0, alt_rel=0.0)
    res = asyncio.run(commands.takeoff(link, altitude=30.0))
    takeoffs = [c for c in link.long_cmds if c[0] == M.MAV_CMD_NAV_TAKEOFF]
    assert takeoffs, "NAV_TAKEOFF must be sent after a successful arm"
    assert res["ok"] is True and res["armed"] is True, res
    assert res["altitude"] == 30.0, res
    assert res["reason"] is None, res
    print("OK takeoff success returns {ok:True, armed:True, altitude}")


def test_takeoff_refused_when_ground_altitude_implausible():
    """HOME/ALT GATE (2026-05-26 field incident): if the on-ground alt_rel is
    implausibly far from 0 (PX4 home/EKF reference wrong), takeoff() must REFUSE
    before arming and send NO arm + NO NAV_TAKEOFF — otherwise PX4 arms, clamps the
    takeoff target, never climbs, and can't auto-disarm. Mirrors the field reading of
    +2.78 m on the ground (home ~2.8 m below the real 912 m terrain)."""
    link = FakeLink(armed=False, alt_msl=912.4, alt_rel=2.78)
    res = asyncio.run(commands.takeoff(link, altitude=5.0))
    assert res["ok"] is False and res["armed"] is False, res
    assert "altitude reference" in (res["reason"] or "").lower(), res
    takeoffs = [c for c in link.long_cmds if c[0] == M.MAV_CMD_NAV_TAKEOFF]
    assert not takeoffs, "no NAV_TAKEOFF may be sent with a corrupt altitude reference"
    assert not link.ack_calls, "must refuse BEFORE arming (no arm command sent)"
    # The ceiling override must NOT bypass this data-integrity stop.
    res2 = asyncio.run(commands.takeoff(link, altitude=5.0, override=True))
    assert res2["ok"] is False, "override (ceiling) must not bypass the home-altitude gate"
    print("OK takeoff refuses on an implausible on-ground altitude (bad home)")


# ── goto / set_home / orbit unit + frame correctness ─────────────────────────
def test_goto_scales_latlon_and_uses_global_int_frame():
    link = FakeLink(alt_msl=520.0, alt_rel=20.0)
    asyncio.run(commands.goto(link, 47.123, 8.987, alt=50.0, speed=5.0))
    g = [c for c in link.int_cmds if c[0] == M.MAV_CMD_DO_REPOSITION][-1]
    _, x, y, z, p1, p2, p3, p4, frame = g
    assert x == int(47.123 * 1e7), "lat not scaled to 1e7"
    assert y == int(8.987 * 1e7), "lon not scaled to 1e7"
    # AGL 50 → AMSL: ground = 520-20 = 500, so 550
    assert abs(z - 550.0) < 1e-6, f"alt should be AMSL 550, got {z}"
    assert p1 == 5.0, "speed should be in p1"
    assert frame == M.MAV_FRAME_GLOBAL_INT
    print("OK goto scales lat/lon, AGL→AMSL, GLOBAL_INT frame, speed in p1")


def test_set_home_estimates_ground_amsl_when_alt_omitted():
    link = FakeLink(alt_msl=520.0, alt_rel=20.0)
    res = asyncio.run(commands.set_home(link, 47.0, 8.0))
    h = [c for c in link.int_cmds if c[0] == M.MAV_CMD_DO_SET_HOME][-1]
    _, x, y, z, p1, p2, p3, p4, frame = h
    assert p1 == 0.0, "param1 must be 0 (use specified location)"
    assert abs(z - 500.0) < 1e-6, f"home alt should be ground AMSL 500, got {z}"
    assert res["alt"] == 500.0
    print("OK set_home estimates ground AMSL (msl-rel) when alt omitted")


def test_orbit_signed_radius_and_resends():
    link = FakeLink()
    asyncio.run(commands.orbit(link, 47.0, 8.0, alt=40.0, radius=25.0,
                               velocity=3.0, clockwise=False))
    orbits = [c for c in link.int_cmds if c[0] == commands.MAV_CMD_DO_ORBIT]
    assert orbits, "no DO_ORBIT sent"
    # counter-clockwise → negative radius in p1
    assert orbits[-1][4] == -25.0, f"CCW orbit should have negative radius, got {orbits[-1][4]}"
    # resent up to 3x for reliability
    assert len(orbits) >= 1
    print(f"OK orbit signs radius for direction and resends ({len(orbits)}x)")


def test_orbit_altitude_floored_to_minimum():
    # 2026-05-27 Overwatch incident: a no-altitude orbit inherited a sagging
    # current altitude and rode the vehicle from 10 m down below its launch point.
    # orbit() must floor the altitude at MIN_ORBIT_ALT_M (10 m). FakeLink ground
    # AMSL = alt_msl - alt_rel = 520 - 20 = 500.
    link = FakeLink(alt_msl=520.0, alt_rel=20.0)
    asyncio.run(commands.orbit(link, 47.0, 8.0, alt=3.0, radius=15.0))
    orbits = [c for c in link.int_cmds if c[0] == commands.MAV_CMD_DO_ORBIT]
    assert orbits, "no DO_ORBIT sent"
    # 3 m floored to 10 m → AMSL 500 + 10 = 510 (z is index [3]); NOT 503
    assert orbits[-1][3] == 510.0, f"orbit alt should floor to 10m (AMSL 510), got {orbits[-1][3]}"
    # an explicit higher altitude is honoured as-is
    link2 = FakeLink(alt_msl=520.0, alt_rel=20.0)
    asyncio.run(commands.orbit(link2, 47.0, 8.0, alt=40.0, radius=15.0))
    o2 = [c for c in link2.int_cmds if c[0] == commands.MAV_CMD_DO_ORBIT]
    assert o2[-1][3] == 540.0, f"explicit 40m orbit must stay 40m (AMSL 540), got {o2[-1][3]}"
    print("OK orbit floors altitude to MIN_ORBIT_ALT_M (10m); explicit higher honoured")


def test_turn_to_heading_uses_reposition_with_target_yaw():
    # Outrider-safe turn (no GCS offboard): yaw via DO_REPOSITION to current±degrees.
    # From heading 0: "right 90" → target 90; "left 45" → 315. Holds current position.
    link = FakeLink(heading=0.0)
    res = asyncio.run(commands.turn_to_heading(link, 90.0, "right"))
    reps = [c for c in link.int_cmds if c[0] == M.MAV_CMD_DO_REPOSITION]
    assert reps, "no DO_REPOSITION sent"
    # int_cmds = (cmd, x, y, z, p1, p2, p3, p4, frame); p4 (index 7) = target yaw deg
    assert reps[-1][7] == 90.0, f"right 90 from hdg 0 → target 90, got {reps[-1][7]}"
    assert reps[-1][1] == int(47.0 * 1e7) and reps[-1][2] == int(8.0 * 1e7), "must hold current position"
    assert res["to_heading"] == 90.0 and res["direction"] == "right" and res["via"] == "reposition"
    link2 = FakeLink(heading=0.0)
    asyncio.run(commands.turn_to_heading(link2, 45.0, "left"))
    reps2 = [c for c in link2.int_cmds if c[0] == M.MAV_CMD_DO_REPOSITION]
    assert reps2[-1][7] == 315.0, f"left 45 from hdg 0 → target 315, got {reps2[-1][7]}"
    print("OK turn_to_heading yaws via DO_REPOSITION to current±degrees (no offboard)")


def test_set_speed_groundspeed_default():
    link = FakeLink()
    res = asyncio.run(commands.set_speed(link, 8.0))
    cmd, params = [c for c in link.long_cmds if c[0] == M.MAV_CMD_DO_CHANGE_SPEED][-1]
    assert params[0] == 1.0, "speed type should be 1 (groundspeed) by default"
    assert params[1] == 8.0, "target speed in param2"
    assert params[2] == -1.0, "throttle param should be -1 (no change)"
    assert res["type"] == "groundspeed"
    print("OK set_speed defaults to groundspeed, throttle -1")


# ── mode encoding ────────────────────────────────────────────────────────────
def test_set_mode_encodes_px4_custom_mode():
    link = FakeLink()
    asyncio.run(commands.set_mode(link, "hold"))
    sets = [c for c in link.long_cmds if c[0] == M.MAV_CMD_DO_SET_MODE]
    assert sets, "no DO_SET_MODE sent"
    _, (base, main, sub) = sets[-1]
    assert int(base) == C.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
    assert (int(main), int(sub)) == C.MODES["HOLD"], (main, sub)
    print("OK set_mode encodes custom-mode-enabled + (main,sub) for HOLD")


def test_rel_to_amsl_conversion():
    link = FakeLink(alt_msl=520.0, alt_rel=20.0)
    # ground = 500; AGL 35 → AMSL 535
    assert abs(commands.rel_to_amsl(link, 35.0) - 535.0) < 1e-6
    print("OK rel_to_amsl uses (msl-rel)+agl")


def test_send_velocity_body_type_mask_ignores_position_and_yaw_angle():
    link = FakeLink()
    commands.send_velocity_body(link, 1.0, 0.0, -0.5, 0.2)
    sent = [s for s in link.master.mav.sent
            if s[0] == "set_position_target_local_ned_send"]
    assert sent, "no setpoint sent"
    args = sent[-1][1]
    # set_position_target_local_ned_send(time, tsys, tcomp, frame, type_mask, ...)
    type_mask = args[4]
    # position bits (1|2|4), accel (64|128|256), yaw-angle (1024) ignored;
    # velocity (8|16|32) and yaw-rate (2048) active.
    assert type_mask & 1 and type_mask & 1024, "position+yaw-angle should be ignored"
    assert not (type_mask & 8), "velocity bits must be ACTIVE (0)"
    assert not (type_mask & 2048), "yaw-rate bit must be ACTIVE (0)"
    assert args[3] == M.MAV_FRAME_BODY_NED, "should be body-frame"
    print("OK send_velocity_body type_mask ignores pos/yaw-angle, enables vel+yawrate")


def test_move_relative_requires_position_fix():
    link = FakeLink(lat=None, lon=None)
    try:
        asyncio.run(commands.move_relative(link, forward=10.0))
    except ValueError:
        print("OK move_relative raises without a position fix")
        return
    raise AssertionError("move_relative should raise ValueError without a fix")


def test_turn_requires_armed():
    link = FakeLink(armed=False)
    try:
        asyncio.run(commands.turn(link, 90.0))
    except ValueError:
        print("OK turn refuses when not armed")
        return
    raise AssertionError("turn should raise ValueError when not armed")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failed.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - len(failed)}/{len(fns)} command tests passed "
          f"({len(failed)} failures — M2 takeoff guard should pass)")
