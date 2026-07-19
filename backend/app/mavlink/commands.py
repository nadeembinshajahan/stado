from __future__ import annotations

import asyncio
import logging
import math

from pymavlink import mavutil

from .. import safety
from . import constants as C
from .link import MavlinkLink

log = logging.getLogger(__name__)

ARM_FORCE_MAGIC = 21196  # PX4/ArduPilot "force" param2 for arm/disarm
# Max plausible on-ground relative altitude (m). The vehicle reads alt_rel ≈ 0 on the
# ground; more than this means PX4's home/EKF altitude reference is wrong, which
# silently corrupts the takeoff target (see commands.takeoff — 2026-05-26 incident).
HOME_ALT_TOLERANCE_M = 1.5
# Not present in every pymavlink dialect; it's a standard MAVLink command id.
MAV_CMD_DO_ORBIT = getattr(mavutil.mavlink, "MAV_CMD_DO_ORBIT", 34)
# Minimum orbit altitude (m above launch). A spoken "orbit X" with no altitude
# defaults to the drone's CURRENT altitude — so if the vehicle was sagging (e.g.
# in POSITION mode under low RC throttle), the orbit inherited that low value and
# had no floor to hold it up. On 2026-05-27 Overwatch was told to orbit point A;
# it had drifted down and the orbit rode it from 10 m all the way below the launch
# point. Floor every orbit here so it never circles dangerously low / near ground.
MIN_ORBIT_ALT_M = 10.0


def _mode(link: MavlinkLink, name: str) -> None:
    main, sub = C.MODES[name]
    link.set_mode(C.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, main, sub)


async def set_mode(link: MavlinkLink, name: str) -> None:
    _mode(link, name.upper())


async def _arm_disarm(link: MavlinkLink, arm: bool, force: bool, timeout: float) -> dict:
    """Send MAV_CMD_COMPONENT_ARM_DISARM and wait for its COMMAND_ACK so the
    caller learns the REAL outcome instead of assuming success. PX4 will DENY /
    TEMPORARILY_REJECT arming on a failed preflight/precondition check (e.g. no
    GPS lock indoors) and emit a STATUSTEXT like 'Arming denied: …'; we surface
    that text as the reason. Returns:
        {ok, armed, result, result_name, reason, statustexts}
    `ok` is True ONLY on MAV_RESULT_ACCEPTED. On timeout (no ACK), ok=False with
    reason 'no arm acknowledgement'. The blocking ACK wait runs in a worker
    thread so the event loop / link reader thread are never blocked."""
    ack = await asyncio.to_thread(
        link.command_long_ack,
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM,
        1.0 if arm else 0.0,
        ARM_FORCE_MAGIC if force else 0.0,
        timeout=timeout,
    )
    verb = "arm" if arm else "disarm"
    if ack["result"] is None:
        reason = f"no {verb} acknowledgement"
    elif ack["accepted"]:
        reason = None
    else:
        reason = ack["reason"] or f"{verb} {ack['result_name'] or ack['result']}"
    return {
        "ok": bool(ack["accepted"]),
        "armed": arm if ack["accepted"] else None,
        "result": ack["result"],
        "result_name": ack["result_name"],
        "reason": reason,
        "statustexts": ack["statustexts"],
    }


async def arm(link: MavlinkLink, force: bool = False, timeout: float = 3.0) -> dict:
    return await _arm_disarm(link, arm=True, force=force, timeout=timeout)


async def disarm(link: MavlinkLink, force: bool = False, timeout: float = 3.0) -> dict:
    return await _arm_disarm(link, arm=False, force=force, timeout=timeout)


