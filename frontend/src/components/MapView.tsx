import { useEffect, useRef, useState } from "react";
import { APIProvider, Map, useMap } from "@vis.gl/react-google-maps";
import { motion } from "framer-motion";
import { Crosshair, Home, LocateFixed, MapPin, Orbit, Send, Trash2, Upload, Play } from "lucide-react";
import { useGcs } from "../store/useGcs";
import { api } from "../lib/api";
import {
  orientedRectCorners,
  poseAtTime,
  zoneColor,
  mapObjectGlyph,
  mapObjectColor,
  REGION_HANDLES,
  regionHandlePos,
  resizeRegionByHandle,
} from "../lib/geo";
import Map3DView from "./Map3DView";
import PerimeterPlanner from "./PerimeterPlanner";
import FleetSurveyPanel from "./FleetSurveyPanel";
import PointsPanel from "./PointsPanel";

const KEY = import.meta.env.VITE_GOOGLE_MAPS_API_KEY as string | undefined;
const MAP_ID = import.meta.env.VITE_GOOGLE_MAPS_MAP_ID as string | undefined;
// maps3d (Photorealistic 3D) ships in the stable channel, so no version
// override is needed (avoids the alpha/beta "development only" banner).
const GMAPS_VERSION = import.meta.env.VITE_GMAPS_VERSION;
const FALLBACK_CENTER = { lat: 25.35338, lng: 55.38043 } /* DEMO: SITL spawn (Ajman, UAE) */;

/**
 * Pans the map to the operator's location once, shortly after load — WITHOUT
 * blocking the map from rendering (Google tiles appear immediately at the
 * fallback center). Skips if the drone already has a GPS fix. All lookups are
 * time-bounded so an offline/airgapped network can never hang the UI.
 */
function InitialLocate() {
  const map = useMap();
  const droneLat = useGcs((s) => s.telem.lat);
  const droneLon = useGcs((s) => s.telem.lon);
  const done = useRef(false);

  // Primary: center on the DRONE's present location as soon as it has a fix.
  useEffect(() => {
    if (!map || done.current || droneLat == null || droneLon == null) return;
    done.current = true;
    map.panTo({ lat: droneLat, lng: droneLon });
    map.setZoom(17);
  }, [map, droneLat, droneLon]);

  // Fallback: if no drone fix arrives shortly, use the operator's geolocation
  // (then IP), so we still don't sit on the far-away default center.
  useEffect(() => {
    if (!map) return;
    let cancelled = false;
    const go = (c: google.maps.LatLngLiteral) => {
      if (cancelled || done.current) return;
      done.current = true;
      map.panTo(c);
      map.setZoom(17);
    };
    const ipFallback = async () => {
      try {
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 3000);
        const r = await fetch("https://ipapi.co/json/", { signal: ctrl.signal });
        clearTimeout(timer);
        const j = await r.json();
        if (j.latitude && j.longitude) go({ lat: j.latitude, lng: j.longitude });
      } catch {
        /* offline / blocked — stay at fallback center */
      }
    };
    const t = setTimeout(() => {
      if (true) return; // DEMO: geolocation override disabled — hold FALLBACK_CENTER until drone fix
      if (navigator.geolocation) {
        navigator.geolocation.getCurrentPosition(
          (p) => go({ lat: p.coords.latitude, lng: p.coords.longitude }),
          () => void ipFallback(),
          { enableHighAccuracy: true, timeout: 5000, maximumAge: 60000 },
        );
        setTimeout(() => { if (!done.current) void ipFallback(); }, 3500);
      } else {
        void ipFallback();
      }
    }, 1500); // give the live drone fix a moment to arrive first
    return () => { cancelled = true; clearTimeout(t); };
  }, [map]);
  return null;
}

