"""Auto-detect candidate survey perimeters from satellite imagery.

The operator gives us a top-down satellite image of an area plus its geographic
bounds (the lat/lon of the image's N/S/E/W edges). We ask the Qwen vision
model to find distinct land PARCELS that are enclosed by a clear, visible
physical demarcation (boundary wall, fence, field bund, hedgerow/tree line, road,
drainage line, or sharp color/texture edge) and to trace that demarcation as a
many-vertex polygon in normalized image pixel coordinates. We then project those
points back to lat/lon using the supplied bounds. We deliberately do NOT
categorize terrain types — the polygon must follow the real boundary the operator
can see, not a loose box around a "field" or "block". The frontend draws the
candidates and the operator picks one to hand off to the existing
`POST /api/survey` planner/uploader.

Pixel -> lat/lon projection
---------------------------
We use simple LINEAR interpolation between the image edges:

    lon = west  + (x / 1000) * (east  - west)
    lat = north - (y / 1000) * (north - south)

(x to the right, y downward, both normalized 0-1000 as the model returns them.)

This assumes the image is an axis-aligned, north-up tile in a roughly
equirectangular projection. For a single Google Static Maps satellite tile at a
city-block scale that is accurate to a few metres, which is well within survey
line spacing. It is NOT correct for very large extents or rotated/tilted views.

Image input
-----------
Two ways to supply the image (pick whichever the caller has):

1. `image_b64` + explicit `bounds` {north, south, east, west}.
   This is the primary path: the frontend already has the referrer-restricted
   Maps JS key, so it fetches the Static Maps image itself and POSTs the bytes.

2. `lat` + `lon` (+ optional `zoom`, `size`). The backend fetches the Static
   Maps image server-side (only possible if a server-side maps key is set via
   STATIC_MAPS_API_KEY) and derives the bounds
   from the Web-Mercator tile math. The shipped frontend key is referrer
   restricted, so this path may 403 from a server; it is a convenience/fallback
   and the endpoint reports a clear error if no usable key is configured.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import os
import re

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import qwen
from .config import settings

log = logging.getLogger("gcs.survey_vision")
router = APIRouter(prefix="/api/survey")

_STATIC_MAPS_URL = "https://maps.googleapis.com/maps/api/staticmap"


# ── request / response models ────────────────────────────────────────────────
class Bounds(BaseModel):
    north: float
    south: float
    east: float
    west: float


class PerimetersReq(BaseModel):
    # Path 1: image bytes + explicit geographic bounds (preferred).
    image_b64: str | None = Field(
        default=None,
        description="base64 satellite image (data: URL prefix allowed). Pair with `bounds`.",
    )
    bounds: Bounds | None = None

    # Path 2: let the backend fetch the Static Maps tile (needs a server maps key).
    lat: float | None = None
    lon: float | None = None
    zoom: int = 18
    size: int = 640  # px (square); Static Maps free tier max is 640

    max_regions: int = 4
    refine: bool = True  # (qwen path) second pass: zoom into each parcel and re-trace
    detector: str = "auto"  # "auto" (DelAny→Qwen fallback) | "delany" | "qwen"


class Perimeter(BaseModel):
    label: str
    description: str
    polygon: list[list[float]]  # [[lat, lon], ...]


class PerimetersResp(BaseModel):
    perimeters: list[Perimeter]
    bounds: Bounds


# ── Web-Mercator helpers (for the server-side fetch path) ─────────────────────
def _static_maps_bounds(lat: float, lon: float, zoom: int, size: int) -> Bounds:
    """Geographic bounds of a square Static Maps tile centered at (lat, lon).

    Static Maps uses the standard Web-Mercator pixel grid: world is
    256 * 2**zoom px wide, lon is linear in x, lat is via the Mercator y.
    """
    world = 256.0 * (2.0**zoom)

    def lonlat_to_px(la: float, lo: float) -> tuple[float, float]:
        x = (lo + 180.0) / 360.0 * world
        s = math.sin(math.radians(la))
        s = min(max(s, -0.9999), 0.9999)
        y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * world
        return x, y

    def px_to_lonlat(x: float, y: float) -> tuple[float, float]:
        lo = x / world * 360.0 - 180.0
        n = math.pi - 2.0 * math.pi * y / world
        la = math.degrees(math.atan(math.sinh(n)))
        return la, lo

    cx, cy = lonlat_to_px(lat, lon)
    half = size / 2.0
    north, west = px_to_lonlat(cx - half, cy - half)
    south, east = px_to_lonlat(cx + half, cy + half)
    return Bounds(north=north, south=south, east=east, west=west)


def _server_maps_key() -> str:
    """A maps key usable for server-side Static Maps fetches.

    Prefer an explicit, unrestricted server key; fall back to the frontend key
    if the operator exported it for the backend too. Referrer-restricted keys
    will 403 here — that's expected; use the image_b64 path instead.
    """
    return os.environ.get("STATIC_MAPS_API_KEY") or os.environ.get(
        "VITE_GOOGLE_MAPS_API_KEY", ""
    )


async def _fetch_static_map(lat: float, lon: float, zoom: int, size: int) -> bytes:
    key = _server_maps_key()
    if not key:
        raise HTTPException(
            400,
            "no server-side maps key (set STATIC_MAPS_API_KEY) — send `image_b64`"
            " + `bounds` from the frontend instead",
        )
    params = {
        "center": f"{lat},{lon}",
        "zoom": str(zoom),
        "size": f"{size}x{size}",
        "scale": "2",  # 2x pixel density (same ground extent) → finer detail for the VLM
        "maptype": "satellite",
        "key": key,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(_STATIC_MAPS_URL, params=params)
    if r.status_code != 200 or not r.content.startswith(b"\x89PNG"):
        raise HTTPException(
            502,
            f"Static Maps fetch failed ({r.status_code}); the maps key is likely"
            " referrer-restricted — send `image_b64` + `bounds` from the frontend",
        )
    return r.content


# ── Qwen vision spatial detection ─────────────────────────────────────────────
_PROMPT = (
    "You are analyzing a top-down satellite/aerial image to find land PARCELS a "
    "drone could survey. Do NOT categorize terrain types (do not return blobs "
    "labeled 'open field', 'residential block', 'undeveloped terrain', etc). "
    "Instead, find distinct land PARCELS/PLOTS that are enclosed by a CLEAR, "
    "VISIBLE PHYSICAL DEMARCATION in the image, and trace that demarcation. "
    "Demarcations include: boundary walls, fences, field bunds/ridges, hedgerows "
    "or tree lines, roads/paths/tracks, drainage lines or canals, and sharp "
    "color/texture edges that together form a CLOSED boundary around the parcel. "
    "For each parcel return a POLYGON that FOLLOWS the demarcation line as closely "
    "as the shape requires — trace the actual boundary, including corners and "
    "bends. Use as many points as needed to hug the real outline (a square plot "
    "may need 4-5 points; an irregular plot may need 12-20). CRITICAL: the polygon "
    "must span the WHOLE parcel from one boundary edge to the opposite edge — its "
    "vertices sit ON the visible demarcation. Do NOT draw a small box in the "
    "MIDDLE of a large parcel, and do NOT return a loose rectangle around a "
    "general area: if a field's real fence/bund/treeline is large, your polygon "
    "must be just as large and trace that fence/bund/treeline. Every polygon edge "
    "must lie ON a visible demarcation, never across open ground inside the parcel. "
    "Prefer larger, clearly-bounded, open/surveyable parcels; it is "
    "fine to skip dense built-up areas where no clear parcel boundary is visible. "
    "Identify {n} or fewer such parcels. Return ONLY a JSON list; each item must be "
    '{{"label": "<the parcel, e.g. \'Walled plot (NW)\', \'Fenced field\', '
    '\'Bounded parcel beside road\'>", "description": "<short phrase naming the '
    'demarcation that defines it, e.g. \'bounded by boundary wall and access '
    'road\'>", "polygon": [[x, y], ...]}} where x,y are PIXEL coordinates '
    "normalized to 0-1000 (x left->right, y top->bottom), ordered around the "
    "boundary. Make the parcels non-overlapping. Return [] if no clearly "
    "demarcated parcel is visible."
)


def _strip_fences(text: str) -> str:
    """Drop ```json ... ``` fences and grab the outermost JSON array."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    m = re.search(r"\[.*\]", text, re.S)
    return m.group(0) if m else text