async def takeoff(link: MavlinkLink, altitude: float = 10.0, override: bool = False) -> dict:
    """Arm, then climb to `altitude` metres ABOVE the launch point (AGL).

    SAFETY: enforces the runtime max-altitude CEILING first — if `altitude`
    exceeds it and `override` is False this RAISES safety.CeilingExceeded BEFORE
    arming (nothing is sent). override=True bypasses + audits it.

    PX4's MAV_CMD_NAV_TAKEOFF altitude (param7) is AMSL, so we add the current
    ground altitude — otherwise e.g. asking for 20 m at a 900 m-elevation site
    commands 20 m AMSL (underground) and the vehicle won't take off.

    Gated on arming: arm() now reports the REAL outcome via its COMMAND_ACK, so
    if arming is DENIED/TEMPORARILY_REJECTED/times out we DO NOT send NAV_TAKEOFF
    (which PX4 would silently reject) and instead RETURN that failure (M2).

    Returns a dict shaped like arm() so the API/voice layer can report the real
    outcome (THIS IS A CONTRACT the API layer depends on):
        {ok, armed, result, result_name, reason, statustexts, altitude}
    On an arm failure, `ok` is False and `reason` is the arm rejection cause; on
    success `ok` is True, `armed` is True and `altitude` is the requested AGL.
    """
    # Refuse a too-high takeoff BEFORE arming so we never spin motors for a climb
    # we won't command. Raises safety.CeilingExceeded (no silent clamp).
    safety.check_altitude(altitude, override=override, context="takeoff")

    # HOME/ALTITUDE PLAUSIBILITY GATE (2026-05-26 field incident). On the ground the
    # vehicle reads alt_rel ≈ 0; a large value means PX4's home/EKF altitude is OFF
    # (incident: home latched ~2.8 m below ground → alt_rel read +2.78). PX4's
    # takeoff.cpp then builds a corrupted AMSL target, its "don't descend on takeoff"
    # clamp fires, AUTO.TAKEOFF "completes" with ZERO climb (armed, idle, no liftoff),
    # and the land detector can't auto-disarm → battery pull. Refuse BEFORE arming
    # (fail-safe: nothing sent, no motors spin). NOT bypassed by `override` (that's
    # only the altitude ceiling) — this is a hard data-integrity stop.
    #
    # RUNTIME KILL SWITCH (added 2026-07-03 at flight test — Outrider baro drifted
    # to alt_rel≈−4.3 m on the ground and the gate was refusing takeoff). Set env
    # DISABLE_HOME_ALT_GATE=1 to bypass. LEAVE UNSET IN NORMAL OPERATION — the gate
    # protects against the exact stuck-armed failure mode above. When bypassed we
    # still log a WARNING carrying the drift so the audit trail records the risk.
    import os as _os
    _bypass_home_gate = _os.environ.get("DISABLE_HOME_ALT_GATE", "").lower() in (
        "1", "true", "yes",
    )
    on_ground_rel = link.snapshot().get("alt_rel")
    if on_ground_rel is not None and abs(on_ground_rel) > HOME_ALT_TOLERANCE_M:
        if _bypass_home_gate:
            log.warning(
                "home-alt gate BYPASSED (DISABLE_HOME_ALT_GATE=1): on-ground alt_rel=%.2f m "
                "(drift ~%.1f m). PX4 may arm but not climb (2026-05-26 failure mode). "
                "Watch for zero-climb + force-disarm ready.",
                on_ground_rel, abs(on_ground_rel),
            )
        else:
            return {
                "ok": False, "armed": False, "result": None, "result_name": None,
                "reason": (
                    f"altitude reference is off — reads {on_ground_rel:.1f} m on the ground "
                    f"(home/EKF wrong by ~{abs(on_ground_rel):.1f} m). PX4 would arm but NOT climb "
                    f"with this (corrupted takeoff target -> clamp -> zero climb, then it can't "
                    f"auto-disarm). Fix it first: power-cycle, or wait for GPS/RTK to settle until "
                    f"the ground reads ~0 m, or re-set home. Safety stop -- nothing armed."
                ),
                "statustexts": [], "altitude": altitude,
            }

    # DEMO PATCH: switch to TAKEOFF mode BEFORE arming. On this PX4 SITL
    # build, NAV_TAKEOFF does NOT auto-switch the mode, so the drone arms
    # in HOLD, sits idle, COM_DISARM_PRFLT fires after 10s.
    # set_mode first → arm → drone climbs immediately.
    await set_mode(link, "TAKEOFF")
    await asyncio.sleep(0.2)

    res = await arm(link)
    if not res.get("ok"):
        # Arming was denied/rejected/timed out — do not launch. Surface
        # the arm failure verbatim (its keys already match the contract).
        # Best-effort revert to HOLD so the drone is in a sane state.
        try:
            await set_mode(link, "HOLD")
        except Exception:
            pass
        return {**res, "altitude": altitude}

    await asyncio.sleep(0.2)
    ground_amsl = link.snapshot().get("alt_msl")
    if ground_amsl is None:
        # No global-position altitude → we can't build a safe AMSL target (the old
        # raw-AGL fallback is UNDERGROUND at an elevated site → PX4 won't climb).
        # Disarm what we just armed and refuse rather than fire a bad takeoff.
        await disarm(link)
        return {
            "ok": False, "armed": False, "result": None, "result_name": None,
            "reason": ("no global-position altitude after arming — can't compute a safe takeoff "
                       "target; disarmed. Wait for a GPS fix."),
            "statustexts": [], "altitude": altitude,
        }
    target_amsl = ground_amsl + altitude
    link.command_long(
        mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
        0.0,  # pitch
        0.0,
        0.0,
        float("nan"),  # yaw — keep current
        float("nan"),  # lat — current
        float("nan"),  # lon — current
        float(target_amsl),
    )
    return {
        "ok": True,
        "armed": True,
        "result": res.get("result"),
        "result_name": res.get("result_name"),
        "reason": None,
        "statustexts": res.get("statustexts", []),
        "altitude": altitude,
    }


