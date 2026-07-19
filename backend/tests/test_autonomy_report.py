"""Functional tests for flight recording / report aggregation, coordinated
survey assignment, vehicle-plate lookup, and survey-vision pixel projection.

No hardware/network. Complements tests/test_coordination.py (does not touch it).

Covers the intent-vs-implementation gaps from the review:

  * FlightRecorder arm->disarm lifecycle: stats, sub-1 m jitter rejection,
    distance accumulation, battery used, on_complete fire,
  * summarize / build_facts / fallback_summary aggregation correctness,
  * group_missions overlapping-window grouping,
  * coordinated.plan_and_fly pairs vehicles<->zones and assigns deconflicted
    altitudes; mismatched list lengths silently truncate (documented),
  * vehicle_lookup plate decode + ALWAYS-masked owner + deterministic mock,
  * survey_vision pixel->lat/lon projection + scale inference + N/S orientation.

Run: cd backend && PYTHONPATH=. .venv/bin/python -m pytest tests/test_autonomy_report.py -q
"""
from __future__ import annotations

import asyncio
import math

import pytest

from app.flights import (
    FlightRecorder,
    group_missions,
    summarize,
)
from app.report import build_facts, fallback_summary


# ── FlightRecorder lifecycle + stats ──────────────────────────────────────────
def _snap(**kw):
    base = {"armed": False, "lat": None, "lon": None, "alt_rel": None,
            "groundspeed": None, "battery_pct": None, "mode": None}
    base.update(kw)
    return base


def test_recorder_arm_disarm_records_flight_with_stats():
    completed = []
    rec = FlightRecorder("overwatch", "Overwatch")
    rec.on_complete = completed.append

    # Disarmed first sample (no flight yet).
    rec.feed_telemetry(_snap(armed=False, lat=12.97, lon=77.59, alt_rel=0.0))
    assert rec._cur is None

    # Arm -> flight begins.
    rec.feed_telemetry(_snap(armed=True, lat=12.97, lon=77.59, alt_rel=0.0,
                             battery_pct=100.0, groundspeed=0.0, mode="TAKEOFF"))
    assert rec._cur is not None

    # Fly ~111 m north (well over the 1 m jitter floor), climb, speed up, drain.
    rec.feed_telemetry(_snap(armed=True, lat=12.971, lon=77.59, alt_rel=30.0,
                             groundspeed=8.0, battery_pct=80.0, mode="MISSION"))
    rec.feed_telemetry(_snap(armed=True, lat=12.972, lon=77.59, alt_rel=45.0,
                             groundspeed=12.0, battery_pct=70.0))

    # Disarm -> finalize.
    rec.feed_telemetry(_snap(armed=False, lat=12.972, lon=77.59, alt_rel=0.0))
    assert rec._cur is None
    assert len(completed) == 1, "on_complete must fire exactly once on landing"

    summary = completed[0]
    assert summary["vehicle_id"] == "overwatch"
    assert summary["max_alt_m"] == 45.0
    assert summary["max_speed_ms"] == 12.0
    assert summary["battery_start_pct"] == 100.0
    assert summary["battery_min_pct"] == 70.0
    assert summary["battery_used_pct"] == 30.0
    # ~222 m of travel (two 111 m legs); allow generous tolerance for haversine.
    assert 200.0 < summary["distance_m"] < 240.0, summary["distance_m"]
    assert summary["takeoff"] == {"lat": 12.97, "lon": 77.59}
    assert summary["landing"] == {"lat": 12.972, "lon": 77.59}


def test_recorder_rejects_sub_meter_jitter():
    rec = FlightRecorder("outrider", "Outrider")
    rec.feed_telemetry(_snap(armed=True, lat=12.97, lon=77.59, alt_rel=10.0))
    # Tiny GPS wobble (< 1 m): must NOT accumulate distance or path points.
    for _ in range(20):
        rec.feed_telemetry(_snap(armed=True, lat=12.97 + 1e-7, lon=77.59 + 1e-7, alt_rel=10.0))
    assert rec._cur["distance_m"] == 0.0, "sub-metre jitter must add no distance"
    # Exactly one path point (the first fix); jitter samples are dropped.
    assert len(rec._cur["path"]) == 1


