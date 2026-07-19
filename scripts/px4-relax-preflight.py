#!/usr/bin/env python3
"""Relax PX4 SITL preflight requirements that aren't meaningful in this demo.

Runs against the PX4 instances on 127.0.0.1:14540 (Overwatch) and 14541
(Outrider). Sets a handful of well-known params that gate arming in ways that
make sense for hardware but produce false negatives in an amd64-emulated SITL:

  COM_RC_IN_MODE=4     "Stick input disabled" — we have no RC channel
  NAV_RCL_ACT=0        Disable "RC loss" failsafe
  NAV_DLL_ACT=0        Disable "data link loss" failsafe (we ARE the GCS)
  COM_RCL_EXCEPT=7     Allow auto modes + offboard + missions without RC
  COM_DISARM_PRFLT=-1  Don't auto-disarm if we sit on the ground briefly
                       between arm and takeoff (otherwise PX4 disarms after
                       ~10 s under emulation-induced sensor jitter)

We do NOT disable the EKF / sensor health checks themselves — separating the
two PX4s into their own Gazebo worlds (see entrypoint.sh) is what actually
fixes the sensor TIMEOUT root cause. These params just remove unrelated
arming gates a typical SITL demo doesn't care about.

Idempotent: PARAM_SET is a no-op if the value already matches.
"""
from __future__ import annotations

import os
import sys
import time

from pymavlink import mavutil

# The MAV_PARAM_TYPE_* constants are top-level in mavlink_common (v2) but the
# default v1/ardupilotmega dialect that mavutil loads at import-time on some
# pymavlink builds doesn't re-export them. Resolve them via getattr with a
# numeric fallback so the script works regardless of dialect (the wire values
# are stable across MAVLink versions).
def _ptype(name: str, fallback: int) -> int:
    return getattr(mavutil.mavlink, name, fallback)

MAV_PARAM_TYPE_INT32 = _ptype("MAV_PARAM_TYPE_INT32", 6)
MAV_PARAM_TYPE_REAL32 = _ptype("MAV_PARAM_TYPE_REAL32", 9)

# (param_name, value, mavparam_type)
PARAMS: tuple[tuple[str, float, int], ...] = (
    ("COM_RC_IN_MODE",   4.0, MAV_PARAM_TYPE_INT32),
    ("NAV_RCL_ACT",      0.0, MAV_PARAM_TYPE_INT32),
    ("NAV_DLL_ACT",      0.0, MAV_PARAM_TYPE_INT32),
    ("COM_RCL_EXCEPT",   7.0, MAV_PARAM_TYPE_INT32),
    ("COM_DISARM_PRFLT", -1.0, MAV_PARAM_TYPE_REAL32),
    # COM_DISARM_LAND default is 2.0s — PX4 disarms when it thinks the drone
    # has been "landed" for >2s. On amd64-emulated SITL the land detector is
    # over-eager: ARM → 2s pass while commands.takeoff() sleeps + computes the
    # AMSL target → PX4 disarms → NAV_TAKEOFF arrives at a disarmed drone →
    # silently ignored → "drone accepts but doesn't respond" (2026-06-01).
    # -1 disables; we still keep the manual /api/command/disarm path.
    ("COM_DISARM_LAND",  -1.0, MAV_PARAM_TYPE_REAL32),

    # ── Sensor-health relaxations (2026-06-01) ─────────────────────────────
    # PX4 was returning "Arming denied: Resolve system health failures first"
    # in MANUAL mode and silently failing in HOLD (ACCEPTED ack but no actual
    # arm) under Cloud Run with cpu=4. Root: sensors_status_imu watchdog
    # ("Accel #0 fail: TIMEOUT!") fires when the Gazebo-published IMU stream
    # arrives >100 ms late, even with each PX4 in its own gz partition. This
    # is the SAME sensor flap discussed in entrypoint.sh's per-world comment;
    # the isolation reduces it but doesn't fully eliminate it.
    #
    # COM_PREARM_MODE=0 — disable continuous prearm sensor check (the gate
    #   that produces the "system health failures first" rejection).
    ("COM_PREARM_MODE",  0.0, MAV_PARAM_TYPE_INT32),
    # CBRK_* circuit breakers — well-known SITL bypasses. These specific
    # magic numbers are the PX4 secret-handshake to acknowledge "yes I know
    # I'm disabling this check, don't fly a real drone with this".
    ("CBRK_SUPPLY_CHK",  894281.0, MAV_PARAM_TYPE_INT32),  # power supply
    ("CBRK_USB_CHK",     197848.0, MAV_PARAM_TYPE_INT32),  # USB cable plugged in
    ("CBRK_IO_SAFETY",    22027.0, MAV_PARAM_TYPE_INT32),  # IO safety switch
    # (CBRK_AIRSPD_CHK is fixed-wing only; multicopter PX4 rejects with
    # "unknown param" — removed.)
    # SDLOG_MODE=0 — don't try to log to (non-existent) SD card.
    ("SDLOG_MODE",       0.0, MAV_PARAM_TYPE_INT32),
    # MIS_TAKEOFF_ALT — default takeoff altitude when no NAV_TAKEOFF target
    # is given. 15m is a sane demo default (was 2.5m PX4-default).
    ("MIS_TAKEOFF_ALT", 15.0, MAV_PARAM_TYPE_REAL32),

    # ── Battery / power checks (drained after ~30 min of running) ──────────
    # gz_x500 has a simulated battery that depletes based on motor usage.
    # After ~30+ min of arm/disarm cycles the simulated voltage drops below
    # BAT_LOW_THR, PX4 declares "Battery unhealthy" preflight fail, and
    # arming is refused — exactly the kind of false-positive we keep hitting
    # in this demo (2026-06-01). Zero the thresholds + flip the circuit
    # breaker so PX4 stops gating on simulated power.
    ("BAT_LOW_THR",       0.0,    MAV_PARAM_TYPE_REAL32),
    ("BAT_CRIT_THR",      0.0,    MAV_PARAM_TYPE_REAL32),
    ("BAT_EMERGEN_THR",   0.0,    MAV_PARAM_TYPE_REAL32),
    # COM_LOW_BAT_ACT=0 — even if battery does trigger, do nothing (no RTL).
    ("COM_LOW_BAT_ACT",   0.0,    MAV_PARAM_TYPE_INT32),
    # CBRK_VOLTAGE_CHK — disable the system-voltage check (separate from
    # battery thresholds). Magic number per PX4 source.
    ("CBRK_VOLTAGE_CHK",  284953.0, MAV_PARAM_TYPE_INT32),
)