async def _mode_reliable(link: MavlinkLink, name: str, attempts: int = 3, gap: float = 1.2) -> None:
    """Set a flight mode, resending ONLY until it takes. set_mode is fire-and-forget
    and PX4 can reject a switch while the vehicle is still settling — the bug where a
    drone ignored repeated LAND/RTL. But blindly resending floods the console with
    duplicate ACKs, so after each send we wait and check telemetry: as soon as the
    mode actually changes we stop (common case = a single command + ACK)."""
    target = name.upper()
    for i in range(attempts):
        _mode(link, name)
        if i == attempts - 1:
            break
        await asyncio.sleep(gap)
        if target in (link.snapshot().get("mode") or "").upper():
            return  # mode took — don't resend (avoids duplicate commands + ACKs)


async def land(link: MavlinkLink) -> None:
    await _mode_reliable(link, "LAND")


async def rtl(link: MavlinkLink) -> None:
    await _mode_reliable(link, "RTL")


async def hold(link: MavlinkLink) -> None:
    """Hold/Loiter — PX4 stops and holds current position (acts as brake)."""
    _mode(link, "HOLD")


# Brake on PX4 is equivalent to switching to Hold: it arrests motion and
# holds position. Exposed separately for the voice/command vocabulary.
brake = hold


def rel_to_amsl(link: MavlinkLink, rel_alt: float) -> float:
    """Convert an altitude above the launch point (AGL) to AMSL, which is what
    PX4's reposition/orbit/takeoff commands actually use. ground = msl - rel."""
    s = link.snapshot()
    if s.get("alt_msl") is not None and s.get("alt_rel") is not None:
        return (s["alt_msl"] - s["alt_rel"]) + rel_alt
    return rel_alt


async def goto(
    link: MavlinkLink, lat: float, lon: float, alt: float, speed: float = -1.0,
    override: bool = False,
) -> None:
    """Fly to a location (PX4 DO_REPOSITION). `alt` is metres above launch (AGL);
    sent as AMSL in the GLOBAL_INT frame. Vehicle should be in Hold/Position.

    SAFETY: enforces the runtime max-altitude CEILING — raises
    safety.CeilingExceeded if `alt` exceeds it and `override` is False (no clamp)."""
    safety.check_altitude(alt, override=override, context="goto")
    link.command_int(
        mavutil.mavlink.MAV_CMD_DO_REPOSITION,
        int(lat * 1e7),
        int(lon * 1e7),
        rel_to_amsl(link, alt),
        p1=float(speed),
        p2=float(mavutil.mavlink.MAV_DO_REPOSITION_FLAGS_CHANGE_MODE),
        p4=float("nan"),  # yaw
        frame=mavutil.mavlink.MAV_FRAME_GLOBAL_INT,
    )


