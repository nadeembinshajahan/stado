import { useEffect, useRef, useState } from "react";
import { useMapsLibrary } from "@vis.gl/react-google-maps";
import { motion } from "framer-motion";
import { ScanSearch, Loader2, X, Send, Home, Orbit, Trash2, Route, Check } from "lucide-react";
import { useGcs } from "../store/useGcs";
import { api } from "../lib/api";
import { orientedRectCorners, poseAtTime, zoneColor, mapObjectColor } from "../lib/geo";
import FleetSurveyPanel from "./FleetSurveyPanel";
import PointsPanel from "./PointsPanel";

// Distinct outline colors for up to 6 candidates (mirrors PerimeterPlanner 2D).
const PALETTE = ["#22e3c4", "#ffb020", "#7c9cff", "#ff6fae", "#9be870", "#ff8a3d"];

/** Add an alpha channel to a #rrggbb hex color → "#rrggbbaa". */
function withAlpha(hex: string, alpha: number): string {
  const a = Math.round(Math.min(1, Math.max(0, alpha)) * 255)
    .toString(16)
    .padStart(2, "0");
  return `${hex}${a}`;
}

/**
 * Photorealistic 3D view (Google Map Tiles). Renders the drone as an extruded
 * marker, HOME on the ground, and the flight path as a 3D polyline. Requires
 * the "Map Tiles API" to be enabled on the Maps key.
 */
