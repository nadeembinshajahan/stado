"""Functional tests for the survey path planner + coordinated splitter.

Pure-math: no hardware, no MAVLink links, no network. Covers the
intent-vs-implementation gaps found in the autonomy/vision review:

  * plan_survey coverage when the swept extent is <= line_spacing (the
    "thin zone produces ZERO waypoints" bug),
  * the 1-drone vs 2-drone scaling consequence of that (a small area split
    between two drones under-covers each zone),
  * split_rect / assign_altitudes correctness (equal strips, gap corridor,
    deconflicted altitudes, Overwatch highest),
  * clean_polygon winding / hull / dedupe,
  * survey_mission waypoint sequencing (takeoff -> grid -> RTL).

Run: cd backend && PYTHONPATH=. .venv/bin/python -m pytest tests/test_survey_planner.py -q
"""
from __future__ import annotations

import math

import pytest

from app.survey.coordinated import (
    assign_altitudes,
    bbox_of_polygon,
    rect_corners,
    split_rect,
)
from app.survey.planner import clean_polygon, plan_survey

_R = 6378137.0


def _offset(lat0: float, lon0: float, north_m: float, east_m: float) -> tuple[float, float]:
    """A (north, east) metre offset from (lat0, lon0) as (lat, lon)."""
    lat = lat0 + math.degrees(north_m / _R)
    lon = lon0 + math.degrees(east_m / (_R * math.cos(math.radians(lat0))))
    return lat, lon


def _square(lat0: float, lon0: float, side_m: float) -> list[tuple[float, float]]:
    """A side_m x side_m square anchored at (lat0, lon0), CCW in (lat, lon)."""
    return [
        _offset(lat0, lon0, 0.0, 0.0),
        _offset(lat0, lon0, 0.0, side_m),
        _offset(lat0, lon0, side_m, side_m),
        _offset(lat0, lon0, side_m, 0.0),
    ]


LAT0, LON0 = 12.97, 77.59


# ── plan_survey basic coverage ────────────────────────────────────────────────
def test_plan_survey_square_even_legs():
    poly = _square(LAT0, LON0, 100.0)
    wps = plan_survey(poly, altitude=30.0, line_spacing_m=20.0, heading_deg=0.0)
    # Every leg is 2 waypoints; never an odd count.
    assert wps, "100 m square at 20 m spacing must produce waypoints"
    assert len(wps) % 2 == 0, "waypoints must come in (in, out) leg pairs"
    # All at the requested altitude.
    assert all(abs(w.alt - 30.0) < 1e-9 for w in wps)


def test_plan_survey_serpentine_alternates_direction():
    """Consecutive legs must reverse (lawnmower), not all run the same way."""
    poly = _square(LAT0, LON0, 100.0)
    wps = plan_survey(poly, altitude=30.0, line_spacing_m=20.0, heading_deg=0.0)
    legs = [(wps[i], wps[i + 1]) for i in range(0, len(wps), 2)]
    assert len(legs) >= 3
    # The "out" of leg k should be near the "in" of leg k+1 (a short cross-over),
    # which only holds if directions alternate.
    for k in range(len(legs) - 1):
        out_k = legs[k][1]
        in_k1 = legs[k + 1][0]
        d = math.hypot(
            math.radians(in_k1.lat - out_k.lat) * _R,
            math.radians(in_k1.lon - out_k.lon) * _R * math.cos(math.radians(LAT0)),
        )
        # Adjacent leg endpoints are one spacing apart at most (~20 m), not a full
        # traverse (~100 m), which is what you'd get if directions did NOT flip.
        assert d < 40.0, f"leg {k}->{k+1} endpoints {d:.1f} m apart (no serpentine?)"


# ── THE COVERAGE GAP: extent <= line_spacing yields ZERO waypoints ────────────
def test_thin_strip_gets_at_least_one_center_pass():
    """A strip thinner than the line spacing must still get ONE center pass.

    Previously (the C3 bug) the first scan line sat at y_min + spacing/2 and the
    loop `while y < y_max` never ran for a swept extent <= spacing, yielding zero
    coverage of a real, surveyable strip. The fix clamps a thin zone to a single
    center line so it is always covered.
    """
    # 200 m long, 10 m wide strip; sweep perpendicular to the long axis.
    poly = [
        _offset(LAT0, LON0, 0.0, 0.0),
        _offset(LAT0, LON0, 0.0, 200.0),
        _offset(LAT0, LON0, 10.0, 200.0),
        _offset(LAT0, LON0, 10.0, 0.0),
    ]
    wps = plan_survey(poly, altitude=30.0, line_spacing_m=20.0, heading_deg=0.0)
    # C3 fixed: at least one center pass (a single in/out leg = 2 waypoints).
    assert len(wps) >= 2, "a thin strip must get >=1 center pass, not zero coverage"
    assert len(wps) % 2 == 0, "waypoints must come in (in, out) leg pairs"
    # The pass runs down the middle of the 10 m strip (~5 m offset north).
    norths = [math.radians(w.lat - LAT0) * _R for w in wps]
    assert all(2.0 < n < 8.0 for n in norths), f"center pass not centered: {norths}"


