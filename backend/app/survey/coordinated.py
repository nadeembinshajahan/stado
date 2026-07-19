"""Coordinated multi-drone area survey.

Given an oriented rectangular region and a fleet, split the rectangle into one
zone per drone (with a gap corridor so the zones never touch, keeping the drones
horizontally apart), plan a lawnmower survey per zone, and fly them concurrently
with vertical separation as a backup deconfliction (Overwatch highest).

Geometry note: positions are lat/lon. Local metric offsets use (north, east)
metres via `haversine_offset`, matching the rest of the MAVLink layer. An
oriented rectangle is built in a local frame whose +y axis points along
`heading_deg` (compass: 0 = north, 90 = east) and whose +x axis points 90° to
its right, then each local (right, fwd) corner is mapped to (east, north).
"""
from __future__ import annotations

import asyncio
import math

from ..mavlink import missions
from ..mavlink.commands import haversine_offset
from ..mavlink.registry import registry
from .planner import plan_survey

# Lowest altitude (AGL) we will stagger a drone down to while fitting a stack
# under the ceiling — MUST match coordination.MIN_ALT_M (the safe floor).
MIN_ALT_M = 3.0


def _local_to_latlon(
    center_lat: float, center_lon: float, right_m: float, fwd_m: float, heading_rad: float
) -> tuple[float, float]:
    """Map a local rectangle offset (right_m along +x, fwd_m along +y, where +y
    points at `heading`) to a lat/lon via north/east metres."""
    c, s = math.cos(heading_rad), math.sin(heading_rad)
    # +y (fwd) points at heading: north = fwd*cos(h), east = fwd*sin(h).
    # +x (right) is heading+90°:   north = right*cos(h+90)=-right*sin(h),
    #                              east  = right*sin(h+90)= right*cos(h).
    north = fwd_m * c - right_m * s
    east = fwd_m * s + right_m * c
    return haversine_offset(center_lat, center_lon, north, east)


def rect_corners(
    center_lat: float,
    center_lon: float,
    width_m: float,
    height_m: float,
    heading_deg: float = 0.0,
) -> list[tuple[float, float]]:
    """The 4 corners of an oriented rectangle centred on (center_lat, center_lon).

    `width_m` spans the local left/right (x) axis, `height_m` the forward/back
    (y) axis. `heading_deg` rotates the rectangle so its +y (height) axis points
    along that compass bearing. Corners are returned counter-clockwise as
    (lat, lon): back-left, back-right, front-right, front-left.
    """
    h = math.radians(heading_deg)
    hw, hh = width_m / 2.0, height_m / 2.0
    locals_ = [
        (-hw, -hh),  # back-left
        (hw, -hh),   # back-right
        (hw, hh),    # front-right
        (-hw, hh),   # front-left
    ]
    return [_local_to_latlon(center_lat, center_lon, rx, fy, h) for rx, fy in locals_]