export default function Map3DView() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const maps3d = useMapsLibrary("maps3d") as any;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const markerLib = useMapsLibrary("marker") as any; // PinElement for styled 3D markers
  const host = useRef<HTMLDivElement>(null);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const map = useRef<any>(null);
  // Per-vehicle 3D elements (marker + heading vector), keyed by vehicle id, so
  // BOTH drones are drawn at once. Created on first sight, updated each telemetry
  // tick, removed when a vehicle disappears from fleetTelem.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const vehicleEls = useRef<Record<string, any>>({});
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const home = useRef<any>(null);
  // Per-vehicle trails — one Polyline3DElement per vehicle id, mirroring the 2D
  // MapView's paths-keyed-by-id pattern so BOTH drones leave a trail in 3D
  // (previously only the active drone's trail was drawn — see fleetTrail effect
  // below).
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const trailEls = useRef<Record<string, any>>({});
  const lastCam = useRef({ lat: 0, lon: 0, t: 0 });
  const framed = useRef(false);
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const surveyEls = useRef<any[]>([]); // Polygon3DElement(s) for survey candidates
  // Saved fleet search-area regions — one Polygon3DElement per region (keyed by
  // region id) — plus per-drone assigned zone polygons.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const regionEls = useRef<Record<string, any>>({});
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const zoneEls = useRef<any[]>([]);
  // Per-drone planned lawnmower survey paths (one Polyline3DElement per zone, in
  // the drone's fleet color) — so EACH drone's grid shows inside its own zone.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const zonePathEls = useRef<any[]>([]);
  // Operator POI markers (one Marker3DElement each).
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const poiEls = useRef<any[]>([]);
  // Ground-localized detected objects — one Marker3DElement per object id, the
  // tracked one highlighted. Keyed so a TTL-expired object's pin is removed.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const objEls = useRef<Record<number, any>>({});
  // Mission replay: PER-DRONE full path polyline, flown-so-far polyline, ghost
  // drone — keyed by flight id so a whole mission (several drones) replays at
  // once, each in its fleet color.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const replayPathEls = useRef<Record<string, any>>({});
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const replayDoneEls = useRef<Record<string, any>>({});
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const replayDroneEls = useRef<Record<string, any>>({});
  const [unavailable, setUnavailable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [planning, setPlanning] = useState(false);
  const [surveying, setSurveying] = useState(false);
  // Previewed lawnmower flight path (Polyline3DElement) for a staged survey.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const previewPathEl = useRef<any>(null);
  // Click-to-act target {lat, lon} from a gmp-click on the 3D map.
  const [pendingTarget, setPendingTarget] = useState<{ lat: number; lon: number } | null>(null);

  const { telem, home: homePos, followVehicle } = useGcs();
  const fleetTelem = useGcs((s) => s.fleetTelem);
  const fleetTrail = useGcs((s) => s.fleetTrail);
  const vehicles = useGcs((s) => s.vehicles);
  const activeVehicle = useGcs((s) => s.activeVehicle);
  const uiMode = useGcs((s) => s.uiMode);
  const pushLog = useGcs((s) => s.pushLog);
  const candidates = useGcs((s) => s.surveyCandidates);
  const selected = useGcs((s) => s.surveyChoice);
  const setCandidates = useGcs((s) => s.setSurveyCandidates);
  const setSelected = useGcs((s) => s.setSurveyChoice);
  const preview = useGcs((s) => s.surveyPreview);
  const setPreview = useGcs((s) => s.setSurveyPreview);
  const fleetZones = useGcs((s) => s.fleetZones);
  const savedRegions = useGcs((s) => s.savedRegions);
  const selectedRegionId = useGcs((s) => s.selectedRegionId);
  const pois = useGcs((s) => s.pois);
  const mapObjects = useGcs((s) => s.mapObjects);
  const pruneMapObjects = useGcs((s) => s.pruneMapObjects);
  const replay = useGcs((s) => s.replay);

  // Build the 3D scene once both libraries are ready (maps3d + marker for PinElement).
  useEffect(() => {
    if (!maps3d || !markerLib || !host.current) return;
    const { Map3DElement, MapMode } = maps3d;
    if (!Map3DElement) {
      setUnavailable(true);
      return;
    }
    // Marker altitude is RELATIVE_TO_GROUND (alt_rel). The CAMERA center, though,
    // is ABSOLUTE (AMSL) — so it must use alt_msl, not alt_rel. Using alt_rel at a
    // high-elevation site (e.g. Bengaluru ~900 m) aimed the camera ~900 m
    // underground, which the tilt projected into a ~300 m horizontal offset.
    const start = {
      lat: telem.lat ?? homePos?.lat ?? 25.35338 /* DEMO: Ajman, UAE */,
      lng: telem.lon ?? homePos?.lng ?? 55.38043,
      altitude: Math.max(telem.alt_rel ?? 0, 1),
    };
    const camCenter = { lat: start.lat, lng: start.lng, altitude: telem.alt_msl ?? telem.alt_rel ?? 0 };
    const m = new Map3DElement({ center: camCenter, range: 550, tilt: 62, heading: 0 });
    if (MapMode) {
      try {
        // SATELLITE = clean imagery with no POI/place markers (3D has no
        // per-feature style array, so mode is the only label control).
        m.mode = MapMode.SATELLITE;
      } catch {
        /* older channel */
      }
    }
    m.style.width = "100%";
    m.style.height = "100%";
    host.current.appendChild(m);
    map.current = m;

    // Per-vehicle drone markers + heading vectors are created/updated/removed in
    // the fleetTelem effect below (BOTH drones are drawn at once). Per-vehicle
    // trails are likewise created lazily in the fleetTrail effect — the
    // single-trail init that used to live here only drew the active drone.

    // Click-to-act: maps3d emits gmp-click whose event.position is a
    // LatLngAltitude. While the fleet "pick center" toggle is on, a click sets
    // the survey region center; otherwise it raises the action panel (Fly here /
    // Set Home / Orbit). We read live store state via getState() so this listener
    // (attached once) never goes stale.
    const onClick = (ev: any) => {
      const pos = ev?.position;
      if (!pos) return;
      const lat = typeof pos.lat === "function" ? pos.lat() : pos.lat;
      const lng = typeof pos.lng === "function" ? pos.lng() : pos.lng;
      if (lat == null || lng == null) return;
      const st = useGcs.getState();
      if (st.fleetPickCenter) {
        if (st.selectedRegionId) {
          // Reposition the selected region's center (3D has no drag handle).
          st.updateRegion(st.selectedRegionId, { center: [lat, lng] });
        } else {
          const n = st.savedRegions.length + 1;
          st.addRegion({
            name: `Sector ${n}`,
            center: [lat, lng],
            width_m: 400,
            height_m: 300,
            heading_deg: 0,
          });
        }
        st.setFleetPickCenter(false);
        return;
      }
      setPendingTarget({ lat, lon: lng });
    };
    m.addEventListener("gmp-click", onClick);

    return () => {
      m.removeEventListener("gmp-click", onClick);
      m.remove();
      map.current = null;
      // The map removal detaches children too; drop our refs so a remount rebuilds.
      vehicleEls.current = {};
      regionEls.current = {};
      zoneEls.current = [];
      zonePathEls.current = [];
      poiEls.current = [];
      objEls.current = {};
      replayPathEls.current = {};
      replayDoneEls.current = {};
      replayDroneEls.current = {};
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [maps3d, markerLib]);

  // Frame the camera on the drone ONCE, as soon as we have a fix — independent of
  // followVehicle (that flag only governs CONTINUOUS following). Without this the
  // 3D camera stays at the scene-build position (stale/fallback) and looks "behind"
  // the drone when follow is off.
  useEffect(() => {
    if (!map.current || framed.current || telem.lat == null || telem.lon == null) return;
    framed.current = true;
    // Camera center altitude is AMSL → use alt_msl (NOT alt_rel) so the look-at
    // sits at the drone, not underground (the ~300 m offset bug).
    const center = { lat: telem.lat, lng: telem.lon, altitude: telem.alt_msl ?? telem.alt_rel ?? 0 };
    lastCam.current = { lat: telem.lat, lon: telem.lon, t: Date.now() }; // suppress an immediate follow re-frame
    const m = map.current;
    if (typeof m.flyCameraTo === "function") {
      m.flyCameraTo({ endCamera: { center, range: 500, tilt: 62 }, durationMillis: 0 });
    } else {
      m.center = center;
    }
  }, [telem.lat, telem.lon, telem.alt_rel]);

  // Recenter the CAMERA on the ACTIVE drone occasionally so live 10 Hz telemetry
  // can't fight the user's pan/zoom. (The per-vehicle markers/heading vectors are
  // moved in the fleetTelem effect below; this effect is camera-only.)
  useEffect(() => {
    if (!map.current || telem.lat == null || telem.lon == null) return;
    if (!followVehicle) return;

    const now = Date.now();
    const lc = lastCam.current;
    const movedM = Math.hypot(
      (telem.lat - lc.lat) * 111320,
      (telem.lon - lc.lon) * 111320 * Math.cos((telem.lat * Math.PI) / 180),
    );
    const first = lc.t === 0;
    // Initial framing immediately; afterwards only when the drone actually moves.
    if (!first && (now - lc.t < 2500 || movedM < 8)) return;
    lastCam.current = { lat: telem.lat, lon: telem.lon, t: now };
    const m = map.current;
    // Camera center altitude is AMSL (alt_msl), not alt_rel — see note above.
    const camAlt = telem.alt_msl ?? telem.alt_rel ?? 0;
    const endCamera = { center: { lat: telem.lat, lng: telem.lon, altitude: camAlt }, range: 600, tilt: 65 };
    if (typeof m.flyCameraTo === "function") {
      m.flyCameraTo({ endCamera, durationMillis: first ? 0 : 1500 });
    } else {
      m.center = endCamera.center;
    }
  }, [telem.lat, telem.lon, telem.alt_rel, telem.alt_msl, telem.heading, followVehicle]);

  // Per-vehicle drone markers — draw EVERY drone in fleetTelem at once. Each gets
  // an extruded, name-labelled Marker3DElement (RELATIVE_TO_GROUND, off its own
  // alt_rel) plus a short heading vector. The ACTIVE vehicle is the prominent teal
  // pin + cyan vector; others are a secondary amber/violet pin + matching vector.
  // New ids are created, existing ones updated in place, and vanished ids removed.
  useEffect(() => {
    if (!map.current || !maps3d) return;
    const { Marker3DElement, Model3DElement, Polyline3DElement, AltitudeMode } = maps3d;
    const PinElement = markerLib?.PinElement;
    if (!Model3DElement && !Marker3DElement) return;

    // Each drone is a real 3D glTF model (cyan = active, amber = others) — NOT a
    // map pin. Falls back to a colored pin only if Model3DElement isn't in this
    // maps3d build. Plus a heading vector and a vertical altitude tether.
    const SECONDARY = ["#ffb020", "#7c9cff", "#ff6fae", "#9be870", "#ff8a3d"];
    const nonActiveIds = Object.keys(fleetTelem).filter((id) => id !== activeVehicle).sort();

    Object.entries(fleetTelem).forEach(([id, t]) => {
      if (t.lat == null || t.lon == null) return;
      const isActive = id === activeVehicle;
      const name = vehicles.find((v) => v.id === id)?.name ?? id;
      const alt = Math.max(t.alt_rel ?? 0, 1);
      const color = isActive ? "#22e3c4" : SECONDARY[nonActiveIds.indexOf(id) % SECONDARY.length];
      const vectorColor = isActive ? "#7dffe8" : color;
      const modelSrc = isActive ? "/models/drone-cyan.glb" : "/models/drone-amber.glb";
      const scale = isActive ? 2.5 : 2; // small, sleek chevron — not a giant blob
      const pos = { lat: t.lat, lng: t.lon, altitude: alt };
      const orientation = { heading: t.heading ?? 0, tilt: 0, roll: 0 };

      let els = vehicleEls.current[id];
      if (!els) {
        let marker;
        const model = !!Model3DElement;
        if (model) {
          marker = new Model3DElement({
            src: modelSrc, position: pos,
            altitudeMode: AltitudeMode.RELATIVE_TO_GROUND, orientation, scale,
          });
        } else {
          marker = new Marker3DElement({
            position: pos, altitudeMode: AltitudeMode.RELATIVE_TO_GROUND, extruded: true, label: name,
          });
          if (PinElement) marker.append(new PinElement({ background: color, borderColor: "#04241e", glyphColor: "#04241e", scale: isActive ? 1.3 : 1.0 }));
        }
        const heading = new Polyline3DElement({ altitudeMode: AltitudeMode.RELATIVE_TO_GROUND, strokeColor: vectorColor, strokeWidth: isActive ? 5 : 4 });
        const tether = new Polyline3DElement({ altitudeMode: AltitudeMode.RELATIVE_TO_GROUND, strokeColor: color, strokeWidth: 2 });
        map.current.append(marker); map.current.append(heading); map.current.append(tether);
        els = { marker, heading, tether, model };
        vehicleEls.current[id] = els;
      }

      els.marker.position = pos;
      if (els.model) {
        els.marker.src = modelSrc;
        els.marker.orientation = orientation;
        els.marker.scale = scale;
      } else {
        els.marker.label = name;
        if (PinElement) els.marker.replaceChildren(new PinElement({ background: color, borderColor: "#04241e", glyphColor: "#04241e", scale: isActive ? 1.3 : 1.0 }));
      }
      els.heading.strokeColor = vectorColor;
      els.heading.strokeWidth = isActive ? 5 : 4;
      // Vertical altitude tether (ground → drone) so height reads at a glance.
      els.tether.strokeColor = color;
      els.tether.path = [
        { lat: t.lat, lng: t.lon, altitude: 0.5 },
        { lat: t.lat, lng: t.lon, altitude: alt },
      ];

      // Heading vector: a ~30 m segment from the drone along its current heading.
      if (t.heading != null) {
        const hd = (t.heading * Math.PI) / 180;
        const R = 6378137, d = 10; // short heading tick (the chevron already shows facing)
        const dLat = ((d * Math.cos(hd)) / R) * (180 / Math.PI);
        const dLon = ((d * Math.sin(hd)) / (R * Math.cos((t.lat * Math.PI) / 180))) * (180 / Math.PI);
        els.heading.path = [
          { lat: t.lat, lng: t.lon, altitude: alt },
          { lat: t.lat + dLat, lng: t.lon + dLon, altitude: alt },
        ];
      }
    });

    // Remove elements for vehicles that vanished (or lost their fix entirely).
    Object.keys(vehicleEls.current).forEach((id) => {
      const t = fleetTelem[id];
      if (t && t.lat != null && t.lon != null) return;
      const els = vehicleEls.current[id];
      try { els.marker.remove(); } catch { /* already detached */ }
      try { els.heading.remove(); } catch { /* already detached */ }
      try { els.tether?.remove(); } catch { /* already detached */ }
      delete vehicleEls.current[id];
    });
  }, [fleetTelem, vehicles, activeVehicle, maps3d, markerLib]);

  // HOME marker (lazy-created once we have a position + the libs) — amber pin
  // clamped to the ground, matching the 2D HOME styling.
  useEffect(() => {
    if (!map.current || !maps3d || !markerLib || !homePos) return;
    const { Marker3DElement, AltitudeMode } = maps3d;
    const { PinElement } = markerLib;
    if (!home.current) {
      home.current = new Marker3DElement({ altitudeMode: AltitudeMode.CLAMP_TO_GROUND, label: "HOME" });
      if (PinElement) {
        home.current.append(new PinElement({
          background: "#ffb020", borderColor: "#1a1205", glyphColor: "#1a1205", scale: 1.0,
        }));
      }
      map.current.append(home.current);
    }
    home.current.position = { ...homePos, altitude: 0 };
  }, [homePos, maps3d, markerLib]);

  // Per-vehicle trajectories — one Polyline3DElement per vehicle id, fed from
  // fleetTrail (mirrors the 2D MapView's paths-keyed-by-id pattern). Active drone
  // = brighter teal, others = amber, both visible at once. Polylines for vanished
  // ids are removed.
  useEffect(() => {
    const m = map.current;
    if (!m || !maps3d) return;
    const { Polyline3DElement, AltitudeMode } = maps3d;
    const ids = Object.keys(fleetTrail);

    // Reap polylines for vehicles that disappeared from fleetTrail.
    for (const id of Object.keys(trailEls.current)) {
      if (!ids.includes(id)) {
        try { trailEls.current[id].remove(); } catch { /* already detached */ }
        delete trailEls.current[id];
      }
    }

    // Create / update one polyline per vehicle. Path needs ≥2 points to render.
    for (const id of ids) {
      const path = fleetTrail[id] ?? [];
      if (path.length < 2) continue;
      const isActive = id === activeVehicle;
      let line = trailEls.current[id];
      if (!line) {
        line = new Polyline3DElement({
          altitudeMode: AltitudeMode.RELATIVE_TO_GROUND,
          strokeColor: isActive ? "#22e3c4" : "#ffb020",
          strokeWidth: isActive ? 7 : 5,
        });
        m.append(line);
        trailEls.current[id] = line;
      } else {
        // Re-style on active-vehicle switch so the brighter trail follows the
        // active drone (the Polyline3DElement's stroke props are live).
        line.strokeColor = isActive ? "#22e3c4" : "#ffb020";
        line.strokeWidth = isActive ? 7 : 5;
      }
      const alt = Math.max(fleetTelem[id]?.alt_rel ?? 30, 1);
      line.path = path.map((p) => ({ lat: p.lat, lng: p.lng, altitude: alt }));
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fleetTrail, fleetTelem, activeVehicle, maps3d]);

  // Survey candidate polygons: rendered as ground-clamped 3D polygons while in
  // survey mode. Recreated whenever the candidate set or selection changes (the
  // maps3d Polygon3DElement isn't reliably restylable in place, and there's no
  // vertex editing in 3D, so a full rebuild is simplest + correct). Clicking a
  // polygon selects it via the interactive variant when present; otherwise the
  // panel list below is the way to pick. Cleared on unmount / leaving survey.
  const clearSurveyEls = () => {
    surveyEls.current.forEach((p) => {
      try {
        p.remove();
      } catch {
        /* already detached */
      }
    });
    surveyEls.current = [];
  };

  useEffect(() => {
    if (!map.current || !maps3d) return;
    const { Polygon3DElement, Polygon3DInteractiveElement, AltitudeMode } = maps3d;
    clearSurveyEls();
    if (uiMode !== "survey" || !Polygon3DElement) return;

    candidates.forEach((c, i) => {
      if (!c.polygon || c.polygon.length < 3) return;
      const color = PALETTE[i % PALETTE.length];
      const isSel = selected === i;
      const outerCoordinates = c.polygon.map(([lat, lng]) => ({ lat, lng, altitude: 0 }));

      // Use the interactive variant if this maps3d build ships it, so gmp-click
      // can drive selection; otherwise fall back to the static polygon (panel
      // list still selects it).
      const Ctor = Polygon3DInteractiveElement || Polygon3DElement;
      const poly = new Ctor({
        altitudeMode: AltitudeMode.CLAMP_TO_GROUND,
        strokeColor: isSel ? "#ffffff" : color,
        strokeWidth: isSel ? 6 : 3,
        fillColor: withAlpha(color, isSel ? 0.32 : 0.12),
        outerCoordinates,
        zIndex: isSel ? 60 : 50,
      });
      if (Polygon3DInteractiveElement) {
        poly.addEventListener("gmp-click", () => setSelected(i));
      }
      map.current.append(poly);
      surveyEls.current.push(poly);
    });

    return clearSurveyEls;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [maps3d, uiMode, candidates, selected]);

  // Previewed lawnmower flight path for a STAGED survey (teal 3D polyline),
  // rebuilt whenever the preview changes; removed when the plan is dropped.
  useEffect(() => {
    if (!map.current || !maps3d) return;
    const { Polyline3DElement, AltitudeMode } = maps3d;
    if (previewPathEl.current) {
      try { previewPathEl.current.remove(); } catch { /* detached */ }
      previewPathEl.current = null;
    }
    if (!Polyline3DElement || !preview || preview.path.length < 2) return;
    const path = preview.path.map(([lat, lng]) => ({ lat, lng, altitude: 0 }));
    const line = new Polyline3DElement({
      altitudeMode: AltitudeMode.CLAMP_TO_GROUND,
      strokeColor: "#22e3c4",
      strokeWidth: 5,
      coordinates: path,
      zIndex: 62,
    });
    map.current.append(line);
    previewPathEl.current = line;
    return () => {
      if (previewPathEl.current) {
        try { previewPathEl.current.remove(); } catch { /* detached */ }
        previewPathEl.current = null;
      }
    };
  }, [maps3d, preview]);

  // Saved search-area regions: one ground-clamped oriented rectangle per region,
  // recomputed from center + width × breadth + rotation whenever ANY region is
  // added/edited/dragged/removed (incl. a center reposition done in 2D). The
  // SELECTED region is bright teal + filled; the others are dim outlines. When
  // this maps3d build ships the interactive polygon, clicking a region selects it
  // (loads it into the panel); otherwise selection is via the panel / 2D view.
  // Rebuilt each change (Polygon3DElement isn't reliably restylable in place).
  useEffect(() => {
    if (!map.current || !maps3d) return;
    const { Polygon3DElement, Polygon3DInteractiveElement, AltitudeMode } = maps3d;
    Object.values(regionEls.current).forEach((p: any) => {
      try { p.remove(); } catch { /* detached */ }
    });
    regionEls.current = {};
    if (!Polygon3DElement) return;
    savedRegions.forEach((r, i) => {
      const id = r.id as string;
      const isSel = id === selectedRegionId;
      const color = isSel ? "#22e3c4" : zoneColor(r.name, i);
      const outerCoordinates = orientedRectCorners(
        r.center[0], r.center[1], r.width_m, r.height_m, r.heading_deg,
      ).map(([la, lo]) => ({ lat: la, lng: lo, altitude: 0 }));
      const Ctor = Polygon3DInteractiveElement || Polygon3DElement;
      const poly = new Ctor({
        altitudeMode: AltitudeMode.CLAMP_TO_GROUND,
        strokeColor: color, strokeWidth: isSel ? 5 : 3,
        fillColor: withAlpha(color, isSel ? 0.14 : 0.06),
        outerCoordinates, zIndex: isSel ? 71 : 68,
      });
      if (Polygon3DInteractiveElement) {
        poly.addEventListener("gmp-click", () => useGcs.getState().selectRegion(id));
      }
      map.current.append(poly);
      regionEls.current[id] = poly;
    });
  }, [savedRegions, selectedRegionId, maps3d]);

  // Per-drone assigned zones: filled + outlined Polygon3DElement in each
  // vehicle's color (overwatch teal / outrider amber, else by index). Rebuilt
  // whenever the assignment set changes.
  useEffect(() => {
    if (!map.current || !maps3d) return;
    const { Polygon3DElement, Polyline3DElement, AltitudeMode } = maps3d;
    zoneEls.current.forEach((p) => { try { p.remove(); } catch { /* detached */ } });
    zonePathEls.current.forEach((p) => { try { p.remove(); } catch { /* detached */ } });
    zoneEls.current = [];
    zonePathEls.current = [];
    if (!Polygon3DElement) return;
    fleetZones.forEach((z, i) => {
      if (!z.polygon || z.polygon.length < 3) return;
      const color = zoneColor(z.name || z.vehicle, i);
      const outerCoordinates = z.polygon.map(([lat, lng]) => ({ lat, lng, altitude: 0 }));
      const poly = new Polygon3DElement({
        altitudeMode: AltitudeMode.CLAMP_TO_GROUND,
        strokeColor: color, strokeWidth: 4,
        fillColor: withAlpha(color, 0.18),
        outerCoordinates, zIndex: 65,
      });
      map.current.append(poly);
      zoneEls.current.push(poly);
      // THIS drone's planned lawnmower path inside its zone, in its fleet color.
      if (Polyline3DElement && z.path && z.path.length >= 2) {
        const line = new Polyline3DElement({
          altitudeMode: AltitudeMode.CLAMP_TO_GROUND,
          strokeColor: withAlpha(color, z.flying ? 0.95 : 0.7),
          strokeWidth: 4,
        });
        line.path = z.path.map(([lat, lng]) => ({ lat, lng, altitude: 0 }));
        map.current.append(line);
        zonePathEls.current.push(line);
      }
    });
  }, [fleetZones, maps3d]);

  // Operator POI markers — violet pins clamped to the ground, labelled with the
  // POI name. Mirrors the 2D MapView POI markers so dropped points show in 3D.
  useEffect(() => {
    if (!map.current || !maps3d) return;
    const { Marker3DElement, AltitudeMode } = maps3d;
    const PinElement = markerLib?.PinElement;
    poiEls.current.forEach((m) => { try { m.remove(); } catch { /* detached */ } });
    poiEls.current = [];
    if (!Marker3DElement) return;
    pois.forEach((p) => {
      const m = new Marker3DElement({
        position: { lat: p.lat, lng: p.lng, altitude: 0 },
        altitudeMode: AltitudeMode.CLAMP_TO_GROUND,
        label: p.name,
      });
      if (PinElement) {
        m.append(new PinElement({ background: "#b07cff", borderColor: "#1a0f2e", glyphColor: "#1a0f2e", scale: 1.0 }));
      }
      map.current.append(m);
      poiEls.current.push(m);
    });
  }, [pois, maps3d, markerLib]);

  // Ground-localized detected objects — one ground-clamped Marker3DElement per
  // object id at its estimated lat/lon. The TRACKED (locked) object is a larger
  // red pin labelled with its class; the rest are smaller per-class-colored pins.
  // Created on first sight, repositioned/restyled each store change, removed when
  // the object's TTL drops it from the store.
  useEffect(() => {
    if (!map.current || !maps3d) return;
    const { Marker3DElement, AltitudeMode } = maps3d;
    const PinElement = markerLib?.PinElement;
    if (!Marker3DElement) return;
    const live = Object.values(mapObjects);
    const ids = new Set(live.map((o) => o.id));
    // Remove pins for objects no longer present.
    for (const idStr of Object.keys(objEls.current)) {
      const id = Number(idStr);
      if (!ids.has(id)) {
        try { objEls.current[id].remove(); } catch { /* detached */ }
        delete objEls.current[id];
      }
    }
    for (const o of live) {
      const color = o.tracked ? "#ff4d5e" : mapObjectColor(o.label);
      const pos = { lat: o.lat, lng: o.lon, altitude: 0 };
      let m = objEls.current[o.id];
      if (!m) {
        m = new Marker3DElement({ position: pos, altitudeMode: AltitudeMode.CLAMP_TO_GROUND });
        map.current.append(m);
        objEls.current[o.id] = m;
      }
      m.position = pos;
      m.label = o.tracked ? o.label.toUpperCase() : "";
      if (PinElement) {
        m.replaceChildren(
          new PinElement({
            background: color,
            borderColor: o.tracked ? "#350a0e" : "#0a1322",
            glyphColor: "#0a1322",
            scale: o.tracked ? 1.3 : 0.8,
          }),
        );
      }
    }
  }, [mapObjects, maps3d, markerLib]);

  // TTL sweeper: prune stale detections on a timer so pins fade out even when no
  // fresh batch arrives (vision stopped / objects left the frame).
  useEffect(() => {
    const t = setInterval(() => pruneMapObjects(), 1000);
    return () => clearInterval(t);
  }, [pruneMapObjects]);

  // Mission replay: draw EACH drone's full recorded path once a replay loads (dim
  // in its fleet color), plus an empty "flown-so-far" polyline + a ghost drone
  // per drone, updated each tick. Keyed by flight id so a whole mission (several
  // drones) draws at once.
  const replayKey = replay ? replay.drones.map((d) => d.flightId).join(",") : null;
  useEffect(() => {
    if (!map.current || !maps3d) return;
    const { Polyline3DElement, Marker3DElement, AltitudeMode } = maps3d;
    const PinElement = markerLib?.PinElement;
    // Tear down all prior replay elements (rebuilt fresh below for the new set).
    [replayPathEls, replayDoneEls, replayDroneEls].forEach((rec) => {
      Object.values(rec.current).forEach((el: any) => {
        try { el.remove(); } catch { /* detached */ }
      });
      rec.current = {};
    });
    if (!replay || !Polyline3DElement) return;
    let framed = false;
    for (const d of replay.drones) {
      if (d.path.length < 2) continue;
      const full = d.path.map(([lat, lng, alt]) => ({ lat, lng, altitude: Math.max(alt ?? 0, 1) }));
      const dim = new Polyline3DElement({
        altitudeMode: AltitudeMode.RELATIVE_TO_GROUND, strokeColor: withAlpha(d.color, 0.5), strokeWidth: 5,
      });
      dim.path = full;
      map.current.append(dim);
      replayPathEls.current[d.flightId] = dim;
      const done = new Polyline3DElement({
        altitudeMode: AltitudeMode.RELATIVE_TO_GROUND, strokeColor: d.color, strokeWidth: 8,
      });
      map.current.append(done);
      replayDoneEls.current[d.flightId] = done;
      if (Marker3DElement) {
        const ghost = new Marker3DElement({
          position: full[0], altitudeMode: AltitudeMode.RELATIVE_TO_GROUND, extruded: true,
          label: d.vehicleName,
        });
        if (PinElement) {
          ghost.append(new PinElement({ background: d.color, borderColor: "#04241e", glyphColor: "#04241e", scale: 1.2 }));
        }
        map.current.append(ghost);
        replayDroneEls.current[d.flightId] = ghost;
      }
      // Frame the camera on the first drawable drone's start.
      if (!framed) {
        const m = map.current;
        if (typeof m.flyCameraTo === "function") {
          m.flyCameraTo({ endCamera: { center: { ...full[0] }, range: 600, tilt: 62 }, durationMillis: 0 });
        }
        framed = true;
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replayKey, maps3d, markerLib]);

  // Replay tick: move each ghost drone + extend each flown-so-far polyline on the
  // SHARED mission clock. A drone outside its own window clamps to its path ends.
  const replayT = replay?.t ?? null;
  useEffect(() => {
    if (!map.current || !maps3d || !replay) return;
    for (const d of replay.drones) {
      if (d.path.length === 0) continue;
      const localT = replay.startTs + replay.t - d.startTs;
      const pose = poseAtTime(d.path, d.times, d.startTs, localT);
      if (!pose) continue;
      const ghost = replayDroneEls.current[d.flightId];
      if (ghost) {
        ghost.position = { lat: pose.lat, lng: pose.lon, altitude: Math.max(pose.alt, 1) };
      }
      const done = replayDoneEls.current[d.flightId];
      if (done) {
        const flown = d.path
          .slice(0, pose.index + 1)
          .map(([lat, lng, alt]) => ({ lat, lng, altitude: Math.max(alt ?? 0, 1) }));
        flown.push({ lat: pose.lat, lng: pose.lon, altitude: Math.max(pose.alt, 1) });
        if (flown.length >= 2) done.path = flown;
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replayT]);

  // Detect perimeters at the drone's current position (no map-view capture in
  // 3D — we ask the backend to fetch its own satellite tile around lat/lon).
  const detect = async () => {
    if (busy) return;
    if (telem.lat == null || telem.lon == null) {
      pushLog("error", "Detect: no GPS fix yet — wait for a position", 2);
      return;
    }
    setBusy(true);
    setCandidates([]);
    try {
      const resp = await api.surveyPerimeters({ lat: telem.lat, lon: telem.lon, zoom: 18 });
      const cands = (resp.perimeters || []).filter((p) => (p.polygon?.length ?? 0) >= 3);
      setCandidates(cands);
      if (cands.length === 0) pushLog("vision", "no survey perimeters detected", 2);
      else pushLog("vision", `detected ${cands.length} candidate perimeter(s)`);
    } catch (e) {
      pushLog("error", `Auto-detect: ${(e as Error).message}`, 3);
    } finally {
      setBusy(false);
    }
  };

  // PLAN + preview (no fly). 3D has no vertex editing, so we plan the candidate
  // polygon as-is; the cleaned ring + lawnmower path come back and draw as a
  // preview, and the mission is staged for a confirm-then-fly.
  const createMission = async () => {
    if (selected == null || planning) return;
    const c = candidates[selected];
    const altitude = Math.round(telem.alt_rel || 30);
    setPlanning(true);
    try {
      const plan = await api.surveyPlan(c.polygon, { altitude, line_spacing_m: 25 });
      setPreview({
        label: c.label,
        choice: selected,
        polygon: plan.polygon,
        path: plan.path,
        waypoints: plan.waypoints,
      });
      await api.surveyStage(plan.polygon, { label: c.label, altitude });
      pushLog("mission", `Survey "${c.label}" planned (${plan.waypoints} wp) — confirm to fly`);
    } catch (e) {
      pushLog("error", `Plan survey: ${(e as Error).message}`, 3);
    } finally {
      setPlanning(false);
    }
  };

  // The single confirmation gate — only here do we upload + start the mission.
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
      /* best-effort */
    }
    pushLog("mission", "Survey plan cancelled");
  };

  // Click-to-act on the 3D map — mirrors the 2D ActionCards. Uses the active
  // drone's alt_rel for the goto/orbit altitude (fallback 30 m).
  const actAlt = telem.alt_rel && telem.alt_rel > 3 ? Math.round(telem.alt_rel) : 30;
  const act = (label: string, fn: () => Promise<unknown>) => async () => {
    try {
      await fn();
      pushLog("cmd", `${label} sent`);
      setPendingTarget(null);
    } catch (e) {
      pushLog("error", `${label}: ${(e as Error).message}`, 3);
    }
  };

  return (
    <div className="absolute inset-0">
      <div ref={host} className="h-full w-full bg-ink" />

      <FleetSurveyPanel />
      <PointsPanel />

      {/* Click-to-act panel — Fly here / Set Home / Orbit on the clicked point. */}
      {pendingTarget && (
        <motion.div
          initial={{ y: 10, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          className="glass absolute left-1/2 top-3 z-10 -translate-x-1/2 rounded-xl px-3 py-2 flex items-center gap-2"
        >
          <span className="tnum text-xs text-slate-300">
            {pendingTarget.lat.toFixed(5)}, {pendingTarget.lon.toFixed(5)}
          </span>
          <button
            className="flex items-center gap-1 rounded-md bg-accent/20 text-accent px-2.5 py-1.5 text-xs font-semibold"
            onClick={act("Fly here", () => api.goto(pendingTarget.lat, pendingTarget.lon, actAlt))}
          >
            <Send size={14} /> Fly here
          </button>
          <button
            className="flex items-center gap-1 rounded-md bg-edge/50 text-slate-100 px-2.5 py-1.5 text-xs font-semibold"
            onClick={act("Orbit", () => api.orbit(pendingTarget.lat, pendingTarget.lon, actAlt, 25, 4))}
          >
            <Orbit size={14} /> Orbit
          </button>
          <button
            className="flex items-center gap-1 rounded-md bg-edge/50 text-slate-100 px-2.5 py-1.5 text-xs font-semibold"
            onClick={act("Set Home", () => api.setHome(pendingTarget.lat, pendingTarget.lon))}
          >
            <Home size={14} /> Set Home
          </button>
          <button className="text-slate-500 hover:text-slate-200" onClick={() => setPendingTarget(null)}>
            <Trash2 size={14} />
          </button>
        </motion.div>
      )}

      {uiMode === "survey" && (
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
            {busy ? "Detecting…" : "Detect perimeters"}
          </button>

          {telem.lat == null && (
            <p className="text-[10px] leading-tight text-amber-300/80">
              No GPS fix yet — detect uses the drone's current position.
            </p>
          )}

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
              <p className="text-[10px] leading-tight text-slate-400">
                3D is detect → pick → plan → confirm. Vertex editing is in the 2D view.
              </p>
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
      )}

      {unavailable && (
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="glass max-w-sm rounded-xl p-5 text-center text-sm text-slate-300">
            3D maps unavailable on this Maps build. Ensure the{" "}
            <span className="text-accent">Map Tiles API</span> is enabled for your key.
          </div>
        </div>
      )}
    </div>
  );
}
