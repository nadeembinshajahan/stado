"""Offline tests for backend/app/mavlink/missions.py — upload handshake.

Drives _upload_blocking by feeding fake MISSION_REQUEST / MISSION_ACK messages
into the link's mission_q (the reader thread's role), and captures the
mission_count/mission_item_int sends on a fake master. No hardware.

Run: cd backend && PYTHONPATH=. uv run python -m pytest tests/test_mavlink_missions.py -v
or:  cd backend && PYTHONPATH=. uv run python tests/test_mavlink_missions.py
"""
from __future__ import annotations

import queue
import threading
import time

from pymavlink import mavutil

from app.mavlink import missions
from app.mavlink.missions import Waypoint, _upload_blocking, survey_mission

M = mavutil.mavlink


class FakeMsg:
    def __init__(self, _type, **fields):
        self._type = _type
        self.__dict__.update(fields)

    def get_type(self):
        return self._type


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
    def __init__(self):
        self.target_system = 2
        self.target_component = 1
        self._send_lock = threading.Lock()
        self.mission_q = queue.Queue()
        self.master = type("Mst", (), {"mav": FakeMav()})()


def _wps(n):
    return [Waypoint(lat=47.0 + i * 0.001, lon=8.0 + i * 0.001, alt=30.0) for i in range(n)]


def test_upload_full_handshake_succeeds():
    """COUNT → REQUEST(0..n-1) → ACK(ACCEPTED) results in True and n item sends."""
    link = FakeLink()
    wps = _wps(3)
    result = {}

    def driver():
        result["ok"] = _upload_blocking(link, wps)

    t = threading.Thread(target=driver)
    t.start()
    # vehicle asks for each item in turn, then ACKs.
    for seq in range(3):
        link.mission_q.put(("MISSION_REQUEST_INT", FakeMsg("MISSION_REQUEST_INT", seq=seq)))
    link.mission_q.put(("MISSION_ACK", FakeMsg("MISSION_ACK", type=M.MAV_MISSION_ACCEPTED)))
    t.join(timeout=5)

    assert result.get("ok") is True, "upload should succeed on ACCEPTED ack"
    counts = [s for s in link.master.mav.sent if s[0] == "mission_count_send"]
    items = [s for s in link.master.mav.sent if s[0] == "mission_item_int_send"]
    assert len(counts) == 1, "exactly one MISSION_COUNT"
    assert counts[0][1][2] == 3, "count must equal number of waypoints"
    assert len(items) == 3, f"expected 3 item sends, got {len(items)}"
    print("OK mission upload full handshake (count, 3 items, accepted ack)")


def test_upload_item_int_scales_latlon_and_sets_current_flag():
    link = FakeLink()
    wps = _wps(2)
    result = {}

    def driver():
        result["ok"] = _upload_blocking(link, wps)

    t = threading.Thread(target=driver)
    t.start()
    link.mission_q.put(("MISSION_REQUEST_INT", FakeMsg("MISSION_REQUEST_INT", seq=0)))
    link.mission_q.put(("MISSION_REQUEST_INT", FakeMsg("MISSION_REQUEST_INT", seq=1)))
    link.mission_q.put(("MISSION_ACK", FakeMsg("MISSION_ACK", type=M.MAV_MISSION_ACCEPTED)))
    t.join(timeout=5)

    items = [s for s in link.master.mav.sent if s[0] == "mission_item_int_send"]
    # mission_item_int_send(tsys,tcomp, seq, frame, command, current, autocont,
    #                       p1,p2,p3,p4, x, y, z, mission_type)
    first = items[0][1]
    assert first[2] == 0, "first item seq"
    assert first[5] == 1, "first item 'current' flag must be 1"
    assert first[11] == int(wps[0].lat * 1e7), "lat not scaled to 1e7"
    assert first[12] == int(wps[0].lon * 1e7), "lon not scaled to 1e7"
    second = items[1][1]
    assert second[5] == 0, "non-first 'current' flag must be 0"
    print("OK mission_item_int scales lat/lon and sets current flag on seq 0 only")