def split_rect(
    center_lat: float,
    center_lon: float,
    width_m: float,
    height_m: float,
    heading_deg: float,
    n: int,
    gap_m: float,
) -> list[list[tuple[float, float]]]:
    """Split the oriented rectangle into `n` equal strips along its LONGER side,
    leaving a `gap_m` corridor between adjacent strips so zones never touch.

    Each strip is returned as a 4-corner (lat, lon) polygon. Splitting along the
    longer side gives each drone a long, efficient lawnmower lane. The total gap
    budget ((n-1)*gap_m) is removed from the usable length and the remainder is
    divided equally among the strips.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    h = math.radians(heading_deg)

    # Decide which local axis is the longer one and split along it. We work in
    # the local (right=x, fwd=y) frame, carve strips, then map corners to latlon.
    split_along_y = height_m >= width_m  # split the longer dimension
    long_len = height_m if split_along_y else width_m
    short_len = width_m if split_along_y else height_m

    total_gap = gap_m * (n - 1)
    strip_len = (long_len - total_gap) / n
    if strip_len <= 0:
        raise ValueError(
            f"gap_m={gap_m} too large for {n} strips over {long_len:.1f} m"
        )

    half_short = short_len / 2.0
    zones: list[list[tuple[float, float]]] = []
    # Strip i occupies [start_i, start_i + strip_len] along the long axis,
    # measured from one edge (- long_len/2) with gaps between strips.
    for i in range(n):
        lo = -long_len / 2.0 + i * (strip_len + gap_m)
        hi = lo + strip_len
        if split_along_y:
            # long axis = y (fwd), short axis = x (right)
            local_corners = [
                (-half_short, lo),
                (half_short, lo),
                (half_short, hi),
                (-half_short, hi),
            ]
        else:
            # long axis = x (right), short axis = y (fwd)
            local_corners = [
                (lo, -half_short),
                (hi, -half_short),
                (hi, half_short),
                (lo, half_short),
            ]
        zones.append(
            [_local_to_latlon(center_lat, center_lon, rx, fy, h) for rx, fy in local_corners]
        )
    return zones


def assign_altitudes(vehicles: list[str], base_alt: float, sep_m: float) -> dict[str, float]:
    """Assign each vehicle an altitude with at least `sep_m` vertical separation
    as a backup deconfliction. Overwatch is always highest. Other vehicles get
    `base_alt`, `base_alt + step`, ... in registry order, with Overwatch placed
    on top of the stack."""
    step = max(sep_m, 10.0)
    others = [v for v in vehicles if v != "overwatch"]
    alts: dict[str, float] = {}
    for i, v in enumerate(others):
        alts[v] = base_alt + i * step
    if "overwatch" in vehicles:
        top = (base_alt + (len(others) - 1) * step) if others else base_alt
        alts["overwatch"] = top + step if others else base_alt + step
        # When Overwatch is the only vehicle, keep it at base_alt.
        if not others:
            alts["overwatch"] = base_alt
    return alts


def assign_altitudes_capped(
    vehicles: list[str], base_alt: float, sep_m: float, override: bool = False,
    min_alt_m: float = MIN_ALT_M, context: str | None = None,
) -> dict[str, float]:
    """Like `assign_altitudes`, but FITS the staggered stack UNDER the runtime
    max-altitude CEILING (safety). The TOP drone (Overwatch) must not exceed the
    ceiling; the whole stack is lowered uniformly (separation preserved) so the
    top sits AT the ceiling when the requested base would push it over, and the
    bottom stays >= `min_alt_m`.

    Raises safety.CeilingExceeded when the ceiling is too low to fit the stack
    (top→bottom span) above the floor — there is no safe stagger, so refuse rather
    than silently collapse the separation. `override=True` bypasses + audits.

    No ceiling => identical to `assign_altitudes`.
    """
    from .. import safety

    alts = assign_altitudes(vehicles, base_alt, sep_m)
    ceiling = safety.get_max_altitude()
    if ceiling is None or not alts:
        return alts

    top = max(alts.values())
    bottom = min(alts.values())
    span = top - bottom  # total stack height (preserve it while lowering)

    # The stack can't fit above the floor under this ceiling — refuse/override.
    if ceiling < float(min_alt_m) + span:
        if override:
            safety._audit_override(top, ceiling, context or "staggered_takeoff")
            return alts
        raise safety.CeilingExceeded(float(min_alt_m) + span, ceiling)

    # Requested top is over the ceiling: lower the WHOLE stack so top == ceiling
    # (separation intact, bottom stays >= floor). override=True keeps the
    # requested (over-ceiling) altitudes but audits the bypass.
    if top > ceiling:
        if override:
            safety._audit_override(top, ceiling, context or "staggered_takeoff")
            return alts
        shift = top - ceiling
        return {v: a - shift for v, a in alts.items()}
    return alts


_R = 6378137.0


def _proj(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    x = math.radians(lon - lon0) * _R * math.cos(math.radians(lat0))
    y = math.radians(lat - lat0) * _R
    return x, y


def _unproj(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    lat = lat0 + math.degrees(y / _R)
    lon = lon0 + math.degrees(x / (_R * math.cos(math.radians(lat0))))
    return lat, lon


def clip_zone_to_polygon(
    zone: list[tuple[float, float]],
    source_polygon: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Clip a (convex, rectangular) zone against the SOURCE survey polygon so the
    fleet path covers the operator's actual footprint — not the bbox.

    Sutherland–Hodgman polygon clipping in a local metric projection, with the
    zone as the SUBJECT and each edge of the (assumed convex / convex-hull-clean)
    source polygon as a clip half-plane. Returns the clipped zone as a list of
    (lat, lon) vertices. If the source polygon is empty/degenerate or the clip
    removes everything (zone lies fully outside the polygon), the ORIGINAL zone
    is returned so the drone still flies its bbox strip rather than nothing.
    """
    if not source_polygon or len(source_polygon) < 3 or not zone:
        return zone
    lat0, lon0 = zone[0]
    subject = [_proj(lat, lon, lat0, lon0) for lat, lon in zone]
    clip = [_proj(lat, lon, lat0, lon0) for lat, lon in source_polygon]

    # Ensure the clip ring is CCW so "inside" is to the left of each edge.
    area = 0.0
    for i in range(len(clip)):
        x1, y1 = clip[i]
        x2, y2 = clip[(i + 1) % len(clip)]
        area += x1 * y2 - x2 * y1
    if area < 0:
        clip = list(reversed(clip))

    def inside(p, a, b) -> bool:
        # Left of (or on) the directed edge a->b.
        return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= -1e-6

    def intersect(p1, p2, a, b):
        # Intersection of segment p1->p2 with the infinite line a->b.
        x1, y1 = p1
        x2, y2 = p2
        x3, y3 = a
        x4, y4 = b
        den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
        if abs(den) < 1e-12:
            return p2
        t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))

    output = subject
    for i in range(len(clip)):
        a = clip[i]
        b = clip[(i + 1) % len(clip)]
        input_pts = output
        output = []
        if not input_pts:
            break
        prev = input_pts[-1]
        for cur in input_pts:
            cur_in = inside(cur, a, b)
            prev_in = inside(prev, a, b)
            if cur_in:
                if not prev_in:
                    output.append(intersect(prev, cur, a, b))
                output.append(cur)
            elif prev_in:
                output.append(intersect(prev, cur, a, b))
            prev = cur

    if len(output) < 3:
        return zone  # zone lies outside the polygon — keep the bbox strip
    return [_unproj(x, y, lat0, lon0) for x, y in output]


