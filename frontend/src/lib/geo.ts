// Small geodesic helpers for the fleet region tool. Kept dependency-free (plain
// equirectangular/haversine-style offsets) — accurate enough for the metre-scale
// rectangles the operator draws around a single site.

const R = 6378137; // WGS-84 mean earth radius (m)
const D2R = Math.PI / 180;
const R2D = 180 / Math.PI;

/** Offset a [lat, lon] by (north_m, east_m) using a local equirectangular approx. */
export function offsetLatLon(
  lat: number,
  lon: number,
  northM: number,
  eastM: number,
): [number, number] {
  const dLat = (northM / R) * R2D;
  const dLon = (eastM / (R * Math.cos(lat * D2R))) * R2D;
  return [lat + dLat, lon + dLon];
}

/**
 * Corners (CCW) of an oriented rectangle centered at [lat, lon], `widthM` along
 * the east-west axis and `heightM` along the north-south axis BEFORE rotation,
 * then rotated clockwise by `headingDeg` (0 = axis-aligned, heading points the
 * "height" axis). Returns [lat, lon] tuples — closed implicitly by the consumer.
 */
export function orientedRectCorners(
  lat: number,
  lon: number,
  widthM: number,
  heightM: number,
  headingDeg = 0,
): [number, number][] {
  const hw = Math.max(0, widthM) / 2;
  const hh = Math.max(0, heightM) / 2;
  // Local corners as (east, north) before rotation.
  const local: [number, number][] = [
    [-hw, -hh],
    [hw, -hh],
    [hw, hh],
    [-hw, hh],
  ];
  const h = headingDeg * D2R;
  const cos = Math.cos(h);
  const sin = Math.sin(h);
  return local.map(([e, n]) => {
    // Clockwise rotation of the (east, north) vector by the heading.
    const east = e * cos + n * sin;
    const north = -e * sin + n * cos;
    return offsetLatLon(lat, lon, north, east);
  });
}

// Per-vehicle zone colors. Named drones get their cockpit colors; everything
// else falls back through the palette by index.
const NAMED: Record<string, string> = {
  overwatch: "#22e3c4",
  outrider: "#ffb020",
};
const PALETTE = ["#22e3c4", "#ffb020", "#7c9cff", "#ff6fae", "#9be870", "#ff8a3d"];

/** Color for a drone zone, keyed by vehicle/name, falling back by index. */
export function zoneColor(nameOrId: string, index: number): string {
  const key = (nameOrId || "").toLowerCase();
  return NAMED[key] ?? PALETTE[index % PALETTE.length];
}

/**
 * Split an oriented rectangle into `n` equal strips along its LONGER side,
 * leaving a `gapM` corridor between adjacent strips so zones never touch.
 * Mirrors the backend `coordinated.split_rect` so the LIVE pre-commit preview
 * shows the same one-zone-per-drone division the backend will compute. Returns
 * each strip as a 4-corner [lat, lon] polygon (back-left, back-right,
 * front-right, front-left), CCW, respecting the region rotation.
 */
export function splitRectZones(
  centerLat: number,
  centerLon: number,
  widthM: number,
  heightM: number,
  headingDeg: number,
  n: number,
  gapM: number,
): [number, number][][] {
  if (n < 1) return [];
  const splitAlongY = heightM >= widthM; // split the longer dimension
  const longLen = splitAlongY ? heightM : widthM;
  const shortLen = splitAlongY ? widthM : heightM;
  const totalGap = gapM * (n - 1);
  const stripLen = (longLen - totalGap) / n;
  if (stripLen <= 0) return [];
  const halfShort = shortLen / 2;
  const zones: [number, number][][] = [];
  for (let i = 0; i < n; i++) {
    const lo = -longLen / 2 + i * (stripLen + gapM);
    const hi = lo + stripLen;
    // Local (x=width, y=height) corners for this strip.
    const local: [number, number][] = splitAlongY
      ? [
          [-halfShort, lo],
          [halfShort, lo],
          [halfShort, hi],
          [-halfShort, hi],
        ]
      : [
          [lo, -halfShort],
          [hi, -halfShort],
          [hi, halfShort],
          [lo, halfShort],
        ];
    zones.push(
      local.map(([x, y]) =>
        regionLocalToLatLon(x, y, centerLat, centerLon, headingDeg),
      ),
    );
  }
  return zones;
}