async def orbit(
    link: MavlinkLink,
    lat: float,
    lon: float,
    alt: float,
    radius: float = 20.0,
    velocity: float = 3.0,
    clockwise: bool = True,
    override: bool = False,
) -> None:
    """Circle a point (PX4 MAV_CMD_DO_ORBIT). Negative radius = counter-clockwise.

    SAFETY: enforces the runtime max-altitude CEILING — raises
    safety.CeilingExceeded if `alt` exceeds it and `override` is False (no clamp).

    Resent a few times: command_int is fire-and-forget, and PX4 will
    TEMPORARILY_REJECT DO_ORBIT when the vehicle is still settling from a prior
    reposition/goto — which silently leaves it in HOLD instead of orbiting (the
    bug where the 2nd drone, still in transit to the point, never entered Orbit
    and so didn't face the centre). Once a drone is orbiting, repeats with the
    same params are idempotent, so re-sending reliably establishes the orbit on
    both drones."""
    # Floor the orbit altitude (2026-05-27 incident: an orbit inherited a sagging
    # ~current altitude and rode the vehicle below its launch point). Never circle
    # below MIN_ORBIT_ALT_M; an explicit higher request is honoured as-is.
    if alt < MIN_ORBIT_ALT_M:
        log.info("orbit altitude %.1fm below floor — raising to %.1fm", alt, MIN_ORBIT_ALT_M)
        alt = MIN_ORBIT_ALT_M
    safety.check_altitude(alt, override=override, context="orbit")
    signed_radius = abs(radius) if clockwise else -abs(radius)
    amsl = rel_to_amsl(link, alt)
    for attempt in range(3):
        link.command_int(
            MAV_CMD_DO_ORBIT,
            int(lat * 1e7),
            int(lon * 1e7),
            amsl,
            p1=float(signed_radius),
            p2=float(velocity),
            p3=0.0,  # yaw behaviour: face the centre
            p4=float("nan"),  # number of orbits — unlimited
            frame=mavutil.mavlink.MAV_FRAME_GLOBAL_INT,
        )
        if attempt < 2:
            await asyncio.sleep(1.3)


async def set_home(link: MavlinkLink, lat: float, lon: float, alt: float | None = None) -> dict:
    """Set the vehicle's home position to a specified location (MAV_CMD_DO_SET_HOME).
    param1 = 0 means use the lat/lon/alt provided (NOT the current position).

    The home ALTITUDE matters: PX4 reports relative altitude as (AMSL - home_alt),
    so a home alt of 0 (sea level) at an elevated site makes alt_rel read ~hundreds
    of metres high. When the caller doesn't specify alt (the map-click "Set Home"
    only has lat/lon), estimate the ground AMSL at the site as the vehicle's current
    (alt_msl - alt_rel) so relative altitude stays correct."""
    if alt is None:
        s = link.snapshot()
        if s.get("alt_msl") is not None and s.get("alt_rel") is not None:
            alt = s["alt_msl"] - s["alt_rel"]  # ground AMSL under the vehicle
        else:
            alt = 0.0
    link.command_int(
        mavutil.mavlink.MAV_CMD_DO_SET_HOME,
        int(lat * 1e7),
        int(lon * 1e7),
        float(alt),
        p1=0.0,  # 0 = use the specified location, not current position
        frame=mavutil.mavlink.MAV_FRAME_GLOBAL,
    )
    return {"lat": lat, "lon": lon, "alt": round(float(alt), 1)}