def test_recorder_prearm_action_folded_into_flight():
    """M3: the opening command (takeoff) is issued BEFORE arm. It must be folded
    into the flight's action timeline on arm, not dropped — otherwise the report
    is missing the very command that started the mission."""
    rec = FlightRecorder("overwatch", "Overwatch")
    # Pre-arm takeoff command — buffered, not dropped.
    rec.feed_action("takeoff", {"altitude_m": 30}, {"ok": True})
    rec.feed_telemetry(_snap(armed=True, lat=12.97, lon=77.59, alt_rel=0.0))
    rec.feed_action("orbit_target", {"radius_m": 25}, {"ok": True})
    rec.feed_action("land", {}, {"ok": False})
    assert len(rec._cur["actions"]) == 3, "pre-arm takeoff is folded into the timeline"
    labels = [a["label"] for a in rec._cur["actions"]]
    assert "Takeoff → 30 m" in labels
    assert "Orbit target r=25 m" in labels
    # Failed result is flagged.
    land = next(a for a in rec._cur["actions"] if a["name"] == "land")
    assert land["ok"] is False


def test_recorder_stale_prearm_action_discarded():
    """A pre-arm action older than the window is NOT mis-attributed to a flight
    that arms much later."""
    import time as _time

    rec = FlightRecorder("outrider", "Outrider")
    rec.feed_action("set_mode", {"mode": "guided"}, {"ok": True})
    # Age the buffered action well past the pre-arm window.
    rec._prearm_actions[0]["ts"] -= 600.0
    rec.feed_telemetry(_snap(armed=True, lat=12.97, lon=77.59, alt_rel=0.0))
    assert rec._cur["actions"] == [], "stale pre-arm action must be discarded"
    assert _time  # silence unused import lint


def test_recorder_mode_timeline_dedup():
    rec = FlightRecorder("overwatch", "Overwatch")
    rec.feed_telemetry(_snap(armed=True, lat=12.97, lon=77.59, alt_rel=0.0, mode="TAKEOFF"))
    rec.feed_mode("MISSION")
    rec.feed_mode("MISSION")  # duplicate -> ignored
    rec.feed_mode("HOLD")
    modes = [m["mode"] for m in rec._cur["mode_timeline"]]
    assert modes == ["TAKEOFF", "MISSION", "HOLD"], modes


# ── report aggregation ────────────────────────────────────────────────────────
def _finished_flight():
    return {
        "id": "f1", "vehicle_id": "overwatch", "vehicle_name": "Overwatch",
        "start_ts": 1000.0, "end_ts": 1125.0, "duration_s": 125.0,
        "max_alt_m": 45.3, "max_speed_ms": 12.4, "distance_m": 1834.0,
        "battery_start_pct": 100, "battery_min_pct": 62, "battery_used_pct": 38,
        "takeoff": {"lat": 12.97, "lon": 77.59}, "landing": {"lat": 12.971, "lon": 77.59},
        "mode_timeline": [{"mode": "TAKEOFF", "ts": 1000}, {"mode": "MISSION", "ts": 1010}],
        "events": [{"text": "GPS glitch"}],
        "actions": [{"ts": 1005, "name": "takeoff", "label": "Takeoff -> 30 m", "ok": True},
                    {"ts": 1090, "name": "land", "label": "Land", "ok": False}],
    }


def test_build_facts_and_summarize_counts():
    f = _finished_flight()
    facts = build_facts(f)
    assert facts["n_actions"] == 2
    assert facts["n_events"] == 1
    assert facts["modes"] == ["TAKEOFF", "MISSION"]
    # The failed action is annotated.
    assert any("(FAILED)" in a for a in facts["actions"])

    s = summarize(f)
    assert s["distance_m"] == 1834.0
    assert s["action_count"] == 2 and s["event_count"] == 1
    assert s["battery_used_pct"] == 38