async def _plan_one(
    vehicle: str,
    zone: list[tuple[float, float]],
    alt: float,
    line_spacing_m: float,
    source_polygon: list[tuple[float, float]] | None = None,
) -> dict:
    """Plan, upload and start a survey for one (vehicle, zone). Returns a result
    dict; never raises — link/planning failures are reported per-vehicle.

    When `source_polygon` is given, the (rectangular) zone is first CLIPPED to
    that polygon so the surveyed footprint matches the operator's drawn area
    rather than its bounding box."""
    try:
        veh = registry.get(vehicle)
    except KeyError:
        return {"vehicle": vehicle, "error": f"unknown vehicle {vehicle}"}

    if source_polygon:
        zone = clip_zone_to_polygon(zone, source_polygon)
    polygon = [[float(lat), float(lon)] for lat, lon in zone]
    if not veh.link.snapshot().get("connected"):
        # Still return the planned lawnmower path so the UI can preview THIS
        # drone's grid in its zone even though we can't upload/fly it.
        grid = plan_survey(zone, altitude=alt, line_spacing_m=line_spacing_m)
        return {
            "vehicle": vehicle,
            "name": veh.name,
            "polygon": polygon,
            "altitude": alt,
            "waypoints": 0,
            "path": [[float(g.lat), float(g.lon)] for g in grid],
            "error": "link not connected",
        }

    grid = plan_survey(zone, altitude=alt, line_spacing_m=line_spacing_m)
    if not grid:
        return {
            "vehicle": vehicle,
            "name": veh.name,
            "polygon": polygon,
            "altitude": alt,
            "waypoints": 0,
            "path": [],
            "error": "survey produced no waypoints (check zone/spacing)",
        }
    # The ordered lawnmower turn points as a [lat, lon] polyline so the UI can
    # draw THIS drone's planned path inside its own zone, in its fleet color.
    path = [[float(g.lat), float(g.lon)] for g in grid]
    wps = missions.survey_mission(grid, alt)
    ok = await missions.upload(veh.link, wps)
    if not ok:
        return {
            "vehicle": vehicle,
            "name": veh.name,
            "polygon": polygon,
            "altitude": alt,
            "waypoints": len(wps),
            "path": path,
            "error": "mission upload rejected by vehicle",
        }
    await missions.start(veh.link)
    return {
        "vehicle": vehicle,
        "name": veh.name,
        "polygon": polygon,
        "altitude": alt,
        "waypoints": len(wps),
        "path": path,
    }