async def set_speed(link: MavlinkLink, speed_ms: float, airspeed: bool = False) -> dict:
    """Set the vehicle's cruise speed (MAV_CMD_DO_CHANGE_SPEED) — applies to
    subsequent goto/move/mission legs. speed_type 1 = ground speed (default),
    0 = air speed; throttle param -1 = leave unchanged."""
    link.command_long(
        mavutil.mavlink.MAV_CMD_DO_CHANGE_SPEED,
        0.0 if airspeed else 1.0,  # speed type
        float(speed_ms),           # target speed, m/s
        -1.0,                      # throttle %, -1 = no change
        0.0, 0.0, 0.0, 0.0,
    )
    return {"speed_ms": round(float(speed_ms), 1), "type": "airspeed" if airspeed else "groundspeed"}


def send_velocity_body(
    link: MavlinkLink, vx: float, vy: float, vz: float, yaw_rate: float
) -> None:
    """Stream a body-frame velocity setpoint (PX4 offboard follow)."""
    # Ignore position (1|2|4), acceleration (64|128|256) and yaw angle (1024);
    # leave velocity bits (8|16|32) and yaw-rate (2048) active.
    type_mask = (1 | 2 | 4) | (64 | 128 | 256) | 1024
    tsys, tcomp = link._target()  # noqa: SLF001 — atomic (system, component) snapshot (M1)
    with link._send_lock:  # noqa: SLF001
        # Snapshot master under the send lock so a reconnect swap can't hand us a
        # closed object mid-send (M1).
        m = link.master
        if m is None:
            return
        m.mav.set_position_target_local_ned_send(
            0, tsys, tcomp,
            mavutil.mavlink.MAV_FRAME_BODY_NED, type_mask,
            0, 0, 0, vx, vy, vz, 0, 0, 0, 0, yaw_rate,
        )


async def start_offboard(link: MavlinkLink) -> None:
    """Prime offboard: stream a few zero setpoints, then switch mode + arm."""
    for _ in range(10):
        send_velocity_body(link, 0, 0, 0, 0)
        await asyncio.sleep(0.05)
    await set_mode(link, "OFFBOARD")
    await arm(link)


def haversine_offset(lat: float, lon: float, north_m: float, east_m: float) -> tuple[float, float]:
    """Offset a lat/lon by metres (north, east). Used by follow/orbit geolocation."""
    r = 6378137.0
    dlat = north_m / r
    dlon = east_m / (r * math.cos(math.radians(lat)))
    return lat + math.degrees(dlat), lon + math.degrees(dlon)


async def move_relative(
    link: MavlinkLink,
    forward: float = 0.0,
    right: float = 0.0,
    up: float = 0.0,
    speed: float = -1.0,
    override: bool = False,
) -> dict:
    """Reposition relative to the current pose: forward/right are body-frame
    metres (forward = along current heading), up is metres of climb. Combines
    freely (e.g. forward 50 + up 20). Uses DO_REPOSITION, so the vehicle must be
    flying in a position-controlled mode (HOLD/POSITION).

    SAFETY: the DERIVED target altitude (current AGL + up) is ceiling-checked via
    goto — a climb above the ceiling is refused unless `override`."""
    s = link.snapshot()
    if s.get("lat") is None or s.get("lon") is None:
        raise ValueError("no position fix")
    hdg = math.radians(s.get("heading") or 0.0)
    north = forward * math.cos(hdg) - right * math.sin(hdg)
    east = forward * math.sin(hdg) + right * math.cos(hdg)
    nlat, nlon = haversine_offset(s["lat"], s["lon"], north, east)
    nalt = max((s.get("alt_rel") or 0.0) + up, 1.0)
    await goto(link, nlat, nlon, nalt, speed, override=override)
    return {"target": [round(nlat, 7), round(nlon, 7), round(nalt, 1)]}


