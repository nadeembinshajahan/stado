"""Demo-only patch for backend/app/mavlink/commands.py — set_mode TAKEOFF before arm.

Why: on this PX4 SITL build, MAV_CMD_NAV_TAKEOFF does NOT auto-switch the
flight mode from HOLD. The drone arms in HOLD, sits idle, and PX4's
COM_DISARM_PRFLT auto-disarms after 10s before our NAV_TAKEOFF takes effect.

Confirmed via PX4 log:
    08:54:07  Armed by external command
    08:54:18  Disarmed by auto preflight disarming   (~11s — default PRFLT)

A direct `set_mode("TAKEOFF") → arm` flow does work:
    08:56:28  Armed by external command
    08:56:28  Using default takeoff altitude: 2.5 m  (← proves AUTO.TAKEOFF mode)
    08:56:30  Takeoff detected

This patch makes commands.takeoff() do that instead of the upstream
arm → sleep(1.0) → NAV_TAKEOFF sequence.

Run inside the Docker build with CWD = /app/backend.
"""
from __future__ import annotations

import pathlib
import sys

p = pathlib.Path("app/mavlink/commands.py")
src = p.read_text()

OLD = (
    '    res = await arm(link)\n'
    '    if not res.get("ok"):\n'
    '        # Arming was denied/rejected/timed out — do not launch. Surface the\n'
    '        # arm failure verbatim (its keys already match the contract).\n'
    '        return {**res, "altitude": altitude}\n'
    '\n'
    '    await asyncio.sleep(1.0)\n'
)

NEW = (
    '    # DEMO PATCH: switch to TAKEOFF mode BEFORE arming. On this PX4 SITL\n'
    '    # build, NAV_TAKEOFF does NOT auto-switch the mode, so the drone arms\n'
    '    # in HOLD, sits idle, COM_DISARM_PRFLT fires after 10s.\n'
    '    # set_mode first → arm → drone climbs immediately.\n'
    '    await set_mode(link, "TAKEOFF")\n'
    '    await asyncio.sleep(0.2)\n'
    '\n'
    '    res = await arm(link)\n'
    '    if not res.get("ok"):\n'
    '        # Arming was denied/rejected/timed out — do not launch. Surface\n'
    '        # the arm failure verbatim (its keys already match the contract).\n'
    '        # Best-effort revert to HOLD so the drone is in a sane state.\n'
    '        try:\n'
    '            await set_mode(link, "HOLD")\n'
    '        except Exception:\n'
    '            pass\n'
    '        return {**res, "altitude": altitude}\n'
    '\n'
    '    await asyncio.sleep(0.2)\n'
)

# Idempotency FIRST (the stado-demo original checked OLD first, which made
# re-runs exit 1 after a successful patch — fixed in this copy).
if NEW.strip().split("\n")[0] in src:
    print("patch_commands_takeoff.py: already patched — no-op")
    sys.exit(0)

if OLD not in src:
    sys.stderr.write(
        "patch_commands_takeoff.py: target block not found in commands.py — "
        "upstream may have changed. Bail.\n"
    )
    sys.exit(1)

p.write_text(src.replace(OLD, NEW, 1))
print("patch_commands_takeoff.py: patched commands.takeoff() — set_mode TAKEOFF before arm")