/** Imperative overlays (drone, trail, home, survey polygon, target) drawn via the maps API. */
function Overlays({ pendingTarget }: { pendingTarget: google.maps.LatLngLiteral | null }) {
  const map = useMap();
  const { surveyPolygon, home: homePos, mission, fleetTelem, fleetTrail, vehicles, activeVehicle, pois } = useGcs();
  const followVehicle = useGcs((s) => s.followVehicle);
  const fleetZones = useGcs((s) => s.fleetZones);
  const savedRegions = useGcs((s) => s.savedRegions);
  const selectedRegionId = useGcs((s) => s.selectedRegionId);
  const mapObjects = useGcs((s) => s.mapObjects);
  const pruneMapObjects = useGcs((s) => s.pruneMapObjects);

  // One marker + one trail polyline per vehicle id (keyed maps, managed below).
  const drones = useRef<Record<string, google.maps.Marker>>({});
  const paths = useRef<Record<string, google.maps.Polyline>>({});
  const home = useRef<google.maps.Marker>();
  const poly = useRef<google.maps.Polygon>();
  const target = useRef<google.maps.Marker>();
  const missionLine = useRef<google.maps.Polyline>();
  const missionPts = useRef<google.maps.Marker[]>([]);
  // Saved fleet search-area regions — one oriented-rectangle polygon per region
  // (keyed by region id) + an optional name label, plus a single draggable center
  // handle for the SELECTED region. Per-drone assigned zones are separate.
  const regionPolys = useRef<Record<string, google.maps.Polygon>>({});
  const regionLabels = useRef<Record<string, google.maps.Marker>>({});
  const regionHandle = useRef<google.maps.Marker>();
  // Edge/corner resize handles for the SELECTED region (keyed by handle id) —
  // dragging one resizes/reshapes the rectangle live, respecting its rotation.
  const resizeHandles = useRef<Record<string, google.maps.Marker>>({});
  const zonePolys = useRef<google.maps.Polygon[]>([]);
  const zoneLabels = useRef<google.maps.Marker[]>([]);
  // Per-drone planned lawnmower survey paths (one polyline per fleet zone, in the
  // drone's fleet color) — so EACH surveying drone's grid shows in its own zone.
  const zonePaths = useRef<google.maps.Polyline[]>([]);
  const poiMarkers = useRef<google.maps.Marker[]>([]);
  // Ground-localized detected objects — one marker per object id (per-class SVG
  // glyph). The tracked (locked) object is highlighted. Keyed so a vanished
  // object's marker is removed when its TTL drops it from the store.
  const objMarkers = useRef<Record<number, google.maps.Marker>>({});
  // Mission replay: PER-DRONE full recorded path (dim), the portion flown so far
  // (bright), and the animated ghost drone marker — keyed by flight id so a
  // whole mission (multiple drones) replays at once, each in its fleet color.
  const replayPaths = useRef<Record<string, google.maps.Polyline>>({});
  const replayDones = useRef<Record<string, google.maps.Polyline>>({});
  const replayDrones = useRef<Record<string, google.maps.Marker>>({});
  const replay = useGcs((s) => s.replay);

  // one-time object creation (per-vehicle markers/trails are created lazily below)
  useEffect(() => {
    if (!map) return;
    home.current = new google.maps.Marker({
      icon: {
        path: google.maps.SymbolPath.CIRCLE,
        scale: 7, fillColor: "#ffb020", fillOpacity: 1,
        strokeColor: "#1a1205", strokeWeight: 2,
        labelOrigin: new google.maps.Point(0, -2.6),
      },
      label: { text: "HOME", color: "#ffb020", fontSize: "10px", fontWeight: "700" },
      zIndex: 90,
    });
    poly.current = new google.maps.Polygon({
      map, strokeColor: "#22e3c4", strokeWeight: 2,
      fillColor: "#22e3c4", fillOpacity: 0.12,
    });
    target.current = new google.maps.Marker({
      icon: {
        path: "M 0,-10 0,10 M -10,0 10,0",
        strokeColor: "#ff4d5e", strokeWeight: 2, scale: 1.4,
      },
    });
    // Uploaded survey/mission path (amber, with direction arrows).
    missionLine.current = new google.maps.Polyline({
      map, geodesic: true, strokeColor: "#ffb020", strokeOpacity: 0.95, strokeWeight: 2.5,
      zIndex: 80,
      icons: [{
        icon: { path: google.maps.SymbolPath.FORWARD_OPEN_ARROW, scale: 2, strokeColor: "#ffb020" },
        offset: "24px", repeat: "90px",
      }],
    });
    // Draggable diamond handle for the SELECTED search-area's center. Dragging it
    // repositions that region live (the polygon + center coords + any zone preview
    // follow in real time — see the region effect below).
    regionHandle.current = new google.maps.Marker({
      draggable: true, zIndex: 92, cursor: "move",
      icon: {
        path: "M 0,-9 L 9,0 L 0,9 L -9,0 Z",
        fillColor: "#22e3c4", fillOpacity: 1,
        strokeColor: "#04241e", strokeWeight: 2, scale: 1,
      },
    });
    const onDrag = (e: google.maps.MapMouseEvent) => {
      const ll = e.latLng;
      const id = useGcs.getState().selectedRegionId;
      if (!ll || !id) return;
      useGcs.getState().updateRegion(id, { center: [ll.lat(), ll.lng()] });
    };
    regionHandle.current.addListener("drag", onDrag);
    regionHandle.current.addListener("dragend", onDrag);

    // Edge + corner resize handles for the SELECTED region. Dragging one
    // recomputes width_m/height_m (and recenters) LIVE via resizeRegionByHandle,
    // keeping the OPPOSITE side fixed and respecting the region's rotation. The
    // polygon, readouts, zone preview + per-drone paths all follow in real time
    // (the saved-regions effect repositions every handle on each store change).
    for (const h of REGION_HANDLES) {
      const isCorner = h.cx !== 0 && h.cy !== 0;
      const m = new google.maps.Marker({
        draggable: true,
        zIndex: 93,
        cursor: isCorner ? "nwse-resize" : h.cx !== 0 ? "ew-resize" : "ns-resize",
        icon: {
          path: isCorner
            ? "M -5,-5 L 5,-5 L 5,5 L -5,5 Z" // corner = square
            : google.maps.SymbolPath.CIRCLE,  // edge = dot
          scale: isCorner ? 1 : 5,
          fillColor: "#22e3c4",
          fillOpacity: 1,
          strokeColor: "#04241e",
          strokeWeight: 2,
        },
      });
      const onResize = (e: google.maps.MapMouseEvent) => {
        const ll = e.latLng;
        const st = useGcs.getState();
        const id = st.selectedRegionId;
        if (!ll || !id) return;
        const r = st.savedRegions.find((x) => x.id === id);
        if (!r) return;
        const next = resizeRegionByHandle(
          h, ll.lat(), ll.lng(),
          r.center[0], r.center[1], r.width_m, r.height_m, r.heading_deg,
        );
        st.updateRegion(id, {
          center: next.center,
          width_m: next.width_m,
          height_m: next.height_m,
        });
      };
      m.addListener("drag", onResize);
      m.addListener("dragend", onResize);
      resizeHandles.current[h.id] = m;
    }

    // Region polygons (one per saved region) are created/updated/removed in the
    // saved-regions effect below; replay overlays are created PER DRONE there too.
    return () => {
      [home, poly, target, missionLine, regionHandle].forEach((r) => r.current?.setMap(null));
      Object.values(resizeHandles.current).forEach((m) => m.setMap(null));
      resizeHandles.current = {};
      Object.values(regionPolys.current).forEach((p) => p.setMap(null));
      Object.values(regionLabels.current).forEach((m) => m.setMap(null));
      regionPolys.current = {};
      regionLabels.current = {};
      missionPts.current.forEach((m) => m.setMap(null));
      zonePolys.current.forEach((p) => p.setMap(null));
      zoneLabels.current.forEach((m) => m.setMap(null));
      zonePaths.current.forEach((p) => p.setMap(null));
      poiMarkers.current.forEach((m) => m.setMap(null));
      Object.values(objMarkers.current).forEach((m) => m.setMap(null));
      objMarkers.current = {};
      Object.values(drones.current).forEach((m) => m.setMap(null));
      Object.values(paths.current).forEach((p) => p.setMap(null));
      [replayPaths, replayDones, replayDrones].forEach((rec) =>
        Object.values(rec.current).forEach((o) => o.setMap(null)),
      );
      drones.current = {};
      paths.current = {};
      replayPaths.current = {};
      replayDones.current = {};
      replayDrones.current = {};
    };
  }, [map]);

  // Per-vehicle drone markers: create for new ids, update pos/heading/icon/label
  // each render, remove markers for ids no longer present. Active = teal full
  // opacity; others = amber, smaller, dimmer. Armed → red. Label = vehicle name.
  useEffect(() => {
    if (!map) return;
    const ids = Object.keys(fleetTelem).filter((id) => {
      const t = fleetTelem[id];
      return t && t.lat != null && t.lon != null;
    });

    // Drop markers for vehicles that disappeared (or lost their fix).
    for (const id of Object.keys(drones.current)) {
      if (!ids.includes(id)) {
        drones.current[id].setMap(null);
        delete drones.current[id];
      }
    }

    for (const id of ids) {
      const t = fleetTelem[id];
      const pos = { lat: t.lat!, lng: t.lon! };
      const isActive = id === activeVehicle;
      const name = vehicles.find((v) => v.id === id)?.name ?? id;
      const baseColor = isActive ? "#22e3c4" : "#ffb020";
      const fillColor = t.armed ? "#ff4d5e" : baseColor;

      let marker = drones.current[id];
      if (!marker) {
        marker = new google.maps.Marker({ map });
        drones.current[id] = marker;
      }
      marker.setPosition(pos);
      marker.setZIndex(isActive ? 100 : 95);
      marker.setLabel({ text: name, color: baseColor, fontSize: "10px", fontWeight: "700" });
      marker.setIcon({
        path: "M 0,-13 L 9,11 L 0,5 L -9,11 Z",
        fillColor,
        fillOpacity: isActive ? 1 : 0.85,
        strokeColor: "#05221d", strokeWeight: 1.5,
        rotation: t.heading ?? 0,
        scale: isActive ? 1.5 : 1.25,
        anchor: new google.maps.Point(0, 0),
        labelOrigin: new google.maps.Point(0, 16),
      });
    }

    // Follow the active vehicle if it has a fix; else any vehicle that does, so
    // follow still works when the active drone is offline/fixless.
    if (followVehicle) {
      const a =
        activeVehicle && fleetTelem[activeVehicle]?.lat != null
          ? fleetTelem[activeVehicle]
          : Object.values(fleetTelem).find((t) => t && t.lat != null);
      if (a && a.lat != null && a.lon != null) map.panTo({ lat: a.lat, lng: a.lon });
    }
  }, [fleetTelem, vehicles, activeVehicle, followVehicle, map]);

  useEffect(() => {
    if (!home.current) return;
    if (homePos) {
      home.current.setPosition(homePos);
      home.current.setMap(map);
    } else {
      home.current.setMap(null);
    }
  }, [homePos, map]);

  // Per-vehicle trails: one polyline each from fleetTrail. Active = brighter teal,
  // others = dimmer in the vehicle's color. Remove polylines for vanished ids.
  useEffect(() => {
    if (!map) return;
    const ids = Object.keys(fleetTrail);

    for (const id of Object.keys(paths.current)) {
      if (!ids.includes(id)) {
        paths.current[id].setMap(null);
        delete paths.current[id];
      }
    }

    for (const id of ids) {
      const isActive = id === activeVehicle;
      let line = paths.current[id];
      if (!line) {
        line = new google.maps.Polyline({ map, geodesic: true });
        paths.current[id] = line;
      }
      line.setOptions({
        strokeColor: isActive ? "#22e3c4" : "#ffb020",
        strokeOpacity: isActive ? 0.9 : 0.5,
        strokeWeight: isActive ? 2.5 : 2,
        zIndex: isActive ? 60 : 55,
      });
      line.setPath(fleetTrail[id] ?? []);
    }
  }, [fleetTrail, activeVehicle, map]);

  useEffect(() => {
    poly.current?.setPath(surveyPolygon);
  }, [surveyPolygon]);

  // Uploaded mission: amber path + a dot at each waypoint.
  useEffect(() => {
    if (!missionLine.current) return;
    missionLine.current.setPath(mission);
    missionPts.current.forEach((m) => m.setMap(null));
    missionPts.current = mission.map((p) => new google.maps.Marker({
      map, position: p, zIndex: 81,
      icon: { path: google.maps.SymbolPath.CIRCLE, scale: 3.5, fillColor: "#ffb020",
              fillOpacity: 1, strokeColor: "#1a1205", strokeWeight: 1 },
    }));
  }, [mission, map]);

  // Saved search-area regions — one oriented-rectangle polygon per region,
  // recomputed from center + width × breadth + rotation whenever ANY region is
  // added/edited/dragged/removed. The SELECTED region is bright + filled with its
  // draggable center handle shown; the others are dim outlines. Clicking a
  // polygon selects that region (loads it into the panel for editing). All of
  // this is live, before any backend command.
  useEffect(() => {
    if (!map) return;
    const ids = new Set(savedRegions.map((r) => r.id as string));

    // Drop polygons/labels for regions that no longer exist.
    for (const id of Object.keys(regionPolys.current)) {
      if (!ids.has(id)) {
        regionPolys.current[id].setMap(null);
        delete regionPolys.current[id];
        regionLabels.current[id]?.setMap(null);
        delete regionLabels.current[id];
      }
    }

    savedRegions.forEach((r, i) => {
      const id = r.id as string;
      const isSel = id === selectedRegionId;
      const color = zoneColor(r.name, i);
      const corners = orientedRectCorners(
        r.center[0], r.center[1], r.width_m, r.height_m, r.heading_deg,
      ).map(([la, lo]) => ({ lat: la, lng: lo }));

      let p = regionPolys.current[id];
      if (!p) {
        p = new google.maps.Polygon({ map });
        p.addListener("click", () => useGcs.getState().selectRegion(id));
        regionPolys.current[id] = p;
      }
      p.setOptions({
        strokeColor: isSel ? "#22e3c4" : color,
        strokeWeight: isSel ? 3 : 2,
        strokeOpacity: isSel ? 1 : 0.7,
        fillColor: isSel ? "#22e3c4" : color,
        fillOpacity: isSel ? 0.12 : 0.05,
        zIndex: isSel ? 71 : 68,
        clickable: true,
      });
      p.setPath(corners);
      p.setMap(map);

      let lbl = regionLabels.current[id];
      if (!lbl) {
        lbl = new google.maps.Marker({
          map,
          icon: { path: google.maps.SymbolPath.CIRCLE, scale: 0, fillOpacity: 0, strokeOpacity: 0 },
          zIndex: 72,
        });
        lbl.addListener("click", () => useGcs.getState().selectRegion(id));
        regionLabels.current[id] = lbl;
      }
      lbl.setPosition({ lat: r.center[0], lng: r.center[1] });
      lbl.setLabel({
        text: r.name,
        color: isSel ? "#22e3c4" : color,
        fontSize: "11px",
        fontWeight: "700",
      });
      lbl.setMap(map);
    });

    // Center drag-handle + edge/corner resize handles: only on the selected
    // region. Each handle is repositioned from the (possibly just-dragged)
    // geometry so it tracks the rectangle live, with rotation applied.
    const sel = savedRegions.find((r) => r.id === selectedRegionId);
    if (regionHandle.current) {
      if (sel) {
        regionHandle.current.setPosition({ lat: sel.center[0], lng: sel.center[1] });
        regionHandle.current.setMap(map);
      } else {
        regionHandle.current.setMap(null);
      }
    }
    for (const h of REGION_HANDLES) {
      const m = resizeHandles.current[h.id];
      if (!m) continue;
      if (sel) {
        const [la, lo] = regionHandlePos(
          h, sel.center[0], sel.center[1], sel.width_m, sel.height_m, sel.heading_deg,
        );
        m.setPosition({ lat: la, lng: lo });
        m.setMap(map);
      } else {
        m.setMap(null);
      }
    }
  }, [savedRegions, selectedRegionId, map]);

  // Per-drone assigned zones — filled + outlined in the vehicle's color, labeled
  // with the drone name. Rebuilt whenever the assignment set changes.
  useEffect(() => {
    if (!map) return;
    zonePolys.current.forEach((p) => p.setMap(null));
    zoneLabels.current.forEach((m) => m.setMap(null));
    zonePaths.current.forEach((p) => p.setMap(null));
    zonePolys.current = [];
    zoneLabels.current = [];
    zonePaths.current = [];
    fleetZones.forEach((z, i) => {
      if (!z.polygon || z.polygon.length < 3) return;
      const color = zoneColor(z.name || z.vehicle, i);
      const path = z.polygon.map(([lat, lng]) => ({ lat, lng }));
      const poly = new google.maps.Polygon({
        map, paths: path,
        strokeColor: color, strokeWeight: 3, strokeOpacity: 1,
        fillColor: color, fillOpacity: 0.16, zIndex: 65,
      });
      zonePolys.current.push(poly);
      // THIS drone's planned lawnmower path, drawn inside its zone in its color
      // (direction arrows so the sweep order reads). Live preview OR flown grid.
      if (z.path && z.path.length >= 2) {
        const line = new google.maps.Polyline({
          map,
          geodesic: true,
          path: z.path.map(([lat, lng]) => ({ lat, lng })),
          strokeColor: color,
          strokeOpacity: z.flying ? 0.95 : 0.7,
          strokeWeight: 2,
          zIndex: 67,
          icons: [{
            icon: { path: google.maps.SymbolPath.FORWARD_OPEN_ARROW, scale: 1.8, strokeColor: color },
            offset: "20px", repeat: "110px",
          }],
        });
        zonePaths.current.push(line);
      }
      const cx = path.reduce((s, p) => s + p.lat, 0) / path.length;
      const cy = path.reduce((s, p) => s + p.lng, 0) / path.length;
      const label = new google.maps.Marker({
        map, position: { lat: cx, lng: cy },
        icon: { path: google.maps.SymbolPath.CIRCLE, scale: 0, fillOpacity: 0, strokeOpacity: 0 },
        label: { text: z.name, color, fontSize: "11px", fontWeight: "700" },
        zIndex: 66,
      });
      zoneLabels.current.push(label);
    });
  }, [fleetZones, map]);

  useEffect(() => {
    if (!target.current) return;
    if (pendingTarget) {
      target.current.setPosition(pendingTarget);
      target.current.setMap(map);
    } else {
      target.current.setMap(null);
    }
  }, [pendingTarget, map]);

  // Named points of interest (operator-dropped) — small violet labeled markers
  // that persist. Right-click one to remove it.
  useEffect(() => {
    if (!map) return;
    poiMarkers.current.forEach((m) => m.setMap(null));
    poiMarkers.current = pois.map((p) => {
      const m = new google.maps.Marker({
        map,
        position: { lat: p.lat, lng: p.lng },
        zIndex: 85,
        title: `${p.name} — right-click to remove`,
        icon: {
          path: google.maps.SymbolPath.CIRCLE,
          fillColor: "#b07cff", fillOpacity: 1, strokeColor: "#1a0f2e", strokeWeight: 1.5,
          scale: 5, labelOrigin: new google.maps.Point(0, 2.4),
        },
        label: { text: p.name, color: "#c9b3ff", fontSize: "10px", fontWeight: "700" },
      });
      m.addListener("rightclick", () => useGcs.getState().removePoi(p.id));
      return m;
    });
  }, [pois, map]);

  // Detected objects geolocated to the ground — one marker per object id, drawn
  // with the per-class SVG glyph at its estimated lat/lon. The TRACKED (locked)
  // object is bright accent + larger + labeled so it stands out as the followed
  // target; the rest are smaller per-class-colored markers. Markers are created
  // on first sight, updated each store change, and removed when the object's TTL
  // drops it from the store.
  useEffect(() => {
    if (!map) return;
    const live = Object.values(mapObjects);
    const ids = new Set(live.map((o) => o.id));
    for (const idStr of Object.keys(objMarkers.current)) {
      const id = Number(idStr);
      if (!ids.has(id)) {
        objMarkers.current[id].setMap(null);
        delete objMarkers.current[id];
      }
    }
    for (const o of live) {
      const color = o.tracked ? "#ff4d5e" : mapObjectColor(o.label);
      let m = objMarkers.current[o.id];
      if (!m) {
        m = new google.maps.Marker({ map });
        objMarkers.current[o.id] = m;
      }
      m.setPosition({ lat: o.lat, lng: o.lon });
      m.setZIndex(o.tracked ? 89 : 84);
      m.setIcon({
        path: mapObjectGlyph(o.label),
        fillColor: color,
        fillOpacity: o.tracked ? 1 : 0.9,
        strokeColor: o.tracked ? "#350a0e" : "#0a1322",
        strokeWeight: 1.2,
        scale: o.tracked ? 1.55 : 1.05,
        anchor: new google.maps.Point(0, 0),
        labelOrigin: new google.maps.Point(0, 8),
      });
      // Label only the tracked target (avoids clutter when many objects show).
      if (o.tracked) {
        m.setLabel({ text: o.label.toUpperCase(), color, fontSize: "9px", fontWeight: "700" });
      } else {
        m.setLabel(null as unknown as string);
      }
    }
  }, [mapObjects, map]);

  // TTL sweeper: prune stale detections on a timer so they fade out even when no
  // new batch arrives (e.g. vision stopped or the camera lost them).
  useEffect(() => {
    const t = setInterval(() => pruneMapObjects(), 1000);
    return () => clearInterval(t);
  }, [pruneMapObjects]);

  // Replay: draw each drone's full recorded path when a replay loads (or clear
  // all replay overlays when replay ends), and pan to the first drone's start.
  // Keyed by flight id so a whole mission (several drones) draws at once.
  const replayKey = replay ? replay.drones.map((d) => d.flightId).join(",") : null;
  useEffect(() => {
    if (!map) return;
    // Tear down overlays for drones no longer in the replay (or all of them when
    // replay ends).
    const liveIds = new Set((replay?.drones ?? []).map((d) => d.flightId));
    for (const rec of [replayPaths, replayDones, replayDrones]) {
      for (const id of Object.keys(rec.current)) {
        if (!liveIds.has(id)) {
          rec.current[id].setMap(null);
          delete rec.current[id];
        }
      }
    }
    if (!replay) return;
    let panned = false;
    for (const d of replay.drones) {
      if (d.path.length < 2) continue;
      const full = d.path.map(([lat, lng]) => ({ lat, lng }));
      let dim = replayPaths.current[d.flightId];
      if (!dim) {
        dim = new google.maps.Polyline({ geodesic: true, strokeWeight: 2, zIndex: 72 });
        replayPaths.current[d.flightId] = dim;
      }
      dim.setOptions({ strokeColor: d.color, strokeOpacity: 0.4 });
      dim.setPath(full);
      dim.setMap(map);
      if (!panned) {
        map.panTo(full[0]);
        panned = true;
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [replayKey, map]);

  // Replay: animate each drone's ghost + flown-so-far overlay on every clock
  // tick, on the SHARED mission clock (replay.startTs + replay.t). A drone whose
  // window hasn't started / has ended just clamps its ghost to its path ends.
  const replayT = replay?.t ?? null;
  useEffect(() => {
    if (!map || !replay) return;
    for (const d of replay.drones) {
      if (d.path.length === 0) continue;
      // Translate the shared clock to this drone's own timeline offset.
      const localT = replay.startTs + replay.t - d.startTs;
      const pose = poseAtTime(d.path, d.times, d.startTs, localT);
      if (!pose) continue;

      let drone = replayDrones.current[d.flightId];
      if (!drone) {
        drone = new google.maps.Marker({ zIndex: 110 });
        replayDrones.current[d.flightId] = drone;
      }
      drone.setPosition({ lat: pose.lat, lng: pose.lon });
      drone.setMap(map);
      drone.setIcon({
        path: "M 0,-13 L 9,11 L 0,5 L -9,11 Z",
        fillColor: d.color, fillOpacity: 1,
        strokeColor: "#05221d", strokeWeight: 1.5,
        rotation: pose.heading, scale: 1.5,
        anchor: new google.maps.Point(0, 0),
        labelOrigin: new google.maps.Point(0, 16),
      });
      drone.setLabel({ text: d.vehicleName, color: d.color, fontSize: "10px", fontWeight: "700" });

      let done = replayDones.current[d.flightId];
      if (!done) {
        done = new google.maps.Polyline({ geodesic: true, strokeWeight: 3.5, zIndex: 73 });
        replayDones.current[d.flightId] = done;
      }
      done.setOptions({ strokeColor: d.color, strokeOpacity: 0.95 });
      const flown = d.path.slice(0, pose.index + 1).map(([lat, lng]) => ({ lat, lng }));
      flown.push({ lat: pose.lat, lng: pose.lon });
      done.setPath(flown);
      done.setMap(map);
    }
  }, [replayT, replay, map]);

  return null;
}

function MapControls() {
  const map = useMap();
  const { telem, fleetTelem, activeVehicle, followVehicle, toggleFollow } = useGcs();
  // Center on the active vehicle if it has a fix; otherwise the first connected
  // vehicle that does — so it still works when the active drone is offline/fixless.
  const centerOnVehicle = () => {
    const pick =
      telem.lat != null
        ? telem
        : activeVehicle && fleetTelem[activeVehicle]?.lat != null
        ? fleetTelem[activeVehicle]
        : Object.values(fleetTelem).find((t) => t && t.lat != null) ?? null;
    if (pick && pick.lat != null) map?.panTo({ lat: pick.lat, lng: pick.lon! });
  };
  return (
    <div className="absolute left-3 top-16 z-30 flex flex-col gap-2">
      <button
        onClick={toggleFollow}
        className={`glass rounded-lg p-2 ${followVehicle ? "text-accent glow-accent" : "text-slate-300"}`}
        title="Follow vehicle"
      >
        <LocateFixed size={18} />
      </button>
      <button
        onClick={centerOnVehicle}
        className="glass rounded-lg p-2 text-slate-300"
        title="Center on vehicle"
      >
        <Crosshair size={18} />
      </button>
    </div>
  );
}

function ActionCards({
  pendingTarget,
  clearTarget,
}: {
  pendingTarget: google.maps.LatLngLiteral | null;
  clearTarget: () => void;
}) {
  const { uiMode, surveyPolygon, clearSurvey, telem } = useGcs();
  const pushLog = useGcs((s) => s.pushLog);
  const addPoi = useGcs((s) => s.addPoi);
  const [poiName, setPoiName] = useState("");
  const alt = telem.alt_rel && telem.alt_rel > 3 ? Math.round(telem.alt_rel) : 20;

  const wrap = (label: string, fn: () => Promise<unknown>) => async () => {
    try {
      await fn();
      pushLog("cmd", `${label} sent`);
    } catch (e) {
      pushLog("error", `${label}: ${(e as Error).message}`, 3);
    }
  };

  if (uiMode === "navigate" && pendingTarget) {
    return (
      <motion.div
        initial={{ y: 10, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        className="glass absolute left-1/2 top-16 z-40 -translate-x-1/2 rounded-xl px-3 py-2 flex items-center gap-2"
      >
        <span className="tnum text-xs text-slate-300">
          {pendingTarget.lat.toFixed(5)}, {pendingTarget.lng.toFixed(5)}
        </span>
        <input
          value={poiName}
          onChange={(e) => setPoiName(e.target.value)}
          placeholder="name (optional)"
          className="w-28 bg-ink/70 border border-edge/60 rounded px-2 py-1 text-xs text-slate-200 placeholder:text-slate-500"
        />
        <button
          className="flex items-center gap-1 rounded-md bg-edge/50 text-slate-100 px-2.5 py-1.5 text-xs font-semibold"
          title="Drop a persistent named marker here"
          onClick={() => {
            const p = addPoi(poiName, pendingTarget.lat, pendingTarget.lng);
            pushLog("cmd", `Marker "${p.name}" placed`);
            setPoiName("");
            clearTarget();
          }}
        >
          <MapPin size={14} /> Mark
        </button>
        <button
          className="flex items-center gap-1 rounded-md bg-accent/20 text-accent px-2.5 py-1.5 text-xs font-semibold"
          onClick={wrap("Fly here", () =>
            api.goto(pendingTarget.lat, pendingTarget.lng, alt).then(clearTarget),
          )}
        >
          <Send size={14} /> Fly here
        </button>
        <button
          className="flex items-center gap-1 rounded-md bg-edge/50 text-slate-100 px-2.5 py-1.5 text-xs font-semibold"
          onClick={wrap(poiName.trim() ? `Orbit ${poiName.trim()}` : "Orbit", () => {
            // Name the point (drops a labeled POI marker) then orbit it.
            if (poiName.trim()) addPoi(poiName, pendingTarget.lat, pendingTarget.lng);
            return api.orbit(pendingTarget.lat, pendingTarget.lng, alt, 25, 4).then(() => {
              setPoiName("");
              clearTarget();
            });
          })}
        >
          <Orbit size={14} /> Orbit
        </button>
        <button
          className="flex items-center gap-1 rounded-md bg-edge/50 text-slate-100 px-2.5 py-1.5 text-xs font-semibold"
          onClick={wrap("Set Home", () =>
            api.setHome(pendingTarget.lat, pendingTarget.lng).then(clearTarget),
          )}
        >
          <Home size={14} /> Set Home
        </button>
        <button className="text-slate-500 hover:text-slate-200" onClick={clearTarget}>
          <Trash2 size={14} />
        </button>
      </motion.div>
    );
  }

  if (uiMode === "survey") {
    return (
      <motion.div
        initial={{ y: 10, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        className="glass absolute left-1/2 top-16 z-40 -translate-x-1/2 rounded-xl px-3 py-2 flex items-center gap-2"
      >
        <span className="text-xs text-slate-300">
          Survey · {surveyPolygon.length} vertices
        </span>
        <button
          disabled={surveyPolygon.length < 3}
          className="flex items-center gap-1 rounded-md bg-accent/20 text-accent px-2.5 py-1.5 text-xs font-semibold disabled:opacity-40"
          onClick={wrap("Upload survey", () =>
            api.survey(
              surveyPolygon.map((p) => [p.lat, p.lng] as [number, number]),
              { altitude: 30, line_spacing_m: 25 },
            ),
          )}
        >
          <Upload size={14} /> Generate + Upload
        </button>
        <button
          disabled={surveyPolygon.length < 3}
          className="flex items-center gap-1 rounded-md bg-ok/20 text-ok px-2.5 py-1.5 text-xs font-semibold disabled:opacity-40"
          onClick={wrap("Execute survey", () =>
            api.survey(
              surveyPolygon.map((p) => [p.lat, p.lng] as [number, number]),
              { altitude: 30, line_spacing_m: 25, execute: true },
            ),
          )}
        >
          <Play size={14} /> Execute
        </button>
        <button className="text-slate-500 hover:text-slate-200" onClick={clearSurvey}>
          <Trash2 size={14} />
        </button>
      </motion.div>
    );
  }

  return null;
}

// Strip business/POI/transit clutter; keep major place names + roads.
const MAP_STYLES: google.maps.MapTypeStyle[] = [
  { featureType: "poi", stylers: [{ visibility: "off" }] },
  { featureType: "poi.business", stylers: [{ visibility: "off" }] },
  { featureType: "transit", stylers: [{ visibility: "off" }] },
  { featureType: "road", elementType: "labels.icon", stylers: [{ visibility: "off" }] },
];

function MapInner() {
  const { uiMode, addSurveyVertex } = useGcs();
  const fleetPickCenter = useGcs((s) => s.fleetPickCenter);
  const setFleetPickCenter = useGcs((s) => s.setFleetPickCenter);
  const addRegion = useGcs((s) => s.addRegion);
  const updateRegion = useGcs((s) => s.updateRegion);
  const selectedRegionId = useGcs((s) => s.selectedRegionId);
  const savedRegions = useGcs((s) => s.savedRegions);
  const [pendingTarget, setPendingTarget] = useState<google.maps.LatLngLiteral | null>(null);

  // Map renders immediately at the fallback center; InitialLocate pans to the
  // operator's location once resolved (non-blocking, time-bounded).
  return (
    <>
    <Map
      mapId={MAP_ID}
      defaultCenter={FALLBACK_CENTER}
      defaultZoom={17}
      mapTypeId="hybrid"
      styles={MAP_STYLES}
      gestureHandling="greedy"
      disableDefaultUI
      tilt={0}
      onClick={(e) => {
        const ll = e.detail.latLng;
        if (!ll) return;
        // Fleet search-area picking takes precedence over the mode handlers.
        if (fleetPickCenter) {
          if (selectedRegionId) {
            // Re-positioning the selected region's center (alternative to drag).
            updateRegion(selectedRegionId, { center: [ll.lat, ll.lng] });
          } else {
            // Drop a brand-new named region; addRegion selects + persists it.
            const n = savedRegions.length + 1;
            addRegion({
              name: `Sector ${n}`,
              center: [ll.lat, ll.lng],
              width_m: 400,
              height_m: 300,
              heading_deg: 0,
            });
          }
          setFleetPickCenter(false);
          return;
        }
        if (uiMode === "survey") addSurveyVertex(ll);
        else if (uiMode === "navigate") setPendingTarget(ll);
      }}
    >
      <InitialLocate />
      <Overlays pendingTarget={pendingTarget} />
      <MapControls />
      <PerimeterPlanner />
      <PointsPanel />
      <ActionCards pendingTarget={pendingTarget} clearTarget={() => setPendingTarget(null)} />
    </Map>
    <FleetSurveyPanel />
    </>
  );
}

export default function MapView() {
  if (!KEY) {
    return (
      <div className="absolute inset-0 flex items-center justify-center bg-[radial-gradient(circle_at_30%_20%,#0b1322,#05070b)]">
        <div className="glass max-w-md rounded-xl p-6 text-center">
          <p className="text-accent font-semibold mb-2">Google Maps key needed</p>
          <p className="text-sm text-slate-300">
            Add <code className="text-accent">VITE_GOOGLE_MAPS_API_KEY</code> to{" "}
            <code>frontend/.env</code> and restart the dev server.
          </p>
        </div>
      </div>
    );
  }
  return (
    <APIProvider apiKey={KEY} version={GMAPS_VERSION}>
      <ViewSwitch />
    </APIProvider>
  );
}

function ViewSwitch() {
  const view3d = useGcs((s) => s.view3d);
  return view3d ? <Map3DView /> : <MapInner />;
}
