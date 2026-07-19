import { useEffect, useRef, useState } from "react";
import { useMap } from "@vis.gl/react-google-maps";
import { motion } from "framer-motion";
import { ScanSearch, Loader2, X, Route, Check } from "lucide-react";
import { useGcs } from "../store/useGcs";
import { api } from "../lib/api";

const KEY = import.meta.env.VITE_GOOGLE_MAPS_API_KEY as string | undefined;

// Distinct outline colors for up to 6 candidates.
const PALETTE = ["#22e3c4", "#ffb020", "#7c9cff", "#ff6fae", "#9be870", "#ff8a3d"];

/** Geographic bounds of a square Static Maps tile centered at (lat, lon) for an
 *  integer `zoom` and logical `size` (px per side). MUST match the backend's
 *  `_static_maps_bounds` (Web-Mercator pixel grid) exactly — the backend maps
 *  image pixels LINEARLY across whatever bounds we send, so the bounds have to
 *  describe the SAME ground the fetched tile shows, not the whole map viewport.
 *  (Sending map.getBounds() — the full viewport — stretched every polygon ~3x
 *  and mislocated it by hundreds of metres: the wrong-footprint bug.) */
function staticTileBounds(lat: number, lon: number, zoom: number, size: number) {
  const world = 256 * 2 ** zoom;
  const lonlatToPx = (la: number, lo: number) => {
    const x = ((lo + 180) / 360) * world;
    let s = Math.sin((la * Math.PI) / 180);
    s = Math.min(Math.max(s, -0.9999), 0.9999);
    const y = (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * world;
    return { x, y };
  };
  const pxToLonlat = (x: number, y: number) => {
    const lo = (x / world) * 360 - 180;
    const n = Math.PI - (2 * Math.PI * y) / world;
    const la = (Math.atan(Math.sinh(n)) * 180) / Math.PI;
    return { la, lo };
  };
  const c = lonlatToPx(lat, lon);
  const half = size / 2;
  const nw = pxToLonlat(c.x - half, c.y - half);
  const se = pxToLonlat(c.x + half, c.y + half);
  return { north: nw.la, south: se.la, east: se.lo, west: nw.lo };
}

/** Fetch a satellite Static Maps image centered on the current map view and
 *  return it as base64 (no data: prefix) plus the geographic bounds OF THE
 *  FETCHED TILE (not the viewport). The frontend owns the (referrer-restricted)
 *  maps key, so it must do this fetch — the backend cannot. */
async function captureView(map: google.maps.Map): Promise<{
  image_b64: string;
  bounds: { north: number; south: number; east: number; west: number };
} | null> {
  if (!KEY) return null;
  const center = map.getCenter();
  const z = map.getZoom();
  if (!center || z == null) return null;
  // Static Maps only renders integer zoom; round so the fetched tile's bounds
  // (computed below) match the actual image extent.
  const zoom = Math.round(z);
  const size = 640; // Static Maps free-tier max square (scale=2 → 1280px image, same ground)

  const url =
    `https://maps.googleapis.com/maps/api/staticmap?center=${center.lat()},${center.lng()}` +
    `&zoom=${zoom}&size=${size}x${size}&scale=2&maptype=satellite&key=${KEY}`;

  const res = await fetch(url);
  if (!res.ok) throw new Error(`Static Maps ${res.status} (enable "Maps Static API")`);
  const blob = await res.blob();
  const image_b64 = await new Promise<string>((resolve, reject) => {
    const fr = new FileReader();
    fr.onloadend = () => resolve(String(fr.result).split(",", 2)[1] ?? "");
    fr.onerror = () => reject(new Error("could not read image"));
    fr.readAsDataURL(blob);
  });

  // Bounds of the ACTUAL tile (center+zoom+logical size), so the backend's
  // pixel→lat/lon mapping lands the polygons on the real parcels.
  return { image_b64, bounds: staticTileBounds(center.lat(), center.lng(), zoom, size) };
}

/** Survey-mode helper: detect candidate perimeters from satellite imagery (here
 *  via the button, or pushed in by a voice "survey this area" command), draw
 *  them on the map, let the operator pick one and survey it. Candidates + the
 *  selection live in the store so the voice path and on-screen picking share one
 *  view. Renders inside the <Map> so it can use the imperative maps API. */
export default function PerimeterPlanner() {
  const map = useMap();
  const uiMode = useGcs((s) => s.uiMode);
  const pushLog = useGcs((s) => s.pushLog);
  const telem = useGcs((s) => s.telem);
  const candidates = useGcs((s) => s.surveyCandidates);
  const selected = useGcs((s) => s.surveyChoice);
  const setCandidates = useGcs((s) => s.setSurveyCandidates);
  const setSelected = useGcs((s) => s.setSurveyChoice);
  const preview = useGcs((s) => s.surveyPreview);
  const setPreview = useGcs((s) => s.setSurveyPreview);

  const [busy, setBusy] = useState(false);
  const [planning, setPlanning] = useState(false);
  const [surveying, setSurveying] = useState(false);

  const shapes = useRef<google.maps.Polygon[]>([]);
  const labels = useRef<google.maps.Marker[]>([]);
  // The previewed lawnmower flight path (teal polyline with direction arrows) —
  // shown after "Create mission" / a voice plan, before the operator confirms.
  const previewLine = useRef<google.maps.Polyline>();
  const fittedKey = useRef<string>("");
  // True while we're programmatically (re)building polygons, so the editable
  // polygon's path listeners don't echo our own writes back into the store. Also
  // set while the operator is dragging vertices, so the redraw effect skips
  // rebuilding the polygon they're actively editing.
  const editingRef = useRef(false);

  const alt = telem.alt_rel && telem.alt_rel > 3 ? Math.round(telem.alt_rel) : 30;

  const clearOverlays = () => {
    shapes.current.forEach((p) => p.setMap(null));
    labels.current.forEach((m) => m.setMap(null));
    shapes.current = [];
    labels.current = [];
  };

  // Reset everything when leaving survey mode or unmounting.
  useEffect(() => {
    if (uiMode !== "survey") {
      clearOverlays();
      setCandidates([]);
      setPreview(null);
    }
    return clearOverlays;
  }, [uiMode]); // eslint-disable-line react-hooks/exhaustive-deps

  // Draw the previewed lawnmower flight path (teal, arrowed) whenever a survey is
  // staged. Cleared (path emptied) when the preview is dropped (confirm/cancel).
  useEffect(() => {
    if (!map) return;
    if (!previewLine.current) {
      previewLine.current = new google.maps.Polyline({
        map,
        geodesic: true,
        strokeColor: "#22e3c4",
        strokeOpacity: 0.95,
        strokeWeight: 2.5,
        zIndex: 70,
        icons: [{
          icon: { path: google.maps.SymbolPath.FORWARD_OPEN_ARROW, scale: 2, strokeColor: "#22e3c4" },
          offset: "20px",
          repeat: "80px",
        }],
      });
    }
    previewLine.current.setPath(
      (preview?.path ?? []).map(([lat, lng]) => ({ lat, lng })),
    );
  }, [map, preview]);

  useEffect(() => () => previewLine.current?.setMap(null), []);

  // Draw / restyle candidate polygons whenever the list or selection changes.
  useEffect(() => {
    if (!map) return;
    // The operator is mid-drag on the selected polygon: this effect fired only
    // because that edit synced back into the store. Don't tear down and rebuild
    // the polygon under their cursor — leave the overlays as-is.
    if (editingRef.current) return;
    clearOverlays();
    candidates.forEach((c, i) => {
      const color = PALETTE[i % PALETTE.length];
      const isSel = selected === i;
      // Vertices are only draggable BEFORE a mission is planned. Once a preview is
      // staged, lock the polygon so it can't drift out of sync with the previewed
      // path — the operator cancels to edit again.
      const canEdit = isSel && !preview;
      const path = c.polygon.map(([lat, lng]) => ({ lat, lng }));
      const poly = new google.maps.Polygon({
        map,
        paths: path,
        strokeColor: color,
        strokeWeight: isSel ? 4 : 2,
        strokeOpacity: 1,
        fillColor: color,
        fillOpacity: isSel ? 0.28 : 0.1,
        clickable: true,
        // Selected polygon is editable (until a plan is staged): Google renders
        // draggable vertex handles plus midpoint handles (drag a midpoint = insert).
        editable: canEdit,
        zIndex: isSel ? 60 : 50,
      });
      poly.addListener("click", () => setSelected(i));
      shapes.current.push(poly);

      // Wire up live editing on the selected polygon only (and only pre-plan).
      if (canEdit) {
        const sync = () => {
          // Guard the store write so the path listeners and the redraw effect
          // both know this change originated here, not from a fresh detect.
          editingRef.current = true;
          const next = poly
            .getPath()
            .getArray()
            .map((p) => [p.lat(), p.lng()] as [number, number]);
          const updated = candidates.map((cand, j) =>
            j === i ? { ...cand, polygon: next } : cand,
          );
          // setSurveyCandidates clears the selection, so restore it immediately.
          setCandidates(updated);
          setSelected(i);
          // Release after React has flushed the (skipped) redraw.
          requestAnimationFrame(() => {
            editingRef.current = false;
          });
        };
        const editPath = poly.getPath();
        editPath.addListener("set_at", sync);
        editPath.addListener("insert_at", sync);
        editPath.addListener("remove_at", sync);

        // Remove a vertex: double-click OR right-click it (never below 3 points).
        // Double-click is the reliable path — right-click is finicky to land on a
        // vertex and can trigger the browser/OS context menu. Removal fires
        // remove_at, which calls sync() above. e.stop() prevents the map's
        // default dblclick-zoom / context behavior.
        const removeVertex = (e: google.maps.PolyMouseEvent) => {
          if (e.vertex == null) return;
          const p = poly.getPath();
          if (p.getLength() <= 3) return;
          if (typeof e.stop === "function") e.stop();
          p.removeAt(e.vertex);
        };
        poly.addListener("dblclick", removeVertex);
        poly.addListener("rightclick", removeVertex);
      }

      // Label centroid.
      const cx = path.reduce((s, p) => s + p.lat, 0) / path.length;
      const cy = path.reduce((s, p) => s + p.lng, 0) / path.length;
      const marker = new google.maps.Marker({
        map,
        position: { lat: cx, lng: cy },
        clickable: true,
        icon: { path: google.maps.SymbolPath.CIRCLE, scale: 0, fillOpacity: 0, strokeOpacity: 0 },
        label: { text: c.label, color, fontSize: "11px", fontWeight: "700" },
      });
      marker.addListener("click", () => setSelected(i));
      labels.current.push(marker);
    });
  }, [map, candidates, selected, preview, setSelected, setCandidates]);

  // When a fresh set of candidates arrives (e.g. pushed in by voice while the
  // map is elsewhere), pan/zoom so they're all visible. Keyed on the candidate
  // set so re-selecting doesn't re-fit.
  useEffect(() => {
    if (!map || candidates.length === 0) return;
    const key = candidates.map((c) => c.label).join("|") + ":" + candidates.length;
    if (key === fittedKey.current) return;
    fittedKey.current = key;
    const b = new google.maps.LatLngBounds();
    candidates.forEach((c) => c.polygon.forEach(([lat, lng]) => b.extend({ lat, lng })));
    if (!b.isEmpty()) map.fitBounds(b, 64);
  }, [map, candidates]);

  if (uiMode !== "survey") return null;

  const detect = async () => {
    if (!map || busy) return;
    setBusy(true);
    setCandidates([]);
    try {
      const cap = await captureView(map);
      if (!cap) throw new Error("map view not ready");
      const resp = await api.surveyPerimeters({ ...cap, max_regions: 4 });
      const cands = (resp.perimeters || []).filter((p) => (p.polygon?.length ?? 0) >= 3);
      setCandidates(cands);
      if (cands.length === 0) pushLog("vision", "no survey perimeters detected in view", 2);
      else pushLog("vision", `detected ${cands.length} candidate perimeter(s)`);
    } catch (e) {
      pushLog("error", `Auto-detect: ${(e as Error).message}`, 3);
    } finally {
      setBusy(false);
    }
  };

  // STEP 1 — Create mission: tidy the (possibly vertex-edited) polygon and PLAN
  // the lawnmower path WITHOUT flying. The cleaned ring + path come back from the
  // backend; we draw the path as a preview and STAGE the mission so a later
  // "Confirm & fly" (or a spoken confirm) flies exactly what's shown.
  const createMission = async () => {
    if (selected == null || planning) return;
    const c = candidates[selected];
    setPlanning(true);
    try {
      const plan = await api.surveyPlan(c.polygon, { altitude: alt, line_spacing_m: 25 });
      // Snap the candidate polygon to the cleaned ring so the picker + the
      // previewed path agree on the boundary. NOTE: setCandidates clears the
      // selection AND any preview, so set those AFTER it (not before).
      setCandidates(candidates.map((cand, j) => (j === selected ? { ...cand, polygon: plan.polygon } : cand)));
      setSelected(selected);
      setPreview({
        label: c.label,
        choice: selected,
        polygon: plan.polygon,
        path: plan.path,
        waypoints: plan.waypoints,
      });
      // Stage it for confirm-then-fly (so voice "confirm" flies this too).
      await api.surveyStage(plan.polygon, { label: c.label, altitude: alt });
      pushLog("mission", `Survey "${c.label}" planned (${plan.waypoints} wp) — confirm to fly`);
    } catch (e) {
      pushLog("error", `Plan survey: ${(e as Error).message}`, 3);
    } finally {
      setPlanning(false);
    }
  };

  // STEP 2 — the single, deliberate confirmation gate for EXECUTING the survey
  // (a big autonomous flight). Only here do we upload + start the mission.
  const confirmFly = async () => {
    if (!preview || surveying) return;
    setSurveying(true);
    try {
      await api.surveyCommit();
      pushLog("cmd", `Survey "${preview.label}" confirmed — uploaded + flying`);
      setPreview(null);
    } catch (e) {
      pushLog("error", `Survey: ${(e as Error).message}`, 3);
    } finally {
      setSurveying(false);
    }
  };

  const cancelPlan = async () => {
    setPreview(null);
    try {
      await api.surveyCancel();
    } catch {
      /* best-effort — clearing the local preview is what matters to the operator */
    }
    pushLog("mission", "Survey plan cancelled");
  };

  return (
    <motion.div
      initial={{ y: -8, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      className="glass absolute right-3 top-3 z-10 w-64 rounded-xl p-3 flex flex-col gap-2"
    >
      <button
        onClick={detect}
        disabled={busy}
        className="flex items-center justify-center gap-2 rounded-md bg-accent/20 text-accent px-3 py-2 text-xs font-semibold disabled:opacity-50"
      >
        {busy ? <Loader2 size={14} className="animate-spin" /> : <ScanSearch size={14} />}
        {busy ? "Detecting…" : "Auto-detect perimeters"}
      </button>

      {candidates.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center justify-between">
            <span className="text-[10px] uppercase tracking-wide text-slate-400">
              Candidates · tap to pick
            </span>
            <button
              className="text-slate-500 hover:text-slate-200"
              title="Clear"
              onClick={() => setCandidates([])}
            >
              <X size={13} />
            </button>
          </div>
          {candidates.map((c, i) => {
            const color = PALETTE[i % PALETTE.length];
            const isSel = selected === i;
            return (
              <button
                key={i}
                onClick={() => setSelected(i)}
                className={`flex items-start gap-2 rounded-md px-2 py-1.5 text-left transition-colors ${
                  isSel ? "bg-edge/70" : "bg-edge/30 hover:bg-edge/50"
                }`}
              >
                <span
                  className="mt-0.5 h-3 w-3 shrink-0 rounded-sm"
                  style={{ background: color, opacity: isSel ? 1 : 0.7 }}
                />
                <span className="min-w-0">
                  <span className="block text-xs font-semibold text-slate-100 truncate">
                    {c.label}
                  </span>
                  <span className="block text-[10px] text-slate-400 leading-tight">
                    {c.description}
                  </span>
                </span>
              </button>
            );
          })}
          {selected != null && !preview && (
            <p className="text-[10px] leading-tight text-slate-400">
              drag vertices to adjust · drag midpoints to add · double-click a vertex to remove
            </p>
          )}

          {/* No plan yet → "Create mission" (plan + preview, no fly). */}
          {!preview && (
            <button
              disabled={selected == null || planning}
              onClick={createMission}
              className="mt-1 flex items-center justify-center gap-1.5 rounded-md bg-accent/20 text-accent px-3 py-2 text-xs font-semibold disabled:opacity-40"
            >
              {planning ? <Loader2 size={14} className="animate-spin" /> : <Route size={14} />}
              Create mission {selected != null ? `· ${candidates[selected].label}` : ""}
            </button>
          )}

          {/* Plan staged → the single, deliberate confirmation gate. */}
          {preview && (
            <div className="mt-1 flex flex-col gap-1.5 rounded-md bg-edge/40 p-2">
              <span className="text-[11px] text-slate-200">
                Planned <span className="font-semibold text-accent">{preview.label}</span> ·{" "}
                {preview.waypoints} waypoints. Preview shown on map.
              </span>
              <div className="flex gap-1.5">
                <button
                  disabled={surveying}
                  onClick={confirmFly}
                  className="flex flex-1 items-center justify-center gap-1.5 rounded-md bg-ok/20 text-ok px-3 py-2 text-xs font-semibold disabled:opacity-40"
                >
                  {surveying ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
                  Confirm &amp; fly
                </button>
                <button
                  disabled={surveying}
                  onClick={cancelPlan}
                  className="flex items-center justify-center gap-1.5 rounded-md bg-edge/60 text-slate-200 px-3 py-2 text-xs font-semibold disabled:opacity-40"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </motion.div>
  );
}