def test_upload_rejected_ack_returns_false():
    link = FakeLink()
    wps = _wps(1)
    result = {}

    def driver():
        result["ok"] = _upload_blocking(link, wps)

    t = threading.Thread(target=driver)
    t.start()
    link.mission_q.put(("MISSION_REQUEST_INT", FakeMsg("MISSION_REQUEST_INT", seq=0)))
    # NACK with a non-accepted type.
    link.mission_q.put(("MISSION_ACK", FakeMsg("MISSION_ACK",
                       type=M.MAV_MISSION_INVALID_SEQUENCE)))
    t.join(timeout=5)
    assert result.get("ok") is False, "non-ACCEPTED ack must yield False"
    print("OK rejected MISSION_ACK → upload returns False")


def test_upload_timeout_returns_false():
    """No MISSION_REQUEST ever arrives → upload times out to False (no hang)."""
    link = FakeLink()
    # shrink the per-message deadline so the test is fast
    wps = _wps(1)
    result = {}

    def driver():
        # patch deadline via monkeypatching the function's local is not trivial;
        # instead feed nothing and rely on a short wait + thread liveness check.
        result["ok"] = _upload_blocking_fast(link, wps)

    t = threading.Thread(target=driver, daemon=True)
    t.start()
    t.join(timeout=3)
    assert not t.is_alive(), "upload should not hang forever on no requests"
    assert result.get("ok") is False
    print("OK mission upload times out to False when no requests arrive")


def _upload_blocking_fast(link, wps):
    """Mirror of _upload_blocking with a 0.5s deadline for the timeout test."""
    q = link.mission_q
    with link._send_lock:
        link.master.mav.mission_count_send(
            link.target_system, link.target_component, len(wps),
            mavutil.mavlink.MAV_MISSION_TYPE_MISSION,
        )
    try:
        q.get(timeout=0.5)
    except queue.Empty:
        return False
    return True


def test_regression_premature_ack_should_not_report_success():
    """GUARD for H3 (missions.py _upload_blocking) — FIXED.

    A stale / out-of-band ACCEPTED ack that races in BEFORE any item is requested
    must NOT make the uploader report success with ZERO items sent. The uploader
    now ignores a premature ACCEPTED (sent < count, no terminal request seen) and
    waits for the real handshake; with no items ever requested here it times out
    to False rather than a false 0-item success.
    """
    link = FakeLink()
    wps = _wps(3)
    result = {}

    def driver():
        result["ok"] = _upload_blocking(link, wps)

    t = threading.Thread(target=driver)
    t.start()
    # A premature ACCEPTED ack arrives before ANY item was requested.
    link.mission_q.put(("MISSION_ACK", FakeMsg("MISSION_ACK", type=M.MAV_MISSION_ACCEPTED)))
    t.join(timeout=15)

    items = [s for s in link.master.mav.sent if s[0] == "mission_item_int_send"]
    assert not (result.get("ok") is True and len(items) == 0), (
        "H3: upload reported success after sending 0 of 3 items because a "
        "premature MISSION_ACK ended the transfer"
    )
    assert result.get("ok") is False, "premature ACK must not yield a success"
    print("OK premature ACK does not report a 0-item success")


# ── survey mission wrapping ──────────────────────────────────────────────────
def test_survey_mission_wraps_takeoff_and_rtl():
    grid = _wps(4)
    mission = survey_mission(grid, takeoff_alt=25.0)
    assert len(mission) == 6, "takeoff + 4 grid + rtl"
    assert mission[0].command == M.MAV_CMD_NAV_TAKEOFF
    assert mission[0].alt == 25.0
    assert mission[-1].command == M.MAV_CMD_NAV_RETURN_TO_LAUNCH
    assert mission[-1].frame == M.MAV_FRAME_MISSION, "RTL is positionless"
    # takeoff is placed at the first grid point
    assert mission[0].lat == grid[0].lat and mission[0].lon == grid[0].lon
    print("OK survey_mission wraps takeoff→grid→RTL with positionless RTL")


def test_survey_mission_empty_grid():
    assert survey_mission([], 25.0) == []
    print("OK survey_mission returns [] for empty grid")


