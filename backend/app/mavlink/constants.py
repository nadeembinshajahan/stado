"""PX4 custom-mode encoding and helpers.

PX4 packs its flight mode into the HEARTBEAT.custom_mode field:

    bits  0-15 : reserved
    bits 16-23 : main mode
    bits 24-31 : sub mode (only meaningful for AUTO)

To *command* a mode we send MAV_CMD_DO_SET_MODE with:
    param1 = base_mode  (must include CUSTOM_MODE_ENABLED)
    param2 = main mode
    param3 = sub mode
"""
from __future__ import annotations

# PX4 main modes
MAIN_MANUAL = 1
MAIN_ALTCTL = 2
MAIN_POSCTL = 3
MAIN_AUTO = 4
MAIN_ACRO = 5
MAIN_OFFBOARD = 6
MAIN_STABILIZED = 7

# PX4 AUTO sub modes
SUB_AUTO_READY = 1
SUB_AUTO_TAKEOFF = 2
SUB_AUTO_LOITER = 3
SUB_AUTO_MISSION = 4
SUB_AUTO_RTL = 5
SUB_AUTO_LAND = 6
SUB_AUTO_FOLLOW_TARGET = 8
SUB_AUTO_PRECLAND = 9

MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 1

# (main, sub) tuples for the modes the GCS commands.
MODES: dict[str, tuple[int, int]] = {
    "MANUAL": (MAIN_MANUAL, 0),
    "ALTITUDE": (MAIN_ALTCTL, 0),
    "POSITION": (MAIN_POSCTL, 0),
    "OFFBOARD": (MAIN_OFFBOARD, 0),
    "STABILIZED": (MAIN_STABILIZED, 0),
    "ACRO": (MAIN_ACRO, 0),
    "HOLD": (MAIN_AUTO, SUB_AUTO_LOITER),
    "TAKEOFF": (MAIN_AUTO, SUB_AUTO_TAKEOFF),
    "MISSION": (MAIN_AUTO, SUB_AUTO_MISSION),
    "RTL": (MAIN_AUTO, SUB_AUTO_RTL),
    "LAND": (MAIN_AUTO, SUB_AUTO_LAND),
    "FOLLOW": (MAIN_AUTO, SUB_AUTO_FOLLOW_TARGET),
}

# Reverse lookup for decoding HEARTBEAT.custom_mode -> human name.
_AUTO_SUB_NAMES = {
    SUB_AUTO_READY: "READY",
    SUB_AUTO_TAKEOFF: "TAKEOFF",
    SUB_AUTO_LOITER: "HOLD",
    SUB_AUTO_MISSION: "MISSION",
    SUB_AUTO_RTL: "RTL",
    SUB_AUTO_LAND: "LAND",
    SUB_AUTO_FOLLOW_TARGET: "FOLLOW",
    SUB_AUTO_PRECLAND: "PRECLAND",
}
_MAIN_NAMES = {
    MAIN_MANUAL: "MANUAL",
    MAIN_ALTCTL: "ALTITUDE",
    MAIN_POSCTL: "POSITION",
    MAIN_ACRO: "ACRO",
    MAIN_OFFBOARD: "OFFBOARD",
    MAIN_STABILIZED: "STABILIZED",
}


def decode_px4_mode(custom_mode: int) -> str:
    main = (custom_mode >> 16) & 0xFF
    sub = (custom_mode >> 24) & 0xFF
    if main == MAIN_AUTO:
        return _AUTO_SUB_NAMES.get(sub, f"AUTO:{sub}")
    return _MAIN_NAMES.get(main, f"MODE:{main}")