/**
 * Lawnmower (boustrophedon) survey polyline over a [lat, lon] polygon, mirroring
 * the backend `plan_survey` (default heading 0): project to a local east/north
 * plane around the first vertex, sweep horizontal scan lines (constant north)
 * spaced `lineSpacingM` apart, clip each to the polygon edges, alternate the
 * sweep direction, and unproject the turn points back to [lat, lon].
 *
 * Used to draw a LIVE per-zone preview of each drone's planned grid (before any
 * backend command), and as a fallback when the backend hasn't returned an actual
 * flown path for a zone. Returns ordered [lat, lon] turn points (open polyline).
 */
export function lawnmowerPath(
  poly: [number, number][],
  lineSpacingM = 20,
): [number, number][] {
  if (poly.length < 3) return [];
  const [lat0, lon0] = poly[0];
  const cosLat = Math.cos(lat0 * D2R);
  // Project each vertex to local (east, north) metres around the first vertex.
  const pts = poly.map(([lat, lon]) => {
    const east = (lon - lon0) * D2R * R * cosLat;
    const north = (lat - lat0) * D2R * R;
    return [east, north] as [number, number];
  });
  const ys = pts.map((p) => p[1]);
  const yMin = Math.min(...ys);
  const yMax = Math.max(...ys);
  const n = pts.length;
  const out: [number, number][] = [];
  let flip = false;
  const spacing = Math.max(1, lineSpacingM);
  for (let y = yMin + spacing / 2; y < yMax; y += spacing) {
    const xs: number[] = [];
    for (let i = 0; i < n; i++) {
      const [x1, y1] = pts[i];
      const [x2, y2] = pts[(i + 1) % n];
      if ((y1 <= y && y < y2) || (y2 <= y && y < y1)) {
        const t = (y - y1) / (y2 - y1);
        xs.push(x1 + t * (x2 - x1));
      }
    }
    xs.sort((a, b) => a - b);
    for (let j = 0; j + 1 < xs.length; j += 2) {
      let a = xs[j];
      let b = xs[j + 1];
      if (b - a < 1e-3) continue;
      if (flip) [a, b] = [b, a];
      for (const px of [a, b]) {
        const lat = lat0 + ((y / R) * R2D);
        const lon = lon0 + ((px / (R * cosLat)) * R2D);
        out.push([lat, lon]);
      }
    }
    flip = !flip;
  }
  return out;
}

/**
 * Point-in-polygon test (ray casting) for [lat, lon] tuples. Used to hit-test a
 * click against a survey region's oriented rectangle so the operator can click a
 * region on the map to select it for editing. `poly` is a list of [lat, lon]
 * vertices (open or closed — the wrap is handled here).
 */
export function pointInPolygon(
  lat: number,
  lon: number,
  poly: [number, number][],
): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [yi, xi] = poly[i]; // [lat, lon]
    const [yj, xj] = poly[j];
    const intersect =
      yi > lat !== yj > lat &&
      lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi;
    if (intersect) inside = !inside;
  }
  return inside;
}

// ── Region resize-by-drag geometry ──────────────────────────────────────────
// The region is an oriented rectangle: `widthM` along its local +x (east before
// rotation), `heightM` along its local +y (north before rotation), rotated
// CLOCKWISE by `headingDeg` (matching `orientedRectCorners`). Dragging a corner
// or edge handle on the map changes width/height (and recenters) LIVE.

/** Project a [lat, lon] into the region's LOCAL un-rotated (x=width, y=height)
 *  metre frame, centered on the region center. Inverse of `orientedRectCorners`'
 *  per-corner mapping. */
export function latLonToRegionLocal(
  lat: number,
  lon: number,
  centerLat: number,
  centerLon: number,
  headingDeg: number,
): [number, number] {
  // World offset (east, north) of the point from the region center.
  const east = (lon - centerLon) * D2R * R * Math.cos(centerLat * D2R);
  const north = (lat - centerLat) * D2R * R;
  // orientedRectCorners maps local (x,y) → world via a CLOCKWISE rotation:
  //   east  =  x*cos + y*sin,  north = -x*sin + y*cos.
  // Invert it (rotate the world offset COUNTER-clockwise by heading):
  const h = headingDeg * D2R;
  const cos = Math.cos(h);
  const sin = Math.sin(h);
  const x = east * cos - north * sin;
  const y = east * sin + north * cos;
  return [x, y];
}