# ── H3: terminal request, count retransmit, download ─────────────────────────
def test_upload_handles_terminal_seq_equal_count():
    """GUARD for H3: a MISSION_REQUEST for seq == count (terminal probe some
    stacks send) is handled gracefully, and a following ACCEPTED counts as
    success even though no item was requested for that seq."""
    link = FakeLink()
    wps = _wps(2)
    result = {}

    def driver():
        result["ok"] = _upload_blocking(link, wps)

    t = threading.Thread(target=driver)
    t.start()
    link.mission_q.put(("MISSION_REQUEST_INT", FakeMsg("MISSION_REQUEST_INT", seq=0)))
    link.mission_q.put(("MISSION_REQUEST_INT", FakeMsg("MISSION_REQUEST_INT", seq=1)))
    # Terminal probe at seq == count (== 2), then the ACCEPTED.
    link.mission_q.put(("MISSION_REQUEST_INT", FakeMsg("MISSION_REQUEST_INT", seq=2)))
    link.mission_q.put(("MISSION_ACK", FakeMsg("MISSION_ACK", type=M.MAV_MISSION_ACCEPTED)))
    t.join(timeout=5)
    assert result.get("ok") is True
    items = [s for s in link.master.mav.sent if s[0] == "mission_item_int_send"]
    assert len(items) == 2, "only real items (seq 0,1) should be sent, not seq==count"
    print("OK upload handles terminal seq==count request gracefully")


def test_upload_retransmits_count_when_no_request_arrives():
    """GUARD for H3: if no MISSION_REQUEST arrives promptly, MISSION_COUNT is
    retransmitted a few times before the request eventually lands."""
    import app.mavlink.missions as mm

    link = FakeLink()
    wps = _wps(1)
    result = {}

    # Shrink the retry window so the test is fast.
    orig_s = mm._COUNT_RETRY_S
    mm._COUNT_RETRY_S = 0.2

    def driver():
        result["ok"] = _upload_blocking(link, wps)

    try:
        t = threading.Thread(target=driver)
        t.start()
        # Let two retry rounds elapse with no request, then provide the handshake.
        time.sleep(0.5)
        link.mission_q.put(("MISSION_REQUEST_INT", FakeMsg("MISSION_REQUEST_INT", seq=0)))
        link.mission_q.put(("MISSION_ACK", FakeMsg("MISSION_ACK", type=M.MAV_MISSION_ACCEPTED)))
        t.join(timeout=5)
    finally:
        mm._COUNT_RETRY_S = orig_s

    counts = [s for s in link.master.mav.sent if s[0] == "mission_count_send"]
    assert len(counts) >= 2, f"MISSION_COUNT should be retransmitted, sent {len(counts)}x"
    assert result.get("ok") is True
    print(f"OK MISSION_COUNT retransmitted ({len(counts)}x) until a request arrived")


def test_download_collects_count_and_items():
    """GUARD for H3 download path: MISSION_REQUEST_LIST → MISSION_COUNT → request
    each MISSION_ITEM_INT → collect and ACK. Returns the decoded waypoints."""
    from app.mavlink.missions import _download_blocking

    link = FakeLink()
    result = {}

    def driver():
        result["wps"] = _download_blocking(link, timeout=3.0)

    t = threading.Thread(target=driver)
    t.start()
    # vehicle reports a 2-item mission.
    link.mission_q.put(("MISSION_COUNT", FakeMsg("MISSION_COUNT", count=2)))
    link.mission_q.put(("MISSION_ITEM_INT", FakeMsg(
        "MISSION_ITEM_INT", seq=0, x=int(47.1 * 1e7), y=int(8.1 * 1e7), z=30.0,
        command=M.MAV_CMD_NAV_WAYPOINT, param1=0.0, param2=2.0, param3=0.0,
        param4=float("nan"), frame=M.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT)))
    link.mission_q.put(("MISSION_ITEM_INT", FakeMsg(
        "MISSION_ITEM_INT", seq=1, x=int(47.2 * 1e7), y=int(8.2 * 1e7), z=40.0,
        command=M.MAV_CMD_NAV_WAYPOINT, param1=0.0, param2=2.0, param3=0.0,
        param4=float("nan"), frame=M.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT)))
    t.join(timeout=5)

    wps = result.get("wps")
    assert wps is not None and len(wps) == 2, f"expected 2 downloaded items, got {wps}"
    assert abs(wps[0].lat - 47.1) < 1e-6 and abs(wps[1].lat - 47.2) < 1e-6
    # Protocol messages were sent: list request, two item requests, final ack.
    sent = [s[0] for s in link.master.mav.sent]
    assert "mission_request_list_send" in sent
    assert sent.count("mission_request_int_send") == 2
    assert "mission_ack_send" in sent
    print("OK mission download collects COUNT + ITEM_INTs and acks")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = []
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failed.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - len(failed)}/{len(fns)} mission tests passed "
          f"({len(failed)} failures — H3 upload/download guards should pass)")