def _coerce_point(pt: object) -> tuple[float, float] | None:
    """Accept [x, y], {x, y}, or {"x":..,"y":..}; return raw (x, y).

    The numeric SCALE is not normalized here — the model may emit 0-1000, 0-1, or
    even 0-100. `_polygon_to_latlon` infers the scale from the whole polygon.
    """
    if isinstance(pt, dict):
        x = pt.get("x")
        y = pt.get("y")
        if x is None or y is None:
            return None
        try:
            return float(x), float(y)
        except (TypeError, ValueError):
            return None
    if isinstance(pt, (list, tuple)) and len(pt) >= 2:
        try:
            return float(pt[0]), float(pt[1])
        except (TypeError, ValueError):
            return None
    return None


def _parse_regions(text: str, max_regions: int) -> list[dict]:
    """Robustly parse the model's reply into [{label, description, polygon(px)}]."""
    raw = _strip_fences(text)
    try:
        arr = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("survey_vision: could not parse model JSON: %r", text[:200])
        return []
    if isinstance(arr, dict):
        # Some replies wrap the list, e.g. {"regions": [...]}.
        for v in arr.values():
            if isinstance(v, list):
                arr = v
                break
        else:
            arr = [arr]
    if not isinstance(arr, list):
        return []

    out: list[dict] = []
    for i, item in enumerate(arr):
        if not isinstance(item, dict):
            continue
        poly_raw = item.get("polygon") or item.get("points") or item.get("box_2d")
        if not isinstance(poly_raw, list):
            continue
        pts: list[tuple[float, float]] = []
        for p in poly_raw:
            cp = _coerce_point(p)
            if cp is not None:
                pts.append(cp)
        if len(pts) < 3:
            continue
        label = str(item.get("label") or item.get("name") or f"region {i + 1}")
        desc = str(item.get("description") or item.get("desc") or label)
        out.append({"label": label, "description": desc, "polygon_px": pts})
        if len(out) >= max_regions:
            break
    return out