async def plan_and_fly(
    vehicles: list[str],
    zones: list[list[tuple[float, float]]],
    base_alt: float,
    line_spacing_m: float,
    sep_m: float,
    source_polygon: list[tuple[float, float]] | None = None,
) -> list[dict]:
    """Pair each vehicle with a zone, assign deconflicted altitudes (Overwatch
    highest, >= sep_m apart), then plan + upload + start each survey concurrently.

    When `source_polygon` is given, each (rectangular) zone is clipped to that
    polygon before planning so the surveyed footprint matches the operator's
    drawn area rather than its bounding box (the single-drone path already clips;
    this brings the fleet path to parity). Adding it as an OPTIONAL trailing
    keyword keeps the existing positional call signature in api.py unchanged.

    Returns one dict per vehicle (one per UNPAIRED vehicle too, with an error):
      {vehicle, name?, polygon:[[lat,lon],...], altitude, waypoints,
       path:[[lat,lon],...]  # the planned lawnmower turn points for this zone
       [, error]}
    """
    # H3: never silently drop a vehicle (or a zone). `zip` would truncate the
    # longer list — meaning a paired-off vehicle would NEVER fly with no error
    # surfaced. Pair what we can, then emit a clear per-vehicle error for any
    # vehicle left without a zone.
    n_pairs = min(len(vehicles), len(zones))
    paired = list(zip(vehicles[:n_pairs], zones[:n_pairs]))
    paired_vehicles = [v for v, _ in paired]
    alts = assign_altitudes(paired_vehicles, base_alt, sep_m)
    tasks = [
        _plan_one(v, zone, alts[v], line_spacing_m, source_polygon) for v, zone in paired
    ]
    results = list(await asyncio.gather(*tasks))
    # Surface any vehicle that got no zone (more vehicles than zones).
    for v in vehicles[n_pairs:]:
        results.append({
            "vehicle": v,
            "altitude": None,
            "waypoints": 0,
            "path": [],
            "error": "no survey zone assigned (more vehicles than zones)",
        })
    return results


def bbox_of_polygon(
    polygon: list[tuple[float, float]],
) -> tuple[float, float, float, float, float]:
    """Bounding box of a lat/lon polygon as
    (center_lat, center_lon, width_m, height_m, heading_deg).

    The box is axis-aligned (heading 0): width spans east/west, height spans
    north/south. Metric extents are computed from the lat/lon span at the
    centre latitude."""
    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    center_lat = (lat_min + lat_max) / 2.0
    center_lon = (lon_min + lon_max) / 2.0
    r = 6378137.0
    height_m = math.radians(lat_max - lat_min) * r
    width_m = math.radians(lon_max - lon_min) * r * math.cos(math.radians(center_lat))
    return center_lat, center_lon, width_m, height_m, 0.0