/** Map a region-local (x=width axis, y=height axis) offset back to [lat, lon],
 *  applying the region rotation. Inverse of `latLonToRegionLocal`. */
export function regionLocalToLatLon(
  x: number,
  y: number,
  centerLat: number,
  centerLon: number,
  headingDeg: number,
): [number, number] {
  const h = headingDeg * D2R;
  const cos = Math.cos(h);
  const sin = Math.sin(h);
  const east = x * cos + y * sin;
  const north = -x * sin + y * cos;
  return offsetLatLon(centerLat, centerLon, north, east);
}

// A resize handle: 4 corners (cx,cy ∈ {-1,+1}) and 4 edge midpoints
// (one axis is 0). `cx`/`cy` are the local sign along the width/height axes.
export interface RegionHandle {
  id: string;
  cx: -1 | 0 | 1; // sign along the width (x) axis
  cy: -1 | 0 | 1; // sign along the height (y) axis
}

export const REGION_HANDLES: RegionHandle[] = [
  { id: "bl", cx: -1, cy: -1 },
  { id: "br", cx: 1, cy: -1 },
  { id: "tr", cx: 1, cy: 1 },
  { id: "tl", cx: -1, cy: 1 },
  { id: "b", cx: 0, cy: -1 },
  { id: "r", cx: 1, cy: 0 },
  { id: "t", cx: 0, cy: 1 },
  { id: "l", cx: -1, cy: 0 },
];

/** World [lat, lon] position of a resize handle on the region's rectangle. */
export function regionHandlePos(
  h: RegionHandle,
  centerLat: number,
  centerLon: number,
  widthM: number,
  heightM: number,
  headingDeg: number,
): [number, number] {
  const x = (h.cx * Math.max(0, widthM)) / 2;
  const y = (h.cy * Math.max(0, heightM)) / 2;
  return regionLocalToLatLon(x, y, centerLat, centerLon, headingDeg);
}

/**
 * Given a handle drag to a new [lat, lon], recompute the region's
 * {center, width_m, height_m} so the OPPOSITE edge/corner stays put (the dragged
 * handle follows the cursor). Edge handles only move their one axis; corners move
 * both. Rotation is preserved. Dimensions are clamped to a small minimum.
 */
export function resizeRegionByHandle(
  h: RegionHandle,
  dragLat: number,
  dragLon: number,
  centerLat: number,
  centerLon: number,
  widthM: number,
  heightM: number,
  headingDeg: number,
): { center: [number, number]; width_m: number; height_m: number } {
  const MIN = 1;
  const [lx, ly] = latLonToRegionLocal(
    dragLat,
    dragLon,
    centerLat,
    centerLon,
    headingDeg,
  );
  let newW = widthM;
  let newH = heightM;
  // Anchor (the fixed opposite handle) and new handle position in local coords.
  let ax = (-h.cx * widthM) / 2; // opposite x stays fixed
  let ay = (-h.cy * heightM) / 2;
  let hx = (h.cx * widthM) / 2;
  let hy = (h.cy * heightM) / 2;

  if (h.cx !== 0) {
    hx = lx;
    newW = Math.max(MIN, Math.abs(hx - ax));
  } else {
    // Edge handle with no x component: x is unconstrained; keep current span.
    ax = -widthM / 2;
    hx = widthM / 2;
  }
  if (h.cy !== 0) {
    hy = ly;
    newH = Math.max(MIN, Math.abs(hy - ay));
  } else {
    ay = -heightM / 2;
    hy = heightM / 2;
  }

  // New center = midpoint between the fixed anchor and the dragged handle,
  // mapped back to world coordinates through the (unchanged) rotation.
  const mx = (ax + hx) / 2;
  const my = (ay + hy) / 2;
  const center = regionLocalToLatLon(mx, my, centerLat, centerLon, headingDeg);
  return { center, width_m: newW, height_m: newH };
}