def test_fallback_summary_mentions_key_stats():
    text = fallback_summary(_finished_flight())
    assert "Overwatch" in text
    assert "km" in text or "m" in text  # distance formatted
    assert "38%" in text  # battery
    assert "action" in text.lower()


def test_group_missions_overlap_and_separate():
    a = {"id": "a", "vehicle_id": "overwatch", "vehicle_name": "OW",
         "start_ts": 100.0, "end_ts": 200.0}
    b = {"id": "b", "vehicle_id": "outrider", "vehicle_name": "OUT",
         "start_ts": 150.0, "end_ts": 250.0}
    far = {"id": "c", "vehicle_id": "overwatch", "vehicle_name": "OW",
           "start_ts": 5000.0, "end_ts": 5100.0}
    missions = group_missions([a, b, far])
    assert len(missions) == 2
    # Newest first.
    assert missions[0]["mission_id"] == "m_c"
    multi = missions[1]
    assert set(multi["vehicles"]) == {"overwatch", "outrider"}
    assert len(multi["flights"]) == 2
    # Deterministic id derived from the earliest member.
    assert multi["mission_id"] == "m_a"


# ── coordinated.plan_and_fly assignment ───────────────────────────────────────
class _FakeLink:
    def __init__(self, connected=True):
        self._connected = connected

    def snapshot(self):
        return {"connected": self._connected}


def test_plan_and_fly_assigns_zone_and_altitude_per_vehicle(monkeypatch):
    from app.survey import coordinated as C

    uploaded = _install_fleet_helper(monkeypatch, {"overwatch", "outrider"})

    lat, lon = 12.97, 77.59
    # gap_m=5 is the HORIZONTAL corridor; sep_m=15 is the VERTICAL floor
    # (preflight-02 F2, operator-approved). They are decoupled.
    zones = C.split_rect(lat, lon, 200.0, 200.0, 0.0, n=2, gap_m=5.0)
    results = asyncio.run(
        C.plan_and_fly(["overwatch", "outrider"], zones, base_alt=30.0,
                       line_spacing_m=25.0, sep_m=15.0)
    )
    assert len(results) == 2
    by_v = {r["vehicle"]: r for r in results}
    assert set(by_v) == {"overwatch", "outrider"}
    # Overwatch deconflicted ABOVE Outrider by a firm >= 15 m.
    assert by_v["overwatch"]["altitude"] > by_v["outrider"]["altitude"]
    assert by_v["overwatch"]["altitude"] - by_v["outrider"]["altitude"] >= 15.0
    # Both got a non-empty planned path + a mission upload.
    assert by_v["overwatch"]["waypoints"] > 0
    assert by_v["outrider"]["waypoints"] > 0
    assert len(uploaded) == 2


def test_plan_and_fly_disconnected_returns_preview_not_flown(monkeypatch):
    from app.survey import coordinated as C

    uploaded = _install_fleet_helper(monkeypatch, set())  # nothing connected
    lat, lon = 12.97, 77.59
    zones = C.split_rect(lat, lon, 200.0, 200.0, 0.0, n=2, gap_m=5.0)
    results = asyncio.run(
        C.plan_and_fly(["overwatch", "outrider"], zones, base_alt=30.0,
                       line_spacing_m=25.0, sep_m=15.0)
    )
    # Preview path present, but flagged not-connected and never uploaded.
    for r in results:
        assert r["error"] == "link not connected"
        assert r["waypoints"] == 0
        assert r["path"], "preview grid should still be returned for the UI"
    assert uploaded == [], "disconnected vehicles must not upload a mission"


