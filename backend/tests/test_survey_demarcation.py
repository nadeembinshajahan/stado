"""Offline, deterministic tests for the survey-perimeter projection math.

These guard the *demarcation accuracy* of the survey-area detector — the part
that turns VLM/Delineate pixel polygons into lat/lon. No network, no model:
pure geometry. They catch the two classes of bug that move a survey onto the
WRONG footprint:

  1. `_infer_scale` mis-detecting the coordinate full-scale (0-1 vs 0-100 vs
     0-1000 vs raw pixels) → polygons squished into a corner or blown up.
  2. A pixel→lat/lon projection / axis-order error → mirrored or N–S-drifted
     polygons. We round-trip a known pixel↔lat/lon through the Web-Mercator tile
     math and assert it returns to the same place to <1 m.

Plus the regression that actually bit the deployed browser path: the bounds the
frontend sends MUST describe the fetched tile, not the map viewport — otherwise
the linear pixel mapping stretches every polygon. We assert the backend's tile
bounds are self-consistent so the frontend mirror (staticTileBounds in
PerimeterPlanner.tsx) has a fixed contract to match.

Run: cd backend && PYTHONPATH=. .venv/bin/python -m pytest tests/test_survey_demarcation.py -q
"""
from __future__ import annotations

import math

from app.survey_vision import (
    Bounds,
    _infer_scale,
    _inv_mercator_y,
    _mercator_y,
    _polygon_to_latlon,
    _static_maps_bounds,
)

_R = 6378137.0


def _meters_between(lat1, lon1, lat2, lon2) -> float:
    """Small-angle planar distance in metres (fine for <1 km separations)."""
    dlat = math.radians(lat2 - lat1) * _R
    dlon = math.radians(lon2 - lon1) * _R * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dlat, dlon)


# ── _infer_scale: must pick the right full-scale, never collapse ──────────────
def test_infer_scale_picks_0_1000_when_requested():
    pts = [(0, 0), (1000, 0), (1000, 1000), (0, 1000)]
    assert _infer_scale(pts) == 1000.0


def test_infer_scale_detects_normalized_0_1():
    pts = [(0.05, 0.10), (0.95, 0.10), (0.95, 0.90), (0.05, 0.90)]
    assert _infer_scale(pts) == 1.0


def test_infer_scale_detects_0_100():
    pts = [(5.0, 10.0), (95.0, 10.0), (95.0, 90.0), (5.0, 90.0)]
    assert _infer_scale(pts) == 100.0


def test_infer_scale_raw_pixels_above_1000():
    # e.g. a 1280px scale=2 image where the model echoed raw pixels.
    pts = [(40, 40), (1240, 40), (1240, 1240), (40, 1240)]
    assert _infer_scale(pts) == 1240.0


def test_infer_scale_does_not_collapse_small_polygon():
    """A small-but-real 0-1000 polygon (max coord ~120) must NOT be read as 0-100
    and blown up to fill the whole tile — the classic 'squished/misplaced' misfire.
    Here max is 120 → 0-1000 scale keeps it small, 0-100 would clamp/oversize it.
    We assert the 1000-scale reading keeps it a small fraction of the image."""
    pts = [(80, 60), (120, 60), (120, 110), (80, 110)]
    # max is 120 → 0-1000 bucket (since 120 > 100). Spans <5% of the tile.
    assert _infer_scale(pts) == 1000.0
    b = Bounds(north=1.0, south=0.0, east=1.0, west=0.0)
    poly = _polygon_to_latlon(pts, b)
    lons = [p[1] for p in poly]
    assert max(lons) - min(lons) < 0.06, "small 0-1000 polygon must stay small"


# ── projection: axis order, no mirroring ──────────────────────────────────────
def test_projection_axis_order():
    """x→lon (W→E), y→lat (N→S). Top-left pixel = NW corner; bottom-right = SE."""
    b = Bounds(north=13.10, south=13.00, east=77.70, west=77.60)
    poly = _polygon_to_latlon([(0, 0), (1000, 1000)], b)
    (tl_lat, tl_lon), (br_lat, br_lon) = poly
    assert abs(tl_lat - 13.10) < 1e-6 and abs(tl_lon - 77.60) < 1e-6, "top-left=NW"
    assert abs(br_lat - 13.00) < 1e-6 and abs(br_lon - 77.70) < 1e-6, "bot-right=SE"


def test_projection_not_mirrored_east_west():
    b = Bounds(north=13.10, south=13.00, east=77.70, west=77.60)
    # A point on the right half of the image must be EAST of one on the left.
    (_, left_lon), (_, right_lon) = _polygon_to_latlon([(100, 500), (900, 500)], b)
    assert right_lon > left_lon, "image-right must map further east"