# pymavlink expects a different source sysid from PX4's autopilot (1, 2) and
# from the GCS backend's own heartbeat (255). 252 gives us a unique slot.
SRC_SYS = 252


def relax(connection: str, label: str, deadline_s: float = 90.0) -> bool:
    """Wait for PX4 HEARTBEAT on the bound port, then push every param."""
    print(f"[relax-preflight] {label}: binding {connection}", flush=True)
    m = mavutil.mavlink_connection(connection, source_system=SRC_SYS)

    # For udpin: we have to RECEIVE a packet before mavutil knows who to send
    # back to. PX4's Normal mavlink instance emits HEARTBEAT at 1 Hz to its
    # configured QGC remote (which is our bind port), so the first wait_heartbeat
    # call latches our peer automatically.
    # Filter for PX4's autopilot heartbeat (sysid 1 or 2). Reject:
    #   - sys 0     : malformed
    #   - sys SRC_SYS (252) : our own echoes
    #   - sys 255   : the GCS backend's own heartbeat (would route PARAM_SET to
    #                 the GCS, not PX4 — 0/N acked, silent failure).
    # 2026-06-01: encountered when running this script post-boot while the
    # GCS backend was already heartbeating on the same port.
    deadline = time.time() + deadline_s
    hb = None
    while time.time() < deadline:
        hb = m.recv_match(type="HEARTBEAT", blocking=True, timeout=2.0)
        if hb is not None and hb.get_srcSystem() not in (0, SRC_SYS, 255):
            break
    if hb is None or hb.get_srcSystem() in (0, SRC_SYS, 255):
        print(f"[relax-preflight] {label}: TIMEOUT — no autopilot heartbeat", flush=True)
        return False

    # Now that mavutil has a remote peer, send our GCS heartbeat so PX4 stops
    # complaining "No connection to the GCS" on the Normal link.
    m.mav.heartbeat_send(
        mavutil.mavlink.MAV_TYPE_GCS,
        mavutil.mavlink.MAV_AUTOPILOT_INVALID,
        0, 0, 0,
    )

    target_sys = hb.get_srcSystem()
    target_comp = hb.get_srcComponent()
    print(
        f"[relax-preflight] {label}: heartbeat from sys={target_sys} comp={target_comp}",
        flush=True,
    )

    ok_count = 0
    for name, value, ptype in PARAMS:
        m.mav.param_set_send(
            target_sys, target_comp,
            name.encode("ascii"),
            value, ptype,
        )
        # Wait for the matching PARAM_VALUE echo as ack.
        end = time.time() + 5.0
        acked = False
        while time.time() < end:
            msg = m.recv_match(type="PARAM_VALUE", blocking=True, timeout=1.0)
            if msg is None:
                continue
            try:
                pname = msg.param_id.rstrip("\x00") if isinstance(msg.param_id, str) \
                        else msg.param_id.decode().rstrip("\x00")
            except Exception:
                continue
            if pname == name:
                print(
                    f"[relax-preflight] {label}:   set {name}={value}  (ack: {msg.param_value})",
                    flush=True,
                )
                acked = True
                ok_count += 1
                break
        if not acked:
            print(f"[relax-preflight] {label}:   set {name}={value}  (no PARAM_VALUE ack)", flush=True)

    m.close()
    print(f"[relax-preflight] {label}: done ({ok_count}/{len(PARAMS)} acked)", flush=True)
    return ok_count > 0


def main() -> int:
    # PX4's "Normal" MAVLink instance (the would-be QGroundControl link) is
    # configured to send HEARTBEAT/STATUSTEXT to 127.0.0.1:14550 — and the demo
    # has NOTHING bound there. We claim those slots: 14550 for Overwatch
    # (PX4 i=0), 14551 for Outrider (PX4 i=1). PX4 derives the QGC remote port
    # as 14550 + instance_id, same convention as the Onboard 14540+i.
    #
    # We bind via mavutil `udpin:` so we just listen + reply to PX4's source
    # (its 18570/18571 socket). Doing it this way means we never collide with
    # the GCS backend on 14540/14541 (which is `udpin` too — the OS won't let
    # two processes bind the same port).
    targets = [
        ("udpin:127.0.0.1:14550", "overwatch"),
        ("udpin:127.0.0.1:14551", "outrider"),
    ]
    rc = 0
    for conn, label in targets:
        try:
            if not relax(conn, label):
                rc = 1
        except Exception as exc:  # noqa: BLE001
            print(f"[relax-preflight] {label}: error {exc}", flush=True)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