// ── Detected-object map icons ────────────────────────────────────────────────
// Per-class SVG glyph paths (roughly centered on the origin, ~24px box) for the
// ground-localized detected objects (car/person/bike/truck/...). Used as a
// google.maps.Symbol `path` on the 2D map and as the pin glyph hint in 3D.
// Bicycle + motorcycle share the "bike" glyph; bus reuses the truck glyph.
export const MAP_OBJECT_GLYPHS: Record<string, string> = {
  // Car: rounded body with a cabin notch.
  car: "M -9,2 L -9,-1 L -6,-1 L -4,-5 L 4,-5 L 6,-1 L 9,-1 L 9,2 L 7,2 A 2,2 0 0 1 3,2 L -3,2 A 2,2 0 0 1 -7,2 Z",
  // Person: head + torso.
  person: "M 0,-8 A 2.4,2.4 0 1 1 0,-3.2 A 2.4,2.4 0 1 1 0,-8 Z M -3.4,8 L -3.4,0 A 3.4,3.4 0 0 1 3.4,0 L 3.4,8 Z",
  // Bike: two wheels + a frame bar.
  bike: "M -6,3 A 3.2,3.2 0 1 1 -6,3.01 Z M 6,3 A 3.2,3.2 0 1 1 6,3.01 Z M -6,3 L -1,-4 L 4,-4 M -1,-4 L 4,3",
  // Truck/Bus: a longer boxy body with a cab step.
  truck: "M -10,3 L -10,-4 L 2,-4 L 2,-1 L 6,-1 L 9,2 L 9,3 Z",
};

/** SVG glyph path for a detected-object class (falls back to the car glyph). */
export function mapObjectGlyph(label: string): string {
  switch (label) {
    case "person":
      return MAP_OBJECT_GLYPHS.person;
    case "bicycle":
    case "motorcycle":
      return MAP_OBJECT_GLYPHS.bike;
    case "truck":
    case "bus":
      return MAP_OBJECT_GLYPHS.truck;
    default:
      return MAP_OBJECT_GLYPHS.car;
  }
}

// Per-class fill colors for detected-object icons (non-tracked). The TRACKED
// object overrides these with the accent/red highlight at the render site.
export const MAP_OBJECT_COLORS: Record<string, string> = {
  car: "#7c9cff",
  truck: "#7c9cff",
  bus: "#7c9cff",
  person: "#ff8a3d",
  bicycle: "#9be870",
  motorcycle: "#9be870",
};

/** Fill color for a detected-object class (falls back to a neutral blue). */
export function mapObjectColor(label: string): string {
  return MAP_OBJECT_COLORS[label] ?? "#7c9cff";
}

/** Compass bearing (deg, 0=N, clockwise) from point a → b. */
export function bearingDeg(
  aLat: number,
  aLon: number,
  bLat: number,
  bLon: number,
): number {
  const φ1 = aLat * D2R;
  const φ2 = bLat * D2R;
  const dλ = (bLon - aLon) * D2R;
  const y = Math.sin(dλ) * Math.cos(φ2);
  const x =
    Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(dλ);
  return (Math.atan2(y, x) * R2D + 360) % 360;
}

/**
 * Interpolate a drone pose along a recorded flight `path` at playback time `t`
 * (seconds since the flight start). `times` is the per-sample unix-second
 * timeline aligned 1:1 with `path`; `startTs` is the flight's first timestamp.
 * Returns the interpolated [lat, lon, alt] plus the heading toward the next
 * sample, or null if the path is empty.
 */
export function poseAtTime(
  path: [number, number, number][],
  times: number[],
  startTs: number,
  t: number,
): { lat: number; lon: number; alt: number; heading: number; index: number } | null {
  if (path.length === 0) return null;
  if (path.length === 1) {
    const [lat, lon, alt] = path[0];
    return { lat, lon, alt: alt ?? 0, heading: 0, index: 0 };
  }
  const absT = startTs + t;
  // Find the segment [i, i+1] containing absT.
  let i = 0;
  while (i < times.length - 1 && times[i + 1] < absT) i++;
  const j = Math.min(i + 1, path.length - 1);
  const t0 = times[i];
  const t1 = times[j];
  const frac = t1 > t0 ? Math.max(0, Math.min(1, (absT - t0) / (t1 - t0))) : 0;
  const [lat0, lon0, alt0] = path[i];
  const [lat1, lon1, alt1] = path[j];
  const lat = lat0 + (lat1 - lat0) * frac;
  const lon = lon0 + (lon1 - lon0) * frac;
  const alt = (alt0 ?? 0) + ((alt1 ?? 0) - (alt0 ?? 0)) * frac;
  const heading =
    lat0 !== lat1 || lon0 !== lon1 ? bearingDeg(lat0, lon0, lat1, lon1) : 0;
  return { lat, lon, alt, heading, index: i };
}