def _infer_scale(pts: list[tuple[float, float]]) -> float:
    """Infer the coordinate full-scale the model used for this polygon.

    We ask for 0-1000, but models sometimes emit normalized 0-1 or 0-100 (or
    pixel coords up to the requested image size). We pick the smallest standard
    full-scale that comfortably contains every coordinate so the linear mapping
    spans the image rather than collapsing into a corner.
    """
    hi = 0.0
    for x, y in pts:
        hi = max(hi, abs(x), abs(y))
    if hi <= 1.5:
        return 1.0
    if hi <= 100.0:
        return 100.0
    if hi <= 1000.0:
        return 1000.0
    # Larger than 1000 -> treat the observed max as full-scale (e.g. raw pixels).
    return hi


def _mercator_y(lat: float) -> float:
    """Web-Mercator normalized y (0 at lat=85.05, 1 at lat=-85.05). Latitude is
    NON-LINEAR in image pixels for a Mercator tile, so we must interpolate in
    this y-space, not in raw degrees."""
    s = math.sin(math.radians(lat))
    s = min(max(s, -0.9999), 0.9999)
    return 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


def _inv_mercator_y(y: float) -> float:
    """Inverse of `_mercator_y`: normalized Mercator y → latitude (deg)."""
    n = math.pi - 2.0 * math.pi * y
    return math.degrees(math.atan(math.sinh(n)))