def test_plan_and_fly_mismatch_surfaces_per_vehicle_error(monkeypatch):
    """H3: more vehicles than zones must NOT silently drop a vehicle. The paired
    vehicle flies; the unpaired one gets a clear per-vehicle error (never a
    silent no-fly via zip() truncation)."""
    from app.survey import coordinated as C

    _install_fleet_helper(monkeypatch, {"overwatch", "outrider"})
    lat, lon = 12.97, 77.59
    zones = C.split_rect(lat, lon, 200.0, 200.0, 0.0, n=1, gap_m=5.0)  # ONE zone
    results = asyncio.run(
        C.plan_and_fly(["overwatch", "outrider"], zones, base_alt=30.0,
                       line_spacing_m=25.0, sep_m=15.0)
    )
    assert len(results) == 2, "every vehicle must be accounted for (no silent drop)"
    by_v = {r["vehicle"]: r for r in results}
    assert set(by_v) == {"overwatch", "outrider"}
    # The paired vehicle flew; the unpaired one carries an explicit error.
    assert by_v["overwatch"]["waypoints"] > 0
    assert by_v["outrider"]["waypoints"] == 0
    assert "no survey zone" in by_v["outrider"]["error"]


# Helper kept separate so monkeypatch is threaded correctly.
def _install_fleet_helper(monkeypatch, connected_ids):
    from app.survey import coordinated as C

    class _Veh:
        def __init__(self, vid, conn):
            self.id = vid
            self.name = vid.title()
            self.link = _FakeLink(conn)

    vehicles = {
        "overwatch": _Veh("overwatch", "overwatch" in connected_ids),
        "outrider": _Veh("outrider", "outrider" in connected_ids),
    }

    class _Reg:
        def get(self, vid):
            if vid not in vehicles:
                raise KeyError(vid)
            return vehicles[vid]

    monkeypatch.setattr(C, "registry", _Reg())

    uploaded = []

    async def _upload(link, wps):
        uploaded.append(wps)
        return True

    async def _start(link):
        return None

    def _survey_mission(grid, alt):
        return list(grid)

    monkeypatch.setattr(C.missions, "upload", _upload)
    monkeypatch.setattr(C.missions, "start", _start)
    monkeypatch.setattr(C.missions, "survey_mission", _survey_mission)
    return uploaded


# ── PREFLIGHT: multi-drone vertical deconfliction (no shared/inverted band) ────
def _stub_coord():
    """Import app.coordination with the heavy vision deps stubbed."""
    import sys
    import types as _t

    for n, a in (("ultralytics", {"YOLO": object}), ("moondream", {})):
        if n not in sys.modules:
            m = _t.ModuleType(n)
            for k, v in a.items():
                setattr(m, k, v)
            sys.modules[n] = m
    from app import coordination as C

    return C


class _PosLink:
    def __init__(self, **state):
        base = {"connected": True, "armed": True, "lat": 12.97, "lon": 77.59,
                "alt_rel": 40.0, "heading": 0.0}
        base.update(state)
        self._state = base

    def snapshot(self):
        return dict(self._state)


class _CoordVeh:
    def __init__(self, vid, link):
        self.id = vid
        self.name = vid.title()
        self.link = link


def _run_one_formation_cycle(monkeypatch, ow_alt):
    """Drive a SINGLE iteration of _formation_loop with Overwatch at `ow_alt` and
    return the altitude Outrider was commanded to (or None if the cycle SKIPPED,
    i.e. no goto was issued because Overwatch was too low to keep separation)."""
    C = _stub_coord()
    ow = _CoordVeh("overwatch", _PosLink(alt_rel=ow_alt, lat=12.97, lon=77.59))
    our = _CoordVeh("outrider", _PosLink(alt_rel=8.0))

    def fake_vehicle(vid):
        return {"overwatch": ow, "outrider": our}.get(vid)

    monkeypatch.setattr(C, "_vehicle", fake_vehicle)

    captured: dict = {"alt": None, "called": False}

    async def fake_goto(link, lat, lon, alt, speed=-1.0, override=False):
        captured["called"] = True
        captured["alt"] = alt
        raise asyncio.CancelledError  # stop the infinite loop after one goto

    async def fake_sleep(_s):
        # If we reach the sleep without a goto, the cycle SKIPPED — stop the loop.
        raise asyncio.CancelledError

    monkeypatch.setattr(C.commands, "goto", fake_goto)
    monkeypatch.setattr(C.asyncio, "sleep", fake_sleep)

    async def drive():
        try:
            await C._formation_loop(offset_m=12.0, bearing_deg=180.0, period_s=0.01)
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    return captured["alt"] if captured["called"] else None