def test_projection_clamps_out_of_range():
    b = Bounds(north=13.10, south=13.00, east=77.70, west=77.60)
    poly = _polygon_to_latlon([(-50, -50), (1200, 1200)], b)
    for lat, lon in poly:
        assert 13.00 - 1e-9 <= lat <= 13.10 + 1e-9
        assert 77.60 - 1e-9 <= lon <= 77.70 + 1e-9


# ── Mercator helpers round-trip ───────────────────────────────────────────────
def test_mercator_y_round_trips():
    for lat in (-60.0, -13.05, 0.0, 13.078065, 51.5, 60.0):
        assert abs(_inv_mercator_y(_mercator_y(lat)) - lat) < 1e-9


# ── _static_maps_bounds: shape + pixel↔latlon round-trip ──────────────────────
def test_static_maps_bounds_ordering():
    b = _static_maps_bounds(13.078065, 77.644651, 18, 640)
    assert b.north > b.south, "north above south"
    assert b.east > b.west, "east right of west"
    # Tile is roughly square in ground extent at this latitude.
    span_lat_m = _meters_between(b.south, b.west, b.north, b.west)
    span_lon_m = _meters_between(b.south, b.west, b.south, b.east)
    assert abs(span_lat_m - span_lon_m) / span_lon_m < 0.05, "near-square tile"


def test_static_maps_bounds_center_is_request_center():
    """The tile center must be the requested (lat, lon) — interpolated through
    the SAME Mercator y-space the projection uses (not a naive degree midpoint)."""
    lat, lon = 13.078065, 77.644651
    b = _static_maps_bounds(lat, lon, 18, 640)
    # Center pixel (0.5, 0.5) of the tile must project back to (lat, lon).
    (clat, clon), = _polygon_to_latlon([(500, 500)], b)
    assert _meters_between(lat, lon, clat, clon) < 0.5, (clat, clon)


def test_pixel_latlon_round_trip_under_1m():
    """Round-trip several known pixels through bounds→latlon and back to pixels;
    must return to the same spot to well under a metre. This is the master guard
    against any axis/scale/projection regression."""
    lat, lon, zoom, size = 13.078065, 77.644651, 18, 640
    b = _static_maps_bounds(lat, lon, zoom, size)
    y_north = _mercator_y(b.north)
    y_south = _mercator_y(b.south)
    for px, py in [(0, 0), (1000, 1000), (250, 750), (800, 120), (500, 500)]:
        (la, lo), = _polygon_to_latlon([(px, py)], b)
        # invert: latlon → normalized 0-1000 (the projection's true inverse)
        nx = (lo - b.west) / (b.east - b.west)
        ny = (_mercator_y(la) - y_north) / (y_south - y_north)
        assert abs(nx * 1000 - px) < 0.5, (px, nx * 1000)
        assert abs(ny * 1000 - py) < 0.5, (py, ny * 1000)


# ── the deployed-path regression: tile bounds, NOT viewport bounds ────────────
def test_tile_bounds_match_image_extent_not_viewport():
    """Regression: the frontend used to send map.getBounds() (the full viewport)
    while fetching only a 640-tile, so polygons were stretched ~3x. The contract
    is now: bounds describe the FETCHED tile (center+zoom+logical size). Here we
    assert a polygon spanning the full tile (image edges) lands exactly on the
    tile's N/S/E/W edges — i.e. no stretch."""
    lat, lon, zoom, size = 13.078065, 77.644651, 18, 640
    b = _static_maps_bounds(lat, lon, zoom, size)
    poly = _polygon_to_latlon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)], b)
    lats = [p[0] for p in poly]
    lons = [p[1] for p in poly]
    assert abs(max(lats) - b.north) < 1e-6 and abs(min(lats) - b.south) < 1e-6
    assert abs(max(lons) - b.east) < 1e-6 and abs(min(lons) - b.west) < 1e-6


def test_viewport_bounds_would_have_stretched_polygons():
    """Document the OLD bug quantitatively: had we projected a tile-pixel polygon
    across a (wider) viewport's bounds, an image-edge vertex would land hundreds
    of metres off. Guards against anyone re-introducing viewport bounds."""
    lat, lon, zoom = 13.078065, 77.644651, 18
    tile = _static_maps_bounds(lat, lon, zoom, 640)        # what the image shows
    viewport = _static_maps_bounds(lat, lon, zoom, 1900)   # a wide map div
    # A vertex at the image's right edge SHOULD be tile.east; across the viewport
    # bounds it lands at viewport.east — far to the east.
    err = _meters_between(lat, tile.east, lat, viewport.east)
    assert err > 150.0, f"viewport-bounds mismatch should be large, got {err:.0f} m"