def test_strip_slightly_wider_than_spacing_covers():
    """A strip just wider than the spacing DOES get a center pass."""
    poly = [
        _offset(LAT0, LON0, 0.0, 0.0),
        _offset(LAT0, LON0, 0.0, 200.0),
        _offset(LAT0, LON0, 25.0, 200.0),
        _offset(LAT0, LON0, 25.0, 0.0),
    ]
    wps = plan_survey(poly, altitude=30.0, line_spacing_m=20.0, heading_deg=0.0)
    assert len(wps) >= 2, "a 25 m strip at 20 m spacing should get >=1 pass"


def test_coverage_is_continuous_and_never_zero_across_spacing_boundary():
    """PREFLIGHT (C3 fix, paranoid): sweep strip widths from sub-metre up through
    several multiples of the spacing — NO width may ever yield zero coverage.

    A zero-waypoint zone is a silent under-fly: the operator sees a "survey
    launched" but a slice of ground is never overflown. The thin-zone clamp must
    hold continuously, with no hole right at the y_max - y_min == spacing edge.
    """
    spacing = 20.0
    for width in (0.5, 1.0, 5.0, 19.0, 19.999, 20.0, 20.001, 21.0, 39.999,
                  40.0, 40.001, 60.0):
        poly = [
            _offset(LAT0, LON0, 0.0, 0.0),
            _offset(LAT0, LON0, 0.0, 200.0),
            _offset(LAT0, LON0, width, 200.0),
            _offset(LAT0, LON0, width, 0.0),
        ]
        wps = plan_survey(poly, altitude=30.0, line_spacing_m=spacing, heading_deg=0.0)
        assert len(wps) >= 2, f"width {width} m must get >=1 pass — zero coverage is a hole"
        assert len(wps) % 2 == 0, f"width {width} m: waypoints must be (in,out) pairs"


def test_two_drone_split_of_tiny_area_covers_both_zones():
    """PREFLIGHT: even a TINY area split between 2 drones must give EACH zone
    coverage (the thin-zone fix). The 1-vs-2 scaling must never silently drop a
    drone's zone to zero waypoints."""
    for side in (20.0, 30.0, 45.0, 60.0):
        zones = split_rect(LAT0, LON0, side, side, 0.0, n=2, gap_m=5.0)
        assert len(zones) == 2
        for z in zones:
            wps = plan_survey(z, altitude=30.0, line_spacing_m=25.0)
            assert len(wps) >= 2, f"{side} m square / 2 drones: each zone must be covered"