def test_formation_never_commands_outrider_at_or_above_overwatch(monkeypatch):
    """PREFLIGHT (collision guard): the formation loop only controls Outrider. The
    OLD clamp `max(ow_alt - SEP, MIN_ALT)` produced a COLLISION at ow=MIN_ALT (both
    at the floor) and an INVERSION below it (Outrider commanded ABOVE Overwatch).
    Now: while Overwatch is high enough, Outrider is SEP below it; when Overwatch
    is too low to keep SEP above the floor, the cycle SKIPS (no converging goto).
    """
    C = _stub_coord()
    # SEP_M is now a firm 15 m (preflight-02 F2). "Comfortably high" means Overwatch
    # is high enough to keep SEP_M above the floor (ow_alt - SEP_M >= MIN_ALT_M).
    floor = C.SEP_M + C.MIN_ALT_M  # 15 + 8 = 23 m
    for ow_alt in (floor, 40.0, 60.0):
        cmd = _run_one_formation_cycle(monkeypatch, ow_alt)
        assert cmd is not None, f"OW={ow_alt}: formation should command Outrider"
        assert cmd < ow_alt, f"OW={ow_alt}: Outrider {cmd} must be strictly BELOW Overwatch"
        assert abs((ow_alt - cmd) - C.SEP_M) < 1e-6, f"OW={ow_alt}: sep != SEP_M (15 m)"
        assert cmd >= C.MIN_ALT_M, f"OW={ow_alt}: Outrider below the {C.MIN_ALT_M} m floor"

    # Overwatch too low to fit the 15 m SEP above the floor: NO converging/inverting
    # setpoint (skip the cycle rather than collapse the separation).
    for ow_alt in (8.0, 15.0, 22.9):
        cmd = _run_one_formation_cycle(monkeypatch, ow_alt)
        if cmd is not None:
            assert cmd < ow_alt, (
                f"OW={ow_alt}: Outrider {cmd} commanded at/above Overwatch (collision!)"
            )


def test_staggered_altitudes_keeps_overwatch_above_outrider(monkeypatch):
    """PREFLIGHT: coordinated_orbit derives Outrider from Overwatch via
    staggered_altitudes — Overwatch must stay strictly above, floor-clamped, with a
    firm >= 15 m VERTICAL separation (preflight-02 F2; SEP_M is now 15 m)."""
    C = _stub_coord()
    assert C.SEP_M >= 15.0, "vertical separation must be unified to >= 15 m"
    for ow in (40.0, 30.0, 13.0, 10.0, 8.0, 0.0, -5.0):
        ow_alt, our_alt = C.staggered_altitudes(ow)
        assert ow_alt > our_alt, f"requested OW={ow}: Overwatch must stay above Outrider"
        assert ow_alt - our_alt >= C.SEP_M - 1e-9, f"OW={ow}: sep below SEP_M (15 m)"
        assert our_alt >= C.MIN_ALT_M, f"OW={ow}: Outrider below the floor"


def test_coordinated_orbit_staggers_both_alt_and_radius(monkeypatch):
    """PREFLIGHT: both drones orbit at STAGGERED altitudes AND different radii so
    the two circles never coincide. Overwatch high + wider; Outrider low + base."""
    C = _stub_coord()

    ow = _CoordVeh("overwatch", _PosLink(alt_rel=40.0))
    our = _CoordVeh("outrider", _PosLink(alt_rel=35.0))

    class _Reg:
        def list(self):
            return [ow, our]

        def active_vehicle(self):
            return ow

    monkeypatch.setattr(C, "registry", _Reg())

    orbits: list = []

    async def fake_orbit(link, lat, lon, alt, radius, vel, override=False):
        orbits.append({"link": link, "alt": alt, "radius": radius})

    monkeypatch.setattr(C.commands, "orbit", fake_orbit)
    # coordinated_orbit calls coordination.stop_all() first; harmless here.
    res = asyncio.run(C.coordinated_orbit(lat=12.97, lon=77.59, radius_m=25.0, altitude=40.0))
    assert res["ok"]
    by_link = {id(ow.link): None, id(our.link): None}
    for o in orbits:
        by_link[id(o["link"])] = o
    ow_o, our_o = by_link[id(ow.link)], by_link[id(our.link)]
    assert ow_o and our_o, "both drones must receive an orbit command"
    # Altitudes staggered, Overwatch higher by a firm >= 15 m (preflight-02 F2).
    assert ow_o["alt"] > our_o["alt"], "Overwatch must orbit higher than Outrider"
    assert ow_o["alt"] - our_o["alt"] >= C.SEP_M - 1e-9
    assert C.SEP_M >= 15.0 and ow_o["alt"] - our_o["alt"] >= 15.0 - 1e-9
    # Radii staggered so the circles don't intersect even at equal-ish altitudes.
    assert ow_o["radius"] > our_o["radius"], "Overwatch circle must be wider"


