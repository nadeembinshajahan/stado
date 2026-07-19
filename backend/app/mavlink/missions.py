from __future__ import annotations

import asyncio
import logging
import queue
import time
from dataclasses import dataclass

from pymavlink import mavutil

from . import commands
from .link import MavlinkLink

log = logging.getLogger("gcs.mission")

_FRAME = mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT
_MISSION = mavutil.mavlink.MAV_MISSION_TYPE_MISSION


@dataclass
class Waypoint:
    lat: float
    lon: float
    alt: float
    command: int = mavutil.mavlink.MAV_CMD_NAV_WAYPOINT
    param1: float = 0.0  # hold time (s) / loiter
    param2: float = 2.0  # acceptance radius (m)
    param3: float = 0.0
    param4: float = float("nan")  # yaw
    frame: int = _FRAME  # positionless commands (e.g. RTL) need MAV_FRAME_MISSION


def _drain(q: "queue.Queue") -> None:
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


def _send_count(link: MavlinkLink, count: int) -> None:
    with link._send_lock:  # noqa: SLF001 — intentional shared lock
        m = link.master
        if m is None:
            return
        m.mav.mission_count_send(
            link.target_system, link.target_component, count, _MISSION
        )


# Retransmit MISSION_COUNT this many times (waiting _COUNT_RETRY_S for the first
# MISSION_REQUEST each round) before giving up — a single dropped COUNT on a thin
# link otherwise stalls the full per-item timeout then fails with 0/N (H3).
_COUNT_RETRIES = 3
_COUNT_RETRY_S = 2.0


def _upload_blocking(link: MavlinkLink, waypoints: list[Waypoint]) -> bool:
    """Run the MISSION_COUNT → MISSION_REQUEST → MISSION_ITEM_INT → ACK handshake.

    Robustness (H3):
      * MISSION_COUNT is retransmitted a few times if the first MISSION_REQUEST
        doesn't arrive promptly (a dropped COUNT no longer stalls 10 s then fails).
      * A MISSION_ACK that arrives BEFORE every item has been requested for a
        multi-item mission is treated as suspicious, not a clean success — a stale
        ACK from a prior transfer can't end this one with 0 items sent.
      * seq == count is handled gracefully (some stacks probe with a terminal
        request) rather than stalling until the per-message timeout.
    """
    q = link.mission_q
    _drain(q)
    count = len(waypoints)
    _send_count(link, count)

    sent = 0
    requested_terminal = False  # saw a MISSION_REQUEST for seq == count
    count_sends = 1
    # Until the first request arrives, poll on the shorter retry deadline so a
    # dropped COUNT can be resent; afterwards, allow the longer per-item deadline.
    deadline_each = 10.0
    saw_first_request = False
    while True:
        timeout = deadline_each if saw_first_request else _COUNT_RETRY_S
        try:
            mtype, msg = q.get(timeout=timeout)
        except queue.Empty:
            if not saw_first_request and count_sends < _COUNT_RETRIES:
                log.warning("no MISSION_REQUEST yet — resending MISSION_COUNT (%d)", count_sends)
                _send_count(link, count)
                count_sends += 1
                continue
            log.error("mission upload timed out after %d/%d items", sent, count)
            return False

        if mtype == "MISSION_ACK":
            ok = int(msg.type) == mavutil.mavlink.MAV_MISSION_ACCEPTED
            # H3: a clean ACCEPTED must follow having sent all items (or hit the
            # terminal seq==count request). A premature ACCEPTED that races in
            # before the items were requested is a stale ACK, not success.
            if ok and count > 0 and sent < count and not requested_terminal:
                log.warning(
                    "ignoring premature MISSION_ACK (sent %d/%d items)", sent, count
                )
                continue
            log.info("mission ack: type=%d (%s)", msg.type, "OK" if ok else "REJECTED")
            return ok

        if mtype in ("MISSION_REQUEST", "MISSION_REQUEST_INT"):
            saw_first_request = True
            seq = int(msg.seq)
            if seq == count:
                # Terminal probe some stacks send after the last item — no-op, but
                # note it so a following ACCEPTED counts as success (H3).
                requested_terminal = True
                continue
            if seq > count or seq < 0:
                continue  # out of range — ignore, await a valid request or ACK
            wp = waypoints[seq]
            current = 1 if seq == 0 else 0
            with link._send_lock:  # noqa: SLF001
                m = link.master
                if m is None:
                    return False
                m.mav.mission_item_int_send(
                    link.target_system, link.target_component,
                    seq, wp.frame, wp.command, current, 1,
                    wp.param1, wp.param2, wp.param3, wp.param4,
                    int(wp.lat * 1e7), int(wp.lon * 1e7), wp.alt,
                    _MISSION,
                )
            sent = max(sent, seq + 1)


