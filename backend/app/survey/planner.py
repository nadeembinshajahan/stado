"""Generate a lawnmower (boustrophedon) survey path over a polygon.

The polygon arrives as map vertices (lat/lon). We project to a local metric
plane, optionally rotate so the scan lines run along a chosen heading, sweep
parallel lines clipped to the polygon, and project the turn points back to
lat/lon as NAV_WAYPOINTs.
"""
from __future__ import annotations

import math

from ..mavlink.missions import Waypoint

_EARTH_R = 6378137.0


def _project(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    x = math.radians(lon - lon0) * _EARTH_R * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * _EARTH_R
    return x, y


def _unproject(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    lat = lat0 + math.degrees(y / _EARTH_R)
    lon = lon0 + math.degrees(x / (_EARTH_R * math.cos(math.radians(lat0))))
    return lat, lon


def _rotate(x: float, y: float, ang: float) -> tuple[float, float]:
    c, s = math.cos(ang), math.sin(ang)
    return x * c - y * s, x * s + y * c


def _polygon_area(pts: list[tuple[float, float]]) -> float:
    """Signed area (shoelace) of a projected x/y polygon. >0 = CCW."""
    a = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def _convex_hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone-chain convex hull of projected x/y points (CCW)."""
    uniq = sorted(set(pts))
    if len(uniq) < 3:
        return uniq

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in uniq:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(uniq):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _segments_intersect(p1, p2, p3, p4) -> bool:
    """True if open segment p1p2 properly crosses p3p4 (shared endpoints OK)."""
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])

    d1 = ccw(p3, p4, p1)
    d2 = ccw(p3, p4, p2)
    d3 = ccw(p1, p2, p3)
    d4 = ccw(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    return False


def _is_self_intersecting(pts: list[tuple[float, float]]) -> bool:
    """True if the closed ring has any pair of non-adjacent edges that cross."""
    n = len(pts)
    if n < 4:
        return False
    for i in range(n):
        a1, a2 = pts[i], pts[(i + 1) % n]
        for j in range(i + 1, n):
            # Skip adjacent edges (they share an endpoint) and the wrap pair.
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            b1, b2 = pts[j], pts[(j + 1) % n]
            if _segments_intersect(a1, a2, b1, b2):
                return True
    return False


def clean_polygon(
    polygon: list[tuple[float, float]],
    hull_fallback: bool = True,
) -> list[tuple[float, float]]:
    """Tidy operator-edited survey vertices into a CLEAN, simple closed polygon.

    Steps (all in a local metric projection so the geometry is metre-accurate):
      1. drop a duplicate trailing vertex (an explicitly-closed ring),
      2. dedupe near-coincident consecutive points (<0.5 m apart),
      3. require >=3 distinct points,
      4. if the ring self-intersects (a dragged vertex crossed an edge),
         replace it with its convex hull so it "wraps cleanly" around the points
         (when `hull_fallback`; otherwise raise),
      5. enforce a consistent counter-clockwise winding.

    Returns an OPEN list of (lat, lon) vertices (no repeated first point), ready
    for `plan_survey`. Raises ValueError if the points can't form a polygon.
    """
    if not polygon or len(polygon) < 3:
        raise ValueError("survey polygon needs at least 3 vertices")

    lat0, lon0 = polygon[0]
    proj = [_project(lat, lon, lat0, lon0) for lat, lon in polygon]

    # 1+2) drop an explicit closing vertex, then collapse near-duplicates.
    cleaned: list[tuple[float, float]] = []
    for x, y in proj:
        if cleaned:
            px, py = cleaned[-1]
            if math.hypot(x - px, y - py) < 0.5:
                continue
        cleaned.append((x, y))
    # Closing duplicate (first ≈ last)?
    if len(cleaned) >= 2:
        fx, fy = cleaned[0]
        lx, ly = cleaned[-1]
        if math.hypot(lx - fx, ly - fy) < 0.5:
            cleaned.pop()

    if len(cleaned) < 3:
        raise ValueError("survey polygon needs at least 3 distinct vertices")

    # 4) untangle a self-intersecting ring via its convex hull.
    if _is_self_intersecting(cleaned):
        if not hull_fallback:
            raise ValueError("survey polygon edges self-intersect")
        cleaned = _convex_hull(cleaned)
        if len(cleaned) < 3:
            raise ValueError("survey polygon collapses to a line")

    # 5) enforce CCW winding so downstream sweeps are deterministic.
    if _polygon_area(cleaned) < 0:
        cleaned.reverse()

    return [_unproject(x, y, lat0, lon0) for x, y in cleaned]


def plan_survey(
    polygon: list[tuple[float, float]],
    altitude: float = 30.0,
    line_spacing_m: float = 20.0,
    heading_deg: float = 0.0,
    speed_ms: float | None = None,
) -> list[Waypoint]:
    """polygon: list of (lat, lon) vertices. Returns ordered NAV waypoints."""
    if len(polygon) < 3:
        raise ValueError("survey polygon needs at least 3 vertices")

    lat0, lon0 = polygon[0]
    pts = [_project(lat, lon, lat0, lon0) for lat, lon in polygon]

    # Rotate so scan lines are horizontal in the working frame.
    ang = -math.radians(heading_deg)
    rpts = [_rotate(x, y, ang) for x, y in pts]

    ys = [p[1] for p in rpts]
    y_min, y_max = min(ys), max(ys)

    n_edges = len(rpts)
    legs: list[tuple[tuple[float, float], tuple[float, float]]] = []
    flip = False

    # Build the list of sweep-line offsets. A zone whose perpendicular extent is
    # <= the line spacing would otherwise place its first line at
    # y_min + spacing/2 >= y_max, so `while y < y_max` never runs and the zone
    # gets ZERO waypoints (silent under-coverage on a 2-drone split). Guarantee
    # at least one center pass for such thin zones.
    if y_max - y_min <= line_spacing_m:
        ys_to_scan = [(y_min + y_max) / 2.0]
    else:
        ys_to_scan = []
        y = y_min + line_spacing_m / 2.0
        while y < y_max:
            ys_to_scan.append(y)
            y += line_spacing_m

    for y in ys_to_scan:
        xs: list[float] = []
        for i in range(n_edges):
            x1, y1 = rpts[i]
            x2, y2 = rpts[(i + 1) % n_edges]
            if (y1 <= y < y2) or (y2 <= y < y1):
                t = (y - y1) / (y2 - y1)
                xs.append(x1 + t * (x2 - x1))
        xs.sort()
        # Pair crossings into interior spans; keep the widest as the survey leg.
        for j in range(0, len(xs) - 1, 2):
            a, b = xs[j], xs[j + 1]
            if b - a < 1e-3:
                continue
            p_in, p_out = (a, y), (b, y)
            if flip:
                p_in, p_out = p_out, p_in
            legs.append((p_in, p_out))
        flip = not flip

    inv = -ang
    waypoints: list[Waypoint] = []
    for p_in, p_out in legs:
        for px, py in (p_in, p_out):
            ux, uy = _rotate(px, py, inv)
            lat, lon = _unproject(ux, uy, lat0, lon0)
            waypoints.append(Waypoint(lat=lat, lon=lon, alt=altitude, param2=2.0))
    return waypoints
