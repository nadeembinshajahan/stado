// Helpers for building a Google Static Maps satellite image with the flight
// trajectory drawn as a colored polyline overlay, and fetching it as a dataURL
// so it can be embedded in the exported PDF.

const KEY = import.meta.env.VITE_GOOGLE_MAPS_API_KEY as string | undefined;

// Cockpit colors. Named drones get their signature color; otherwise teal.
const NAMED: Record<string, string> = {
  overwatch: "#22e3c4",
  outrider: "#ffb020",
};

/** Color (hex, no alpha) for a flight's trajectory, keyed by vehicle name/id. */
export function trajectoryColor(nameOrId: string | undefined): string {
  const key = (nameOrId || "").toLowerCase();
  return NAMED[key] ?? "#22e3c4";
}

/**
 * Downsample a list of [lat, lon, ...] points to at most `max` points,
 * always keeping the first and last so the trajectory's endpoints are exact.
 */
export function downsamplePath<T extends number[]>(path: T[], max: number): T[] {
  if (path.length <= max) return path;
  const out: T[] = [];
  const step = (path.length - 1) / (max - 1);
  for (let i = 0; i < max; i++) {
    out.push(path[Math.round(i * step)]);
  }
  // Guarantee the true last point is present.
  if (out[out.length - 1] !== path[path.length - 1]) {
    out[out.length - 1] = path[path.length - 1];
  }
  return out;
}

/**
 * Encode a list of [lat, lon] pairs using Google's encoded-polyline algorithm.
 * This keeps the Static Maps URL well under its ~8192-char limit even for long
 * trajectories.
 */
export function encodePolyline(points: [number, number][]): string {
  let lastLat = 0;
  let lastLon = 0;
  let result = "";

  const encodeValue = (value: number): string => {
    let v = value < 0 ? ~(value << 1) : value << 1;
    let chunk = "";
    while (v >= 0x20) {
      chunk += String.fromCharCode((0x20 | (v & 0x1f)) + 63);
      v >>= 5;
    }
    chunk += String.fromCharCode(v + 63);
    return chunk;
  };

  for (const [lat, lon] of points) {
    const latE5 = Math.round(lat * 1e5);
    const lonE5 = Math.round(lon * 1e5);
    result += encodeValue(latE5 - lastLat);
    result += encodeValue(lonE5 - lastLon);
    lastLat = latE5;
    lastLon = lonE5;
  }
  return result;
}

// Logical Static Maps image size (the `scale=2` param just doubles pixel
// density; Web Mercator zoom math uses these LOGICAL dimensions).
const IMG_W = 640;
const IMG_H = 400;
// Google Static Maps tile size in px at zoom 0 (one 256px tile = 360° of lon).
const TILE = 256;
// Cap the zoom so a near-stationary hover (tiny bbox) doesn't zoom absurdly far
// in and pixelate the satellite frame. Static Maps tops out around 21.
const MAX_ZOOM = 19;
const MIN_ZOOM = 1;
// We want the trajectory's bounding box to occupy AT LEAST this fraction of the
// image in both dimensions, so the path is clearly dominant (not a dot/squiggle
// lost in a huge frame). The remainder is padding around the path.
const FILL_FRACTION = 0.5;

/** Web-Mercator Y in [0,1] (0 = north pole-ish, 1 = south), for a latitude. */
function mercatorY(latDeg: number): number {
  const lat = Math.max(-85.05112878, Math.min(85.05112878, latDeg));
  const s = Math.sin((lat * Math.PI) / 180);
  return 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
}

/**
 * Choose a Static Maps center + zoom so the path's bounding box fills at least
 * ~FILL_FRACTION of the image in BOTH dimensions, with padding around it.
 *
 * Web Mercator: at zoom z the whole world spans TILE*2^z px. A lon span of
 * `dLon` degrees → (dLon/360)*TILE*2^z px; a lat span maps through mercatorY.
 * We pick the LARGEST z at which the bbox still fits inside FILL_FRACTION of the
 * image (so it fills ~that fraction), capped at MAX_ZOOM. Degenerate (single
 * point / tiny path) → falls back to MAX_ZOOM centered on the point.
 */