def _polygon_to_latlon(
    pts: list[tuple[float, float]], b: Bounds
) -> list[list[float]]:
    """Project a whole polygon of normalized points to [[lat, lon], ...].

    Handles arbitrary-length point lists and auto-detects the coordinate scale
    (0-1, 0-100, 0-1000, or raw pixels) from the polygon as a whole.

    Longitude is linear in image-x. LATITUDE is interpolated in Web-Mercator
    y-space (NOT linearly in degrees), because a Google satellite tile is a
    Mercator projection where equal pixel steps are NOT equal latitude steps.
    Linear-in-degrees was sub-cm at a single city tile but grows with extent and
    latitude; doing it right is free and removes a whole class of N–S drift.
    """
    scale = _infer_scale(pts)
    y_north = _mercator_y(b.north)
    y_south = _mercator_y(b.south)
    out: list[list[float]] = []
    for x, y in pts:
        nx = min(max(x / scale, 0.0), 1.0)
        ny = min(max(y / scale, 0.0), 1.0)
        lon = b.west + nx * (b.east - b.west)
        lat = _inv_mercator_y(y_north + ny * (y_south - y_north))
        out.append([lat, lon])
    return out


async def _detect_regions(image: bytes, mime: str, max_regions: int) -> list[dict]:
    if not settings.dashscope_api_key:
        raise HTTPException(400, "DASHSCOPE_API_KEY not set; cannot detect perimeters")
    prompt = _PROMPT.format(n=max_regions)
    text = await qwen.vision_chat(image, prompt, mime=mime)
    if text is None:
        raise HTTPException(502, "vision detection failed (model call unsuccessful)")
    return _parse_regions(text, max_regions)


# ── stage 2: per-parcel boundary refinement ───────────────────────────────────
_REFINE_PROMPT = (
    "This is a zoomed-in satellite crop centered on ONE land parcel. Identify the "
    "single main parcel that dominates this crop and return its boundary as an "
    "ordered list of CORNER points — the vertices where the boundary changes "
    "direction. The boundary is the visible demarcation: boundary wall, fence, "
    "field bund/ridge, hedgerow or tree line, road/track, drainage line, or a sharp "
    "color/texture edge. Put a corner exactly where two boundary segments meet; the "
    "polygon edges between corners are STRAIGHT lines that must lie ON the visible "
    "demarcation. Most plots/fields have 4-8 corners — do NOT round the corners or "
    "sprinkle extra points along a straight edge, and do NOT exceed ~10 points. "
    'Return ONLY JSON: {"polygon": [[x, y], ...]} where x,y are pixel coordinates '
    "normalized 0-1000 for THIS crop (x left->right, y top->bottom), ordered around "
    "the boundary. The parcel fills most of the crop, so its corners are NEAR the "
    "crop edges (typically 5-95% of the frame) — do NOT return a tiny box in the "
    "middle, and do NOT just return the full image frame either; snap each corner "
    "to the exact pixel where the real boundary turns."
)


def _parse_one_polygon(text: str) -> list[tuple[float, float]]:
    """Parse a single {"polygon": [...]} (or bare [[x,y],...]) reply into points."""
    raw = _strip_fences(text)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return []
    poly: object = obj
    if isinstance(obj, dict):
        poly = obj.get("polygon") or obj.get("points") or obj.get("box_2d") or []
    elif isinstance(obj, list) and obj and isinstance(obj[0], dict):
        poly = obj[0].get("polygon") or obj[0].get("points") or []
    if not isinstance(poly, list):
        return []
    pts: list[tuple[float, float]] = []
    for p in poly:
        cp = _coerce_point(p)
        if cp is not None:
            pts.append(cp)
    return pts