def _download_blocking(link: MavlinkLink, timeout: float = 10.0) -> list[Waypoint] | None:
    """Read the vehicle's active mission: MISSION_REQUEST_LIST → MISSION_COUNT →
    request each MISSION_ITEM_INT → MISSION_ACK (H3 download path). Returns the
    waypoints, or None on timeout/failure. MISSION_COUNT and MISSION_ITEM_INT are
    routed into mission_q by the reader thread (link._handle)."""
    q = link.mission_q
    _drain(q)
    with link._send_lock:  # noqa: SLF001
        m = link.master
        if m is None:
            return None
        m.mav.mission_request_list_send(
            link.target_system, link.target_component, _MISSION
        )

    # 1) Wait for MISSION_COUNT.
    count: int | None = None
    deadline = time.time() + timeout
    while count is None and time.time() < deadline:
        try:
            mtype, msg = q.get(timeout=max(0.1, deadline - time.time()))
        except queue.Empty:
            break
        if mtype == "MISSION_COUNT":
            count = int(msg.count)
        elif mtype == "MISSION_ACK":
            return None  # empty/refused
    if count is None:
        log.error("mission download: no MISSION_COUNT received")
        return None

    # 2) Request each item in turn and collect them.
    items: dict[int, Waypoint] = {}
    for seq in range(count):
        with link._send_lock:  # noqa: SLF001
            m = link.master
            if m is None:
                return None
            m.mav.mission_request_int_send(
                link.target_system, link.target_component, seq, _MISSION
            )
        got = False
        item_deadline = time.time() + timeout
        while not got and time.time() < item_deadline:
            try:
                mtype, msg = q.get(timeout=max(0.1, item_deadline - time.time()))
            except queue.Empty:
                break
            if mtype == "MISSION_ITEM_INT" and int(msg.seq) == seq:
                items[seq] = Waypoint(
                    lat=int(msg.x) / 1e7, lon=int(msg.y) / 1e7, alt=float(msg.z),
                    command=int(msg.command), param1=float(msg.param1),
                    param2=float(msg.param2), param3=float(msg.param3),
                    param4=float(msg.param4), frame=int(msg.frame),
                )
                got = True
        if not got:
            log.error("mission download: item %d not received", seq)
            return None

    # 3) Acknowledge the completed transfer.
    with link._send_lock:  # noqa: SLF001
        m = link.master
        if m is not None:
            m.mav.mission_ack_send(
                link.target_system, link.target_component,
                mavutil.mavlink.MAV_MISSION_ACCEPTED, _MISSION,
            )
    return [items[i] for i in range(count)]


async def download(link: MavlinkLink, timeout: float = 10.0) -> list[Waypoint] | None:
    """Async wrapper: read the vehicle's current mission off a worker thread."""
    return await asyncio.to_thread(_download_blocking, link, timeout)


async def upload(link: MavlinkLink, waypoints: list[Waypoint]) -> bool:
    ok = await asyncio.to_thread(_upload_blocking, link, waypoints)
    if ok:
        # Push the uploaded plan to the GCS so it's drawn on the map.
        from ..ws.hub import hub
        hub.publish_threadsafe({
            "type": "mission",
            "waypoints": [[w.lat, w.lon] for w in waypoints],
            "commands": [int(w.command) for w in waypoints],
        })
    return ok


async def clear(link: MavlinkLink) -> None:
    with link._send_lock:  # noqa: SLF001
        link.master.mav.mission_clear_all_send(
            link.target_system, link.target_component, _MISSION
        )


async def start(link: MavlinkLink) -> None:
    """Switch to AUTO.MISSION and begin executing the uploaded mission."""
    await commands.arm(link)
    await asyncio.sleep(0.5)
    link.command_long(mavutil.mavlink.MAV_CMD_MISSION_START, 0, 0)
    await commands.set_mode(link, "MISSION")


def survey_mission(grid: list[Waypoint], takeoff_alt: float) -> list[Waypoint]:
    """Wrap a survey grid into a complete, self-contained mission:
    takeoff → grid → return-to-launch. Safe to execute from the ground."""
    if not grid:
        return []
    takeoff = Waypoint(
        lat=grid[0].lat, lon=grid[0].lon, alt=takeoff_alt,
        command=mavutil.mavlink.MAV_CMD_NAV_TAKEOFF,
    )
    rtl = Waypoint(
        lat=0.0, lon=0.0, alt=0.0,
        command=mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH,
        frame=mavutil.mavlink.MAV_FRAME_MISSION,  # positionless command
    )
    return [takeoff, *grid, rtl]