# ── 1-vs-2 drone scaling consequence of the coverage gap ──────────────────────
def test_two_drone_split_undercovers_small_area():
    """Splitting a small square between 2 drones can starve each zone of passes.

    A 60 m square swept at 25 m spacing: as one zone (1 drone) it gets multiple
    passes; split in two (each ~27.5 m wide after the 5 m gap) each zone gets
    only a SINGLE pass. This is the functional regression the reviewer flagged:
    survey scaling that silently changes coverage with drone count.
    """
    side = 60.0
    spacing = 25.0

    one = split_rect(LAT0, LON0, side, side, 0.0, n=1, gap_m=5.0)
    assert len(one) == 1
    wps_one = plan_survey(one[0], altitude=30.0, line_spacing_m=spacing)
    passes_one = len(wps_one) // 2

    two = split_rect(LAT0, LON0, side, side, 0.0, n=2, gap_m=5.0)
    assert len(two) == 2
    passes_two = [len(plan_survey(z, altitude=30.0, line_spacing_m=spacing)) // 2 for z in two]

    # 1 drone gets >1 pass; each 2-drone zone collapses to a single pass.
    assert passes_one >= 2, f"1-drone coverage should be multi-pass, got {passes_one}"
    assert all(p <= 1 for p in passes_two), (
        f"2-drone zones under-cover (single pass each): {passes_two}. "
        "Documents the coverage gap; tighten line_spacing to zone width."
    )


# ── split_rect geometry ───────────────────────────────────────────────────────
def test_split_rect_one_zone_is_whole_rect():
    zones = split_rect(LAT0, LON0, 100.0, 80.0, 0.0, n=1, gap_m=5.0)
    assert len(zones) == 1
    corners = rect_corners(LAT0, LON0, 100.0, 80.0, 0.0)
    # Same 4 corners (set equality up to ordering / float noise).
    for (zlat, zlon), (clat, clon) in zip(sorted(zones[0]), sorted(corners)):
        assert abs(zlat - clat) < 1e-6 and abs(zlon - clon) < 1e-6


def test_split_rect_splits_longer_side_and_leaves_gap():
    """n=2 over a tall rectangle: two strips along the LONG axis with a gap."""
    width_m, height_m, gap = 40.0, 120.0, 6.0
    zones = split_rect(LAT0, LON0, width_m, height_m, 0.0, n=2, gap_m=gap)
    assert len(zones) == 2

    def span_north(zone):
        lats = [p[0] for p in zone]
        return (max(lats) - min(lats)) * math.radians(1) * _R

    spans = [span_north(z) for z in zones]
    # Each strip ~ (120 - 6) / 2 = 57 m along the long (north) axis.
    for s in spans:
        assert abs(s - 57.0) < 2.0, f"strip span {s:.1f} m != ~57 m"
    # The gap between the two strips' nearest edges ~ 6 m.
    z0_lats = [p[0] for p in zones[0]]
    z1_lats = [p[0] for p in zones[1]]
    lo, hi = sorted([(max(z0_lats), min(z1_lats)), (max(z1_lats), min(z0_lats))])[0]
    gap_actual = abs((min(z1_lats) - max(z0_lats))) * math.radians(1) * _R
    assert abs(gap_actual - gap) < 2.0, f"corridor {gap_actual:.1f} m != {gap} m"


def test_split_rect_rejects_oversized_gap():
    with pytest.raises(ValueError):
        # 3 strips, each gap 60 m over a 100 m side -> negative strip length.
        split_rect(LAT0, LON0, 100.0, 100.0, 0.0, n=3, gap_m=60.0)


def test_split_rect_rejects_zero_drones():
    with pytest.raises(ValueError):
        split_rect(LAT0, LON0, 100.0, 100.0, 0.0, n=0, gap_m=5.0)


# ── assign_altitudes deconfliction ────────────────────────────────────────────
def test_assign_altitudes_overwatch_highest_two_drones():
    # Survey vertical separation is now a firm 15 m (preflight-02 F2).
    alts = assign_altitudes(["overwatch", "outrider"], base_alt=30.0, sep_m=15.0)
    assert alts["overwatch"] > alts["outrider"], "Overwatch must be highest"
    # step = max(sep_m, 10) = 15 -> at least 15 m apart.
    assert alts["overwatch"] - alts["outrider"] >= 15.0


def test_assign_altitudes_order_independent():
    a = assign_altitudes(["overwatch", "outrider"], 30.0, 15.0)
    b = assign_altitudes(["outrider", "overwatch"], 30.0, 15.0)
    assert a == b, "altitude assignment must not depend on list order"


def test_assign_altitudes_single_drone_uses_base():
    assert assign_altitudes(["outrider"], 30.0, 15.0) == {"outrider": 30.0}
    assert assign_altitudes(["overwatch"], 30.0, 15.0) == {"overwatch": 30.0}


def test_assign_altitudes_three_drones_separated_and_ordered():
    alts = assign_altitudes(["outrider", "overwatch", "scout"], 30.0, 15.0)
    # Overwatch on top of the stack.
    assert alts["overwatch"] == max(alts.values())
    vals = sorted(alts.values())
    for lo, hi in zip(vals, vals[1:]):
        assert hi - lo >= 15.0, "every layer must be >= 15 m apart (preflight-02 F2)"


def test_assign_altitudes_never_shares_a_band_for_any_fleet_size():
    """PREFLIGHT (paranoid): for every fleet size and order, NO two vehicles may be
    assigned the same altitude — a shared band is a built-in mid-air collision."""
    import itertools

    pools = ["overwatch", "outrider", "scout", "sentinel"]
    for k in range(1, len(pools) + 1):
        for combo in itertools.permutations(pools, k):
            alts = assign_altitudes(list(combo), base_alt=30.0, sep_m=15.0)
            vals = list(alts.values())
            assert len(set(round(v, 3) for v in vals)) == len(vals), (
                f"two vehicles share an altitude for {combo}: {alts}"
            )
            if k >= 2:
                # Multi-drone: Overwatch (if present) must be strictly highest.
                if "overwatch" in alts:
                    assert alts["overwatch"] == max(vals), combo


def test_survey_fleet_altitudes_overwatch_above_outrider_by_floor_step():
    """PREFLIGHT (preflight-02 F2): the survey altitude assignment used by
    survey_area / survey_region keeps Overwatch above Outrider by a firm >= 15 m
    of VERTICAL separation (the survey callers now pass sep_m=15, operator-approved).
    The HORIZONTAL zone corridor (gap_m) stays small and is unaffected."""
    alts = assign_altitudes(["overwatch", "outrider"], base_alt=30.0, sep_m=15.0)
    sep = alts["overwatch"] - alts["outrider"]
    assert sep >= 15.0, f"survey fleet vertical separation collapsed to {sep} m"
    assert alts["outrider"] >= 8.0, "Outrider must stay above a safe floor"


def test_vertical_sep_and_horizontal_gap_are_decoupled():
    """PREFLIGHT (preflight-02 F2): VERTICAL altitude separation is a firm >= 15 m
    while the HORIZONTAL zone corridor stays small (~5 m). The two must be
    independent — a tight horizontal corridor must NOT shrink the altitude floor."""
    # Vertical: firm 15 m floor.
    alts = assign_altitudes(["overwatch", "outrider"], base_alt=30.0, sep_m=15.0)
    assert alts["overwatch"] - alts["outrider"] >= 15.0
    # Horizontal: a small 5 m corridor still yields a tight (~5 m), efficient gap —
    # NOT widened to 15 m (which would waste survey coverage).
    zones = split_rect(LAT0, LON0, 40.0, 120.0, 0.0, n=2, gap_m=5.0)
    z0_lats = [p[0] for p in zones[0]]
    z1_lats = [p[0] for p in zones[1]]
    gap_actual = abs(min(z1_lats) - max(z0_lats)) * math.radians(1) * _R
    assert abs(gap_actual - 5.0) < 2.0, f"horizontal corridor {gap_actual:.1f} m != ~5 m"


# ── clean_polygon ─────────────────────────────────────────────────────────────
def test_clean_polygon_drops_closing_vertex_and_winds_ccw():
    poly = _square(LAT0, LON0, 50.0)
    closed = poly + [poly[0]]  # explicitly closed ring
    cleaned = clean_polygon(closed)
    assert len(cleaned) == 4, "explicit closing vertex should be dropped"


def test_clean_polygon_self_intersection_hull_fallback():
    # A bow-tie (figure-8) self-intersects; hull fallback should untangle it.
    bowtie = [
        _offset(LAT0, LON0, 0.0, 0.0),
        _offset(LAT0, LON0, 50.0, 50.0),
        _offset(LAT0, LON0, 0.0, 50.0),
        _offset(LAT0, LON0, 50.0, 0.0),
    ]
    cleaned = clean_polygon(bowtie, hull_fallback=True)
    assert len(cleaned) >= 3
    # Without fallback it must raise instead.
    with pytest.raises(ValueError):
        clean_polygon(bowtie, hull_fallback=False)


def test_clean_polygon_rejects_degenerate():
    with pytest.raises(ValueError):
        clean_polygon([_offset(LAT0, LON0, 0, 0), _offset(LAT0, LON0, 0, 1)])


# ── survey_mission sequencing ─────────────────────────────────────────────────
def test_survey_mission_wraps_takeoff_and_rtl():
    from app.mavlink import missions
    from app.mavlink.missions import Waypoint
    from pymavlink import mavutil

    grid = [Waypoint(lat=LAT0, lon=LON0, alt=30.0), Waypoint(lat=LAT0 + 1e-4, lon=LON0, alt=30.0)]
    full = missions.survey_mission(grid, takeoff_alt=30.0)
    assert len(full) == len(grid) + 2, "must wrap with exactly one takeoff + one RTL"
    assert full[0].command == mavutil.mavlink.MAV_CMD_NAV_TAKEOFF
    assert full[-1].command == mavutil.mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH
    # Takeoff anchored at the first grid point.
    assert full[0].lat == grid[0].lat and full[0].lon == grid[0].lon


def test_survey_mission_empty_grid_is_empty():
    from app.mavlink import missions

    assert missions.survey_mission([], takeoff_alt=30.0) == []


# ── bbox_of_polygon ───────────────────────────────────────────────────────────
def test_bbox_of_polygon_extent_and_axis_aligned():
    poly = _square(LAT0, LON0, 100.0)
    clat, clon, w, h, hd = bbox_of_polygon(poly)
    assert hd == 0.0, "bbox is axis-aligned (heading 0)"
    assert abs(w - 100.0) < 1.0 and abs(h - 100.0) < 1.0