async def turn(link: MavlinkLink, degrees: float, direction: str = "left") -> dict:
    """Yaw the vehicle in place by `degrees` ('left' = CCW, 'right' = CW).

    Done with an OFFBOARD body-frame yaw-RATE setpoint rather than a DO_REPOSITION
    target heading: the rotation direction is then guaranteed by the SIGN of the
    yaw rate (+ = clockwise/right, − = counter-clockwise/left in BODY_NED), instead
    of depending on PX4's shortest-path-to-heading choice (which made every turn go
    the same way). Position is held (zero velocity) throughout, then we return to
    HOLD so offboard doesn't time out into a failsafe."""
    s = link.snapshot()
    if not s.get("connected"):
        raise ValueError("not connected")
    if not s.get("armed"):
        raise ValueError("vehicle must be armed and airborne to turn")

    hdg = s.get("heading") or 0.0
    deg = abs(float(degrees))
    right = not str(direction).lower().startswith("l")  # left only on an explicit 'l…'
    rate = math.radians(40.0)  # ~40°/s — brisk but stable
    yaw_rate = rate if right else -rate
    steps = max(1, round((math.radians(deg) / rate) / 0.05))  # 20 Hz stream

    # Prime offboard with a few zero setpoints, switch mode (do NOT arm — must
    # already be flying), stream the yaw-rate, then settle and return to HOLD.
    for _ in range(8):
        send_velocity_body(link, 0.0, 0.0, 0.0, 0.0)
        await asyncio.sleep(0.05)
    await set_mode(link, "OFFBOARD")
    for _ in range(steps):
        send_velocity_body(link, 0.0, 0.0, 0.0, yaw_rate)
        await asyncio.sleep(0.05)
    for _ in range(6):
        send_velocity_body(link, 0.0, 0.0, 0.0, 0.0)
        await asyncio.sleep(0.05)
    await hold(link)

    target = (hdg + (deg if right else -deg)) % 360.0
    return {"from_heading": round(hdg, 1), "to_heading": round(target, 1),
            "direction": "right" if right else "left"}


async def turn_to_heading(
    link: MavlinkLink, degrees: float, direction: str = "left",
) -> dict:
    """Yaw in place by `degrees` via DO_REPOSITION to an ABSOLUTE target heading
    (current ± degrees), holding position + altitude. This is the turn for vehicles
    WITHOUT GCS offboard (Outrider over the DDS bridge), where the offboard yaw-rate
    `turn()` can't run — it would switch the drone into OFFBOARD with no setpoint
    stream and strand it. DO_REPOSITION rides the same vehicle_command path as
    goto/orbit, so it works over DDS.

    DIRECTION CAVEAT: PX4 takes the SHORTEST path to the target heading, so the
    rotation direction is guaranteed only for |degrees| <= 180 (covers the common
    'turn right/left 45/90'); a larger turn takes the short way round. Resent a few
    times because command_int is fire-and-forget and DDS can drop a packet."""
    s = link.snapshot()
    if not s.get("connected"):
        raise ValueError("not connected")
    if not s.get("armed"):
        raise ValueError("vehicle must be armed and airborne to turn")
    if s.get("lat") is None or s.get("lon") is None:
        raise ValueError("no position fix")
    hdg = s.get("heading") or 0.0
    deg = abs(float(degrees))
    right = not str(direction).lower().startswith("l")  # left only on an explicit 'l…'
    target = (hdg + (deg if right else -deg)) % 360.0
    amsl = rel_to_amsl(link, max(s.get("alt_rel") or 0.0, 1.0))
    for attempt in range(3):
        link.command_int(
            mavutil.mavlink.MAV_CMD_DO_REPOSITION,
            int(s["lat"] * 1e7),
            int(s["lon"] * 1e7),
            amsl,
            p1=-1.0,  # default speed
            p2=float(mavutil.mavlink.MAV_DO_REPOSITION_FLAGS_CHANGE_MODE),
            p4=float(target),  # target yaw heading, degrees (PX4 converts to rad)
            frame=mavutil.mavlink.MAV_FRAME_GLOBAL_INT,
        )
        if attempt < 2:
            await asyncio.sleep(0.8)
    return {"from_heading": round(hdg, 1), "to_heading": round(target, 1),
            "direction": "right" if right else "left", "via": "reposition"}