def _norm_pts(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Normalize raw points (any scale) to 0-1, clamped to the frame."""
    scale = _infer_scale(pts)
    return [(min(max(x / scale, 0.0), 1.0), min(max(y / scale, 0.0), 1.0)) for x, y in pts]


async def _refine_all(image: bytes, regions: list[dict]) -> list[dict]:
    """Zoom into each detected parcel and re-trace its boundary at higher
    effective resolution. Falls back to the stage-1 polygon on any failure, so
    refinement can only sharpen — never lose — a candidate."""
    if not regions:
        return regions
    try:
        import cv2
        import numpy as np
    except Exception:  # noqa: BLE001
        log.warning("survey_vision: refine deps unavailable, skipping", exc_info=True)
        return regions

    bgr = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        return regions
    H, W = bgr.shape[:2]

    async def refine(region: dict) -> dict:
        norm = _norm_pts(region["polygon_px"])
        xs = [p[0] for p in norm]
        ys = [p[1] for p in norm]
        x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
        bw, bh = x1 - x0, y1 - y0
        if bw <= 0 or bh <= 0:
            return region
        m = 0.18  # margin so the boundary isn't clipped at the crop edge
        cx0 = max(0.0, x0 - bw * m)
        cx1 = min(1.0, x1 + bw * m)
        cy0 = max(0.0, y0 - bh * m)
        cy1 = min(1.0, y1 + bh * m)
        px0, px1, py0, py1 = int(cx0 * W), int(cx1 * W), int(cy0 * H), int(cy1 * H)
        if px1 - px0 < 24 or py1 - py0 < 24:
            return region
        crop = bgr[py0:py1, px0:px1]
        longest = max(crop.shape[0], crop.shape[1])
        if longest < 768:  # upscale small crops so the model sees detail
            s = 768.0 / longest
            crop = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
        if not ok:
            return region
        text = await qwen.vision_chat(buf.tobytes(), _REFINE_PROMPT)
        if text is None:
            log.warning("survey_vision: refine call failed for %r", region.get("label"))
            return region
        refined = _parse_one_polygon(text)
        if len(refined) < 4:
            return region
        # crop-normalized 0-1 → full-image 0-1
        rn = _norm_pts(refined)
        full = [(cx0 + cx * (cx1 - cx0), cy0 + cy * (cy1 - cy0)) for cx, cy in rn]
        fxs = [p[0] for p in full]
        fys = [p[1] for p in full]
        # Reject a collapsed result or one that just traced the whole crop frame.
        rw, rh = max(fxs) - min(fxs), max(fys) - min(fys)
        if rw < bw * 0.3 or rh < bh * 0.3:
            return region
        if rw > (cx1 - cx0) * 0.985 and rh > (cy1 - cy0) * 0.985:
            return region
        out = dict(region)
        out["polygon_px"] = [(x * 1000.0, y * 1000.0) for x, y in full]  # store as 0-1000 full-image
        return out

    return await asyncio.gather(*(refine(r) for r in regions))


# ── Delineate-Anything: local SOTA field-boundary delineation ─────────────────
# YOLOv11-seg instance model (Lavreniuk et al. 2025) trained on field boundaries.
# Gives tight, edge-locked parcel polygons that VLM grounding can't — runs locally
# on MPS via ultralytics, no cloud/cost. https://lavreniuk.github.io/Delineate-Anything
_DELANY_URL = (
    "https://huggingface.co/MykolaL/DelineateAnything/resolve/main/"
    "DelineateAnything-S.pt?download=true"
)
_DELANY_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "models", "DelineateAnything-S.pt")
)
_delany_model = None  # (YOLO, device) cached after first load


def _load_delany():
    """Lazily download (first use) + load the Delineate-Anything model on MPS/CPU."""
    global _delany_model
    if _delany_model is not None:
        return _delany_model
    os.makedirs(os.path.dirname(_DELANY_PATH), exist_ok=True)
    if not os.path.exists(_DELANY_PATH):
        log.info("downloading Delineate-Anything weights → %s", _DELANY_PATH)
        with httpx.stream("GET", _DELANY_URL, timeout=180, follow_redirects=True) as r:
            r.raise_for_status()
            with open(_DELANY_PATH, "wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
    from ultralytics import YOLO
    import torch
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    _delany_model = (YOLO(_DELANY_PATH), dev)
    return _delany_model


def _detect_fields_delany(image: bytes, max_regions: int, conf: float = 0.35) -> list[dict]:
    """Delineate field parcels with Delineate-Anything → tight polygons (px 0-1000).
    Returns [] (caller falls back to the VLM) if unavailable or nothing is found."""
    try:
        import cv2
        import numpy as np
        model, dev = _load_delany()
    except Exception:  # noqa: BLE001
        log.warning("Delineate-Anything unavailable, falling back", exc_info=True)
        return []
    bgr = cv2.imdecode(np.frombuffer(image, np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        return []
    h, w = bgr.shape[:2]
    try:
        res = model.predict(bgr, imgsz=1280, conf=conf, iou=0.6, device=dev,
                            retina_masks=True, verbose=False)[0]
    except Exception:  # noqa: BLE001
        log.warning("Delineate-Anything inference failed, falling back", exc_info=True)
        return []
    if res.masks is None:
        return []
    confs = res.boxes.conf.cpu().numpy() if res.boxes is not None else []
    items: list[tuple[float, float, list]] = []
    for k, xy in enumerate(res.masks.xy):
        if len(xy) < 3:
            continue
        cnt = xy.astype(np.int32).reshape(-1, 1, 2)
        area = cv2.contourArea(cnt)
        if area < w * h * 0.004:  # drop specks
            continue
        approx = cv2.approxPolyDP(cnt, 0.008 * cv2.arcLength(cnt, True), True).reshape(-1, 2)
        cf = float(confs[k]) if k < len(confs) else 0.0
        pts = [(float(x) / w * 1000.0, float(y) / h * 1000.0) for x, y in approx]
        items.append((area, cf, pts))
    items.sort(key=lambda t: t[0], reverse=True)  # largest parcels first
    out: list[dict] = []
    for i, (_area, cf, pts) in enumerate(items[:max_regions]):
        out.append({
            "label": f"Field {i + 1}",
            "description": f"auto-delineated field boundary · {cf:.0%} confidence",
            "polygon_px": pts,
        })
    return out


def _decode_image(image_b64: str) -> tuple[bytes, str]:
    """Decode a base64 (optionally data:URL) image; sniff PNG vs JPEG."""
    s = image_b64.strip()
    if s.startswith("data:"):
        s = s.split(",", 1)[-1]
    try:
        data = base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, f"image_b64 is not valid base64: {exc}") from exc
    if not data:
        raise HTTPException(400, "image_b64 decoded to empty bytes")
    mime = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
    return data, mime


# ── endpoint ──────────────────────────────────────────────────────────────────
@router.post("/perimeters", response_model=PerimetersResp)
async def perimeters(req: PerimetersReq) -> PerimetersResp:
    """Detect 2-4 candidate survey perimeters from a satellite image.

    Provide EITHER `image_b64` + `bounds` (preferred — the frontend fetches the
    tile with its maps key) OR `lat` + `lon` (backend fetches the tile, needs a
    server-side maps key).
    """
    max_regions = max(1, min(req.max_regions, 6))

    if req.image_b64:
        if req.bounds is None:
            raise HTTPException(400, "`bounds` is required when sending `image_b64`")
        image, mime = _decode_image(req.image_b64)
        bounds = req.bounds
    elif req.lat is not None and req.lon is not None:
        bounds = _static_maps_bounds(req.lat, req.lon, req.zoom, req.size)
        image = await _fetch_static_map(req.lat, req.lon, req.zoom, req.size)
        mime = "image/png"
    else:
        raise HTTPException(
            400, "provide `image_b64`+`bounds`, or `lat`+`lon` for a server fetch"
        )

    # Primary: Delineate-Anything (tight, edge-locked field polygons, local).
    # Fallback: Qwen vision grounding (+ refine) for non-field / urban areas
    # where the field model finds nothing.
    regions: list[dict] = []
    if req.detector in ("auto", "delany"):
        regions = _detect_fields_delany(image, max_regions)
    if not regions and req.detector in ("auto", "qwen"):
        regions = await _detect_regions(image, mime, max_regions)
        if req.refine:
            regions = await _refine_all(image, regions)
    perimeters_out: list[Perimeter] = []
    for r in regions:
        poly = _polygon_to_latlon(r["polygon_px"], bounds)
        if len(poly) < 3:
            continue
        perimeters_out.append(
            Perimeter(label=r["label"], description=r["description"], polygon=poly)
        )

    return PerimetersResp(perimeters=perimeters_out, bounds=bounds)