export function chooseView(
  coords: [number, number, number][],
): { lat: number; lon: number; zoom: number } {
  let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
  for (const p of coords) {
    minLat = Math.min(minLat, p[0]);
    maxLat = Math.max(maxLat, p[0]);
    minLon = Math.min(minLon, p[1]);
    maxLon = Math.max(maxLon, p[1]);
  }
  const lat = (minLat + maxLat) / 2;
  const lon = (minLon + maxLon) / 2;

  // Fractional world-px span of the bbox at zoom 0 (before *2^z).
  const lonFrac = Math.abs(maxLon - minLon) / 360; // 0..1 of world width
  const yFrac = Math.abs(mercatorY(maxLat) - mercatorY(minLat)); // 0..1 of world height

  // The exact (fractional) zoom z* at which the bbox spans exactly FILL_FRACTION
  // of the image, per dimension. The world at zoom 0 is TILE px in both dims:
  //   lonFrac * TILE * 2^z = FILL_FRACTION * IMG_W   (and similarly for lat)
  //   => 2^z = (FILL*IMG_W)/(lonFrac*TILE)  => z = log2(...)
  // The BINDING dimension is the smaller z* (the one that fills the image first).
  const zoomForLon =
    lonFrac > 0
      ? Math.log2((FILL_FRACTION * IMG_W) / (lonFrac * TILE))
      : Infinity;
  const zoomForLat =
    yFrac > 0
      ? Math.log2((FILL_FRACTION * IMG_H) / (yFrac * TILE))
      : Infinity;

  // Round UP to the next integer zoom. At z* the path spans 50%; the integer
  // tile zoom must still keep it on-frame, and ceil(z*) makes the binding
  // dimension span between 50% and 100% of the image — so the path stays the
  // dominant feature without clipping. (floor would leave it as small as 25%.)
  let zoom = Math.ceil(Math.min(zoomForLon, zoomForLat));
  if (!Number.isFinite(zoom)) zoom = MAX_ZOOM; // degenerate: single point / no span
  zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, zoom));
  return { lat, lon, zoom };
}

/**
 * Build a Google Static Maps URL showing the flight `path` as a colored
 * polyline over satellite imagery. center & zoom are computed from the path's
 * bounding box so the trajectory is the dominant feature (fills ≥~50% of the
 * frame). Returns null when there is no usable path or no API key.
 */
export function buildTrajectoryUrl(
  path: [number, number, number][],
  color: string,
): string | null {
  if (!KEY) return null;
  // Need at least two distinct points to draw a line.
  const coords = (path || []).filter(
    (p) => Array.isArray(p) && p.length >= 2 && p[0] != null && p[1] != null,
  ) as [number, number, number][];
  if (coords.length < 2) return null;

  const { lat, lon, zoom } = chooseView(coords);

  // Downsample to keep the encoded polyline (and URL) small & valid.
  const sampled = downsamplePath(coords, 90);
  const latLon = sampled.map((p) => [p[0], p[1]] as [number, number]);
  const enc = encodePolyline(latLon);

  // color:0xRRGGBBAA — strip the leading '#', force full alpha.
  const hex = color.replace("#", "");
  const pathParam = `color:0x${hex}ff|weight:4|enc:${enc}`;

  const params = new URLSearchParams({
    maptype: "satellite",
    size: `${IMG_W}x${IMG_H}`,
    scale: "2",
    format: "png",
    center: `${lat.toFixed(6)},${lon.toFixed(6)}`,
    zoom: String(zoom),
    key: KEY,
  });
  // Append path manually so the 'enc:' colons/pipes aren't double-escaped in a
  // way Static Maps rejects (it accepts standard percent-encoding fine).
  return `https://maps.googleapis.com/maps/api/staticmap?${params.toString()}&path=${encodeURIComponent(
    pathParam,
  )}`;
}

/** Fetch an image URL and convert it to a PNG dataURL (for jsPDF embedding). */
export async function fetchImageDataUrl(
  url: string,
): Promise<{ dataUrl: string; w: number; h: number }> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`map fetch failed: ${res.status}`);
  const blob = await res.blob();
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const r = new FileReader();
    r.onloadend = () => resolve(r.result as string);
    r.onerror = reject;
    r.readAsDataURL(blob);
  });
  const dims = await new Promise<{ w: number; h: number }>((resolve) => {
    const img = new Image();
    img.onload = () => resolve({ w: img.naturalWidth, h: img.naturalHeight });
    img.onerror = () => resolve({ w: 0, h: 0 });
    img.src = dataUrl;
  });
  return { dataUrl, w: dims.w, h: dims.h };
}
