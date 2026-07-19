"""Action-completion monitors.

When STADO issues a flight command, the operator wants to hear "…complete" ONLY
when the action has actually finished — not a premature acknowledgement. Each
function here spawns a fire-and-forget asyncio task that polls the vehicle's
telemetry until the action's completion condition holds, then calls `notify`
(which queues a spoken voice event). Tasks self-time-out so they never hang, and
never block the command path. Per-drone by construction; the voice layer batches
simultaneous completions into one spoken turn so "both drones …" reads naturally.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Callable

from .mavlink.link import MavlinkLink

log = logging.getLogger("gcs.completion")

Notify = Callable[[str], None]


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


async def _watch(done_msg: str, link: MavlinkLink, predicate, notify: Notify,
                 *, timeout: float, settle: float = 3.0, poll: float = 0.6) -> None:
    """Poll `predicate(snapshot)` until True (then notify) or `timeout` (then give
    up silently — never claim a completion that didn't happen)."""
    await asyncio.sleep(settle)  # let the command take effect before judging
    waited = settle
    while waited < timeout:
        try:
            if predicate(link.snapshot()):
                try:
                    notify(done_msg)
                except Exception:  # noqa: BLE001
                    log.exception("completion notify failed")
                return
        except Exception:  # noqa: BLE001
            log.exception("completion predicate error")
        await asyncio.sleep(poll)
        waited += poll
    log.info("completion timed out (not reported): %s", done_msg)


def takeoff(vname: str, link: MavlinkLink, target_alt: float, notify: Notify) -> None:
    def pred(s: dict) -> bool:
        a = s.get("alt_rel")
        return a is not None and a >= max(1.0, 0.9 * target_alt)
    asyncio.create_task(_watch(
        f"{vname} reached {round(target_alt)} meters — takeoff complete.",
        link, pred, notify, timeout=90, settle=4))


def land(vname: str, link: MavlinkLink, notify: Notify) -> None:
    def pred(s: dict) -> bool:
        return s.get("armed") is False or (s.get("alt_rel") or 99) < 0.7
    asyncio.create_task(_watch(
        f"{vname} is down — landing complete.",
        link, pred, notify, timeout=180, settle=4))


def rtl(vname: str, link: MavlinkLink, notify: Notify) -> None:
    def pred(s: dict) -> bool:
        return s.get("armed") is False or (s.get("alt_rel") or 99) < 0.7
    asyncio.create_task(_watch(
        f"{vname} is home and landed — return to launch complete.",
        link, pred, notify, timeout=300, settle=4))


def goto(vname: str, link: MavlinkLink, tlat: float, tlon: float, notify: Notify,
         *, label: str = "the point", radius: float = 4.0) -> None:
    def pred(s: dict) -> bool:
        la, lo = s.get("lat"), s.get("lon")
        return la is not None and lo is not None and _dist_m(la, lo, tlat, tlon) <= radius
    asyncio.create_task(_watch(
        f"{vname} reached {label}.",
        link, pred, notify, timeout=150, settle=2))


def orbit(vname: str, link: MavlinkLink, clat: float, clon: float, radius: float,
          notify: Notify, *, label: str = "the point") -> None:
    # "Established" = riding the commanded ring (within ~half a radius of it).
    tol = max(5.0, 0.5 * radius)

    def pred(s: dict) -> bool:
        la, lo = s.get("lat"), s.get("lon")
        if la is None or lo is None:
            return False
        return abs(_dist_m(la, lo, clat, clon) - radius) <= tol
    asyncio.create_task(_watch(
        f"{vname} established orbit around {label} at {round(radius)} meters.",
        link, pred, notify, timeout=120, settle=5))


def autotune(vname: str, vehicle_id: str, notify: Notify) -> None:
    """Watch a vehicle's autotune controller until it reaches a TERMINAL state
    (COMPLETE/FAILED/CANCELLED) and speak the outcome. Unlike the flight watchers
    above this polls the AutotuneController snapshot (driven by PX4's ACK) rather
    than telemetry — so it reports the real tune result on BOTH Overwatch (MAVLink)
    and Outrider (DDS bridge) without depending on STATUSTEXT. Fire-and-forget;
    self-times-out so it never hangs if the controller is torn down."""
    from .autotune import AutotuneState, manager as _atm

    async def _run() -> None:
        # Generous cap: a tune is ~40 s but the controller's own no-progress
        # backstop is 60 s, so give it a little beyond that before giving up.
        deadline = asyncio.get_event_loop().time() + 90.0
        while asyncio.get_event_loop().time() < deadline:
            snap = _atm.status(vehicle_id)
            state = (snap or {}).get("state")
            if state == AutotuneState.COMPLETE.value:
                notify(f"{vname} autotune complete — new gains apply on landing.")
                return
            if state == AutotuneState.FAILED.value:
                reason = (snap or {}).get("reason") or "no reason given"
                notify(f"{vname} autotune failed: {reason}.")
                return
            if state == AutotuneState.CANCELLED.value:
                return  # operator-initiated; no spoken completion needed
            await asyncio.sleep(1.0)
        log.info("autotune completion watch timed out (not reported): %s", vname)

    asyncio.create_task(_run())