# ── vehicle_lookup ────────────────────────────────────────────────────────────
def test_plate_decode_valid_and_state():
    from app import vehicle_lookup as V

    d = V.decode_plate("ka 01-ab 1234")
    assert d["valid"] is True
    assert d["plate"] == "KA01AB1234"
    assert d["state"] == "Karnataka"
    assert d["rto_code"] == "KA01"


def test_plate_decode_invalid():
    from app import vehicle_lookup as V

    d = V.decode_plate("NOTAPLATE!!")
    assert d["valid"] is False
    assert d["state"] is None


def test_lookup_owner_always_masked_and_deterministic():
    from app import vehicle_lookup as V

    V._CACHE.clear()
    a = asyncio.run(V.lookup("KA05MX4321"))
    V._CACHE.clear()
    b = asyncio.run(V.lookup("KA05MX4321"))
    assert a["source"] == "mock"
    # Owner masked: contains '*' and no full name leaks.
    assert "*" in a["owner"]
    # Deterministic per plate.
    assert a["maker_model"] == b["maker_model"]
    assert a["owner"] == b["owner"]


def test_mask_name():
    from app.vehicle_lookup import mask_name

    assert mask_name("Rahul Sharma").startswith("R")
    assert "*" in mask_name("Rahul Sharma")
    assert mask_name("") == "REDACTED"
    assert mask_name(None) == "REDACTED"


# ── survey_vision pixel projection ─────────────────────────────────────────────
def test_survey_vision_polygon_projection_orientation():
    from app.survey_vision import Bounds, _polygon_to_latlon

    b = Bounds(north=13.0, south=12.0, east=78.0, west=77.0)
    # Top-left (0,0) -> NW corner; bottom-right (1000,1000) -> SE corner.
    pts = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
    poly = _polygon_to_latlon(pts, b)
    nw, ne, se, sw = poly
    assert abs(nw[0] - 13.0) < 1e-9 and abs(nw[1] - 77.0) < 1e-9  # north, west
    assert abs(se[0] - 12.0) < 1e-9 and abs(se[1] - 78.0) < 1e-9  # south, east
    # y increases downward -> latitude DECREASES (north at top).
    assert nw[0] > sw[0]


def test_survey_vision_infers_scale():
    from app.survey_vision import _infer_scale

    assert _infer_scale([(0.1, 0.9)]) == 1.0
    assert _infer_scale([(10, 90)]) == 100.0
    assert _infer_scale([(100, 900)]) == 1000.0
    assert _infer_scale([(1500, 2000)]) == 2000.0  # raw pixels beyond 1000


def test_static_maps_bounds_north_above_south():
    from app.survey_vision import _static_maps_bounds

    b = _static_maps_bounds(12.97, 77.59, zoom=18, size=640)
    assert b.north > b.south, "north edge must be a higher latitude than south"
    assert b.east > b.west, "east edge must be a higher longitude than west"
    # Centre is inside the box.
    assert b.south < 12.97 < b.north and b.west < 77.59 < b.east


# ── M4: regions / pois name resolution (exact -> word-boundary -> prefix) ─────
def test_regions_find_exact_disambiguates_sector_1_from_12():
    import app.regions as R

    R.set_regions([
        {"id": "a", "name": "Sector 1", "center": [12.97, 77.59],
         "width_m": 100, "height_m": 100, "heading_deg": 0},
        {"id": "b", "name": "Sector 12", "center": [12.98, 77.60],
         "width_m": 100, "height_m": 100, "heading_deg": 0},
    ])
    # Exact match wins over the loose "Sector 12" — the old substring bug
    # could resolve "Sector 1" to "Sector 12".
    assert R.find("Sector 1")["id"] == "a"
    assert R.find("sector 12")["id"] == "b"
    R.set_regions([])


def test_regions_find_ambiguous_returns_none():
    import app.regions as R

    R.set_regions([
        {"id": "a", "name": "Sector 10", "center": [12.97, 77.59],
         "width_m": 100, "height_m": 100, "heading_deg": 0},
        {"id": "b", "name": "Sector 12", "center": [12.98, 77.60],
         "width_m": 100, "height_m": 100, "heading_deg": 0},
    ])
    # "Sector 1" prefixes BOTH and is not an exact/word-boundary match of either
    # -> ambiguous -> None (don't guess).
    assert R.find("Sector 1") is None
    R.set_regions([])


def test_pois_find_word_boundary_and_prefix():
    import app.pois as P

    P.set_pois([
        {"id": "lz1", "name": "LZ 1", "lat": 12.97, "lng": 77.59},
        {"id": "lz2", "name": "LZ 2", "lat": 12.98, "lng": 77.60},
    ])
    assert P.find("LZ 1")["id"] == "lz1"  # exact
    assert P.find("nope") is None
    P.set_pois([{"id": "north", "name": "North Gate", "lat": 12.97, "lng": 77.59}])
    # Word-boundary: "north" is a whole word in the single name -> match.
    assert P.find("North")["id"] == "north"
    P.set_pois([])


# ── H2: coordinated survey clips each zone to the SOURCE polygon ──────────────
def test_clip_zone_to_polygon_trims_to_source():
    from app.survey.coordinated import clip_zone_to_polygon

    lat0, lon0 = 12.97, 77.59
    # A 100 m square zone (the bbox strip).
    zone = [
        _o(lat0, lon0, 0, 0), _o(lat0, lon0, 0, 100),
        _o(lat0, lon0, 100, 100), _o(lat0, lon0, 100, 0),
    ]
    # Source polygon: the WEST half only (a 50 m-wide column).
    src = [
        _o(lat0, lon0, 0, 0), _o(lat0, lon0, 0, 50),
        _o(lat0, lon0, 100, 50), _o(lat0, lon0, 100, 0),
    ]
    clipped = clip_zone_to_polygon(zone, src)
    easts = [math.radians(p[1] - lon0) * _R * math.cos(math.radians(lat0)) for p in clipped]
    assert max(easts) < 55.0, f"zone must be clipped to the source's 50 m east extent: {easts}"
    assert min(easts) < 5.0


def test_clip_zone_outside_polygon_keeps_strip():
    from app.survey.coordinated import clip_zone_to_polygon

    lat0, lon0 = 12.97, 77.59
    zone = [
        _o(lat0, lon0, 0, 0), _o(lat0, lon0, 0, 10),
        _o(lat0, lon0, 10, 10), _o(lat0, lon0, 10, 0),
    ]
    # Source far away (no overlap) -> clip removes everything -> fall back to
    # the original strip so the drone still flies SOMETHING (never None).
    src = [
        _o(lat0, lon0, 1000, 1000), _o(lat0, lon0, 1000, 1100),
        _o(lat0, lon0, 1100, 1100), _o(lat0, lon0, 1100, 1000),
    ]
    assert clip_zone_to_polygon(zone, src) == zone
    # Empty / degenerate source -> zone unchanged.
    assert clip_zone_to_polygon(zone, []) == zone


_R = 6378137.0


def _o(lat0, lon0, north_m, east_m):
    lat = lat0 + math.degrees(north_m / _R)
    lon = lon0 + math.degrees(east_m / (_R * math.cos(math.radians(lat0))))
    return lat, lon
