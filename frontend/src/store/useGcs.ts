import { create } from "zustand";
import type { AllyMarker, AllyMarkerLive, AutotuneState, FlightDetail, LogEvent, MapObject, MapObjectLive, MissionDetail, ReplayDrone, ReplayState, SurveyCandidate, SurveyPreview, Telemetry, Track, VehicleId, VehicleInfo } from "../lib/types";
import { zoneColor } from "../lib/geo";

// Detected-object TTL: a ground-localized object fades/removes if it hasn't been
// re-seen within this window. The backend republishes live objects at ~3-4 Hz,
// well inside this, so only genuinely stale detections age out.
export const MAP_OBJECT_TTL_MS = 3000;

// Ally-marker TTL: the AR ally marker fades if no fresh projection arrives within
// this window. The backend publishes at ~6 Hz (well inside this), so the marker
// only disappears when a vehicle loses its fix / the loop stops emitting.
export const ALLY_MARKER_TTL_MS = 1500;

// A blank, disconnected telemetry snapshot. Exported so the StatusBar can show a
// truthful "NO LINK" for a fleet member that has no telemetry of its own yet,
// instead of borrowing the active drone's data (an other-drone-data hazard).
export const EMPTY: Telemetry = {
  connected: false, armed: false, mode: null,
  lat: null, lon: null, alt_msl: null, alt_rel: null, heading: null,
  roll: null, pitch: null, yaw: null,
  groundspeed: null, airspeed: null, climb: null, throttle: null,
  battery_pct: null, battery_voltage: null, battery_current: null,
  gps_fix: null, satellites: null, vx: null, vy: null, vz: null,
  home_lat: null, home_lon: null, last_heartbeat: 0,
};

export type Mode = "navigate" | "survey" | "track";

// Operator-defined survey region (center + dimensions + orientation) used by the
// coordinated fleet-survey flow. Drawn live in 2D + 3D while being defined.
// `id` is a stable handle for the saved-regions list (optional on the live
// preview that has no saved home yet).
export interface FleetRegion {
  id?: string;
  name: string;
  center: [number, number]; // [lat, lon]
  width_m: number;
  height_m: number;
  heading_deg: number;
}

// One assigned survey zone per drone, returned by the coordinated backend split.
// `path` is THIS drone's planned lawnmower survey polyline ([lat, lon] turn
// points) inside its zone — drawn in the drone's fleet color in 2D + 3D. It may
// come from the backend (the actual planned grid) or be computed live in the
// frontend from the zone polygon for the pre-commit preview.
export interface FleetZone {
  vehicle: string;
  name: string;
  polygon: [number, number][]; // [lat, lon]
  path?: [number, number][]; // [lat, lon] lawnmower turn points
  // false (or undefined) = a LIVE pre-commit preview computed from the region;
  // true = a committed survey the backend is flying. Drives the panel's status
  // text and lets the live-preview effect avoid overwriting a flying assignment.
  flying?: boolean;
}

export interface Poi {
  id: number;
  name: string;
  lat: number;
  lng: number;
}

export interface ConvEntry {
  id: number;
  role: "user" | "assistant" | "tool";
  text?: string;
  tool?: { name: string; args: Record<string, unknown>; ok?: boolean };
  ts: number;
}

// Operator-dropped POIs persist across refresh (same pattern as the saved
// regions below). The
// App.tsx socket-open effect re-syncs them to the backend so the voice agent
// re-registers them automatically.
const POIS_KEY = "gcs.pois";

function loadPois(): Poi[] {
  try {
    const raw = localStorage.getItem(POIS_KEY);
    if (!raw) return [];
    const v = JSON.parse(raw) as Poi[];
    if (!Array.isArray(v)) return [];
    return v.filter(
      (p) =>
        p &&
        typeof p.id === "number" &&
        typeof p.lat === "number" &&
        typeof p.lng === "number" &&
        typeof p.name === "string",
    );
  } catch {
    return [];
  }
}

function savePois(pois: Poi[]) {
  try {
    if (pois.length) localStorage.setItem(POIS_KEY, JSON.stringify(pois));
    else localStorage.removeItem(POIS_KEY);
  } catch {
    /* localStorage unavailable — ignore */
  }
}

// Named survey/search-area regions the operator saved. Persisted to localStorage
// (same pattern as pois) so the planned search areas survive a refresh.
// Names are unique (case-insensitive): re-using a name UPDATES that region, just
// like the POI rule, so "Sector 1" can never resolve to a stale duplicate.
const REGIONS_KEY = "gcs.regions";

function loadRegions(): FleetRegion[] {
  try {
    const raw = localStorage.getItem(REGIONS_KEY);
    if (!raw) return [];
    const v = JSON.parse(raw) as FleetRegion[];
    if (!Array.isArray(v)) return [];
    return v.filter(
      (r) =>
        r &&
        typeof r.name === "string" &&
        Array.isArray(r.center) &&
        r.center.length === 2 &&
        typeof r.center[0] === "number" &&
        typeof r.center[1] === "number" &&
        typeof r.width_m === "number" &&
        typeof r.height_m === "number" &&
        typeof r.heading_deg === "number",
    ).map((r) => ({ ...r, id: r.id ?? `rgn_${Math.random().toString(36).slice(2, 9)}` }));
  } catch {
    return [];
  }
}

function saveRegions(regions: FleetRegion[]) {
  try {
    if (regions.length) localStorage.setItem(REGIONS_KEY, JSON.stringify(regions));
    else localStorage.removeItem(REGIONS_KEY);
  } catch {
    /* localStorage unavailable — ignore */
  }
}

let regionSeq = 0;
function newRegionId(): string {
  return `rgn_${Date.now().toString(36)}_${(regionSeq++).toString(36)}`;
}

interface GcsState {
  socketOpen: boolean;
  vehicles: VehicleInfo[];
  activeVehicle: string | null;
  telem: Telemetry; // ACTIVE vehicle (drives the HUD/instruments)
  trail: google.maps.LatLngLiteral[]; // ACTIVE vehicle trail
  // Per-vehicle latest telemetry + trail, keyed by vehicle id — so the map can
  // draw EVERY drone at once (the HUD still shows only the active one).
  fleetTelem: Record<string, Telemetry>;
  fleetTrail: Record<string, google.maps.LatLngLiteral[]>;
  home: google.maps.LatLngLiteral | null;
  mission: google.maps.LatLngLiteral[];
  view3d: boolean;
  log: LogEvent[];
  tracks: Track[];
  // Ground-localized detected objects (car/person/bike/...) keyed by object id,
  // each stamped with `seen` (ms) for a per-object TTL. Rendered as icons on the
  // 2D + 3D maps; the `tracked` one (locked follow target) is distinguished.
  mapObjects: Record<number, MapObjectLive>;
  // AR ally markers (other fleet vehicles projected into the active camera feed),
  // keyed by ally id, each stamped with `seen` for a short TTL. Rendered as a
  // distinct marker on the Overwatch FPV overlay.
  allyMarkers: Record<string, AllyMarkerLive>;
  frameSize: { w: number; h: number };
  selectedTrack: number | null;
  visionRunning: boolean;
  lockActive: boolean;
  // Anonymized vehicle-ID for the currently locked car (plate + masked record).
  // Set from the backend "vehicle_id" message; cleared when the lock is lost or
  // vision stops.
  vehicleId: VehicleId | null;
  uiMode: Mode;
  surveyPolygon: google.maps.LatLngLiteral[];
  surveyCandidates: SurveyCandidate[];
  surveyChoice: number | null;
  // The planned-but-not-yet-flown survey (clean polygon + lawnmower preview path),
  // shown as a preview on the map. Non-null = a survey is staged and the UI shows
  // the "Confirm & fly" / "Cancel" gate. Set by "Create mission" (tap path) or by
  // the voice survey_pending event; cleared on commit/cancel.
  surveyPreview: SurveyPreview | null;
  fleetRegion: FleetRegion | null; // LIVE preview rectangle (drawn in 2D + 3D)
  fleetZones: FleetZone[];
  fleetPickCenter: boolean; // map clicks set the region center while true
  // Named, persisted search-area regions + which one is currently selected for
  // editing/dragging. The selected region's geometry is mirrored into
  // `fleetRegion` so the existing map render path draws it live.
  savedRegions: FleetRegion[];
  selectedRegionId: string | null;
  pois: Poi[]; // named points of interest the operator dropped (e.g. for orbit)
  followVehicle: boolean;
  conversation: ConvEntry[];
  reportOpen: boolean;
  // Smart-RTL: set from the backend "low_battery" event; drives the alert banner.
  // Cleared when the operator dismisses it (or acts on it).
  lowBatteryAlert: { vehicle: string; name: string; battery_pct: number; threshold: number } | null;
  // Mission replay session (null = live mode). When set, the map renders a ghost
  // drone animated along the recorded path; live telemetry is left untouched.
  replay: ReplayState | null;
  // Whether the secondary (Outrider) feed panel is shown. Hiding collapses the
  // panel to a small edge-tab that brings it back (WebRTC stays alive).
  showSecondFeed: boolean;
  // Whether the MAIN (Overwatch) feed panel is shown; hidden → docks to a tab.
  showMainFeed: boolean;
  // True while drag-drawing a click-to-track box on the main feed (disables drag).
  boxSelecting: boolean;
  // Which video feed (if any) is in fullscreen-focus mode. The focused panel
  // grows large/centered; the other shrinks to a corner thumbnail.
  focusedFeed: "main" | "feed2" | null;
  // Live PX4 autotune state per vehicle id, set from the WS `autotune` event (and
  // the REST status on demand). Drives the Autotune panel's progress bar / axis /
  // STATUSTEXT feed. A vehicle absent here has never tuned (treated as IDLE).
  autotune: Record<string, AutotuneState>;
  // Per-vehicle Ready-for-Flight gate state. `ready` = the operator has enabled
  // the slider; while OFF the backend refuses every flight-authorizing command.
  // `locked` = the vehicle is armed+airborne (>1m alt_rel), so the gate is force-
  // held ON — the disable toggle is disabled in the UI. Populated from the
  // backend's telemetry payload (see main.py _telemetry_loop) and refreshed
  // on GET /api/safety/ready_for_flight on socket-open.
  readyForFlight: Record<string, { ready: boolean; locked: boolean }>;

  setSocketOpen: (v: boolean) => void;
  setShowSecondFeed: (v: boolean) => void;
  setShowMainFeed: (v: boolean) => void;
  setBoxSelecting: (v: boolean) => void;
  setFocusedFeed: (f: "main" | "feed2" | null) => void;
  setReportOpen: (v: boolean) => void;
  setVehicles: (v: VehicleInfo[]) => void;
  setActiveVehicle: (id: string) => void;
  toggleFollow: () => void;
  toggle3d: () => void;
  set2d: () => void;
  setTelemetry: (t: Telemetry) => void;
  ingestTelemetry: (vehicleId: string | undefined, t: Telemetry) => void;
  pushLog: (kind: string, text: string, severity?: number, vehicle?: string) => void;
  setTracks: (tracks: Track[], w: number, h: number) => void;
  // Merge a fresh `map_objects` batch: upsert each object with a new `seen`
  // stamp and drop any previously-known object now older than the TTL.
  setMapObjects: (objects: MapObject[], vehicle?: string) => void;
  // Drop expired detections (called on a timer by the map views so objects fade
  // out even when no new batch arrives, e.g. vision stopped).
  pruneMapObjects: () => void;
  // Merge a fresh `ally_overlay` batch with a short TTL, and prune stale markers
  // (used both on receipt and on the FPV overlay's rAF loop so the marker fades).
  setAllyMarkers: (items: AllyMarker[]) => void;
  pruneAllyMarkers: () => void;
  selectTrack: (id: number | null) => void;
  setVisionRunning: (v: boolean) => void;
  setLockActive: (v: boolean) => void;
  setVehicleId: (v: VehicleId | null) => void;
  setLowBatteryAlert: (v: GcsState["lowBatteryAlert"]) => void;
  // Upsert one vehicle's autotune snapshot (from the WS event or REST status).
  setAutotune: (a: AutotuneState) => void;
  // Update Ready-for-Flight state for a single vehicle. Called from ws.ts on
  // each telemetry frame and from the setter action after a REST toggle.
  setReadyForFlight: (vehicleId: string, ready: boolean, locked: boolean) => void;
  // Toggle a vehicle's gate via the REST API. Returns true on success. A 409
  // (mid-flight lock) surfaces as a rejection with the backend's message.
  setReadyForFlightRemote: (vehicleId: string, ready: boolean) => Promise<void>;
  setMission: (wps: google.maps.LatLngLiteral[]) => void;
  setMode: (m: Mode) => void;
  addSurveyVertex: (p: google.maps.LatLngLiteral) => void;
  clearSurvey: () => void;
  setSurveyCandidates: (c: SurveyCandidate[]) => void;
  setSurveyChoice: (i: number | null) => void;
  setSurveyPreview: (p: SurveyPreview | null) => void;
  setFleetRegion: (r: FleetRegion | null) => void;
  setFleetZones: (z: FleetZone[]) => void;
  setFleetPickCenter: (v: boolean | ((prev: boolean) => boolean)) => void;
  // Saved-region CRUD. addRegion assigns an id (or reuses an existing region
  // when the name collides), persists, and selects it. updateRegion patches a
  // saved region in place (live edits/drag). removeRegion deletes it + clears its
  // zones if selected. selectRegion loads a region into the live preview.
  addRegion: (r: Omit<FleetRegion, "id">) => FleetRegion;
  updateRegion: (id: string, patch: Partial<Omit<FleetRegion, "id">>) => void;
  removeRegion: (id: string) => void;
  selectRegion: (id: string | null) => void;
  addPoi: (name: string, lat: number, lng: number) => Poi;
  removePoi: (id: number) => void;
  clearPois: () => void;
  startReplay: (flight: FlightDetail) => void;
  startMissionReplay: (mission: MissionDetail) => void;
  stopReplay: () => void;
  setReplayPlaying: (playing: boolean) => void;
  setReplaySpeed: (speed: number) => void;
  setReplayTime: (t: number) => void; // seconds since flight start
  convHeard: (text: string) => void;
  convSaid: (text: string) => void;
  convTool: (name: string, args: Record<string, unknown>) => number;
  convToolResult: (id: number, ok: boolean) => void;
  convClear: () => void;
}

// Build one drone's replay track from a recorded flight. The backend path is
// [lat,lon,alt] with no per-sample timestamps, so we synthesize an even time
// spread across the flight's actual wall-clock window; actions/mode-changes
// carry absolute unix timestamps which the player maps onto the shared clock.
// `index` drives the fallback fleet color. Returns null if the path is unusable.
function buildReplayDrone(flight: FlightDetail, index: number): ReplayDrone | null {
  const path = (flight.path || []).filter(
    (p) => Array.isArray(p) && p.length >= 2 && p[0] != null && p[1] != null,
  ) as [number, number, number][];
  if (path.length === 0) return null;
  const startTs = flight.start_ts;
  const endTs = flight.end_ts ?? startTs + (flight.duration_s ?? Math.max(path.length, 1));
  const dur = Math.max(1, endTs - startTs);
  const n = path.length;
  const times =
    n > 1 ? path.map((_, i) => startTs + (dur * i) / (n - 1)) : path.map(() => startTs);
  return {
    flightId: flight.id,
    vehicleId: flight.vehicle_id,
    vehicleName: flight.vehicle_name,
    color: zoneColor(flight.vehicle_name || flight.vehicle_id, index),
    startTs,
    endTs,
    path,
    times,
    actions: flight.actions ?? [],
    modeTimeline: flight.mode_timeline ?? [],
  };
}

let logId = 0;
let convId = 0;
const INITIAL_POIS = loadPois();
// Seed the id counter past any persisted id so new POIs never collide.
let poiId = INITIAL_POIS.reduce((m, p) => Math.max(m, p.id), 0);
const INITIAL_REGIONS = loadRegions();
const CONV_CAP = 50;

export const useGcs = create<GcsState>((set, get) => ({
  socketOpen: false,
  vehicles: [],
  activeVehicle: null,
  telem: EMPTY,
  trail: [],
  fleetTelem: {},
  fleetTrail: {},
  home: null,
  mission: [],
  view3d:
    typeof location !== "undefined" &&
    new URLSearchParams(location.search).get("view") === "3d",
  log: [],
  tracks: [],
  mapObjects: {},
  allyMarkers: {},
  frameSize: { w: 1280, h: 720 },
  selectedTrack: null,
  visionRunning: false,
  lockActive: false,
  vehicleId: null,
  uiMode: "navigate",
  surveyPolygon: [],
  surveyCandidates: [],
  surveyChoice: null,
  surveyPreview: null,
  fleetRegion: null,
  fleetZones: [],
  fleetPickCenter: false,
  savedRegions: INITIAL_REGIONS,
  selectedRegionId: null,
  pois: INITIAL_POIS,
  followVehicle: false,
  conversation: [],
  reportOpen: false,
  lowBatteryAlert: null,
  replay: null,
  showSecondFeed: true,
  showMainFeed: true,
  boxSelecting: false,
  focusedFeed: null,
  autotune: {},
  readyForFlight: {},

  setSocketOpen: (v) => set({ socketOpen: v }),
  setShowSecondFeed: (v) => set({ showSecondFeed: v }),
  setShowMainFeed: (v) => set({ showMainFeed: v }),
  setBoxSelecting: (v) => set({ boxSelecting: v }),
  setFocusedFeed: (f) => set({ focusedFeed: f }),
  setReportOpen: (v) => set({ reportOpen: v }),
  setVehicles: (v) => set({ vehicles: v }),

  // Switching active drone: point the HUD/instruments at that drone's existing
  // per-vehicle telemetry/trail (kept in fleetTelem/fleetTrail so BOTH drones
  // stay on the map). Mission is per-active so it's cleared.
  setActiveVehicle: (id) =>
    set((s) => {
      if (s.activeVehicle === id) return s;
      const t = s.fleetTelem[id] ?? EMPTY;
      const trail = s.fleetTrail[id] ?? [];
      const home =
        t.home_lat != null && t.home_lon != null
          ? { lat: t.home_lat, lng: t.home_lon }
          : null;
      return { activeVehicle: id, telem: t, trail, home, mission: [] };
    }),

  toggleFollow: () => set((s) => ({ followVehicle: !s.followVehicle })),
  toggle3d: () => set((s) => ({ view3d: !s.view3d })),
  set2d: () => set({ view3d: false }),

  setTelemetry: (t) => {
    const prev = get().trail;
    let trail = prev;
    if (t.lat != null && t.lon != null) {
      const last = prev[prev.length - 1];
      const moved =
        !last ||
        Math.abs(last.lat - t.lat) > 1e-6 ||
        Math.abs(last.lng - t.lon) > 1e-6;
      if (moved) {
        trail = [...prev, { lat: t.lat, lng: t.lon }].slice(-600);
      }
    }
    // Home: prefer the vehicle's reported home; else lock to the first GPS fix.
    let home = get().home;
    if (t.home_lat != null && t.home_lon != null) {
      home = { lat: t.home_lat, lng: t.home_lon };
    } else if (!home && t.lat != null && t.lon != null) {
      home = { lat: t.lat, lng: t.lon };
    }
    set({ telem: t, trail, home });
  },

  // Per-vehicle telemetry: always update fleetTelem/fleetTrail for the sending
  // drone (so EVERY drone is on the map). If it's the active drone (or there's no
  // fleet yet), also drive the main HUD telem/trail/home.
  ingestTelemetry: (vehicleId, t) =>
    set((s) => {
      const id = vehicleId ?? s.activeVehicle ?? "_single";
      const prev = s.fleetTrail[id] ?? [];
      let vtrail = prev;
      if (t.lat != null && t.lon != null) {
        const last = prev[prev.length - 1];
        const moved =
          !last ||
          Math.abs(last.lat - t.lat) > 1e-6 ||
          Math.abs(last.lng - t.lon) > 1e-6;
        if (moved) vtrail = [...prev, { lat: t.lat, lng: t.lon }].slice(-400);
      }
      const fleetTelem = { ...s.fleetTelem, [id]: t };
      const fleetTrail = { ...s.fleetTrail, [id]: vtrail };
      const isActive = s.activeVehicle == null || id === s.activeVehicle || id === "_single";
      if (!isActive) return { fleetTelem, fleetTrail };
      let home = s.home;
      if (t.home_lat != null && t.home_lon != null) home = { lat: t.home_lat, lng: t.home_lon };
      else if (!home && t.lat != null && t.lon != null) home = { lat: t.lat, lng: t.lon };
      return { fleetTelem, fleetTrail, telem: t, trail: vtrail, home };
    }),

  pushLog: (kind, text, severity, vehicle) =>
    set((s) => {
      // Coalesce a run of identical lines (retry ACKs, fleet commands hitting both
      // drones) into ONE entry with a count, instead of flooding the console.
      // Only coalesce when the vehicle also matches, so two drones' identical
      // lines stay separate (and keep their own prefixes).
      const last = s.log[0];
      if (
        last &&
        last.kind === kind &&
        last.text === text &&
        last.vehicle === vehicle &&
        Date.now() - last.ts < 5000
      ) {
        return {
          log: [{ ...last, ts: Date.now(), count: (last.count ?? 1) + 1 }, ...s.log.slice(1)],
        };
      }
      return {
        log: [{ id: ++logId, ts: Date.now(), kind, text, severity, vehicle, count: 1 }, ...s.log].slice(0, 80),
      };
    }),

  setTracks: (tracks, w, h) =>
    set({ tracks, frameSize: { w, h } }),

  // Upsert the batch (stamp each with `seen=now`), then drop any object not in
  // this batch that has aged past the TTL. Objects still within TTL are kept so
  // a momentarily-undetected object doesn't flicker out between cycles.
  setMapObjects: (objects, vehicle) =>
    set((s) => {
      const now = performance.now();
      const next: Record<number, MapObjectLive> = {};
      // Carry forward still-fresh objects from the previous batch.
      for (const o of Object.values(s.mapObjects)) {
        if (now - o.seen < MAP_OBJECT_TTL_MS) next[o.id] = o;
      }
      // Upsert this batch on top (overwrites position/tracked/conf, refreshes seen).
      for (const o of objects) {
        next[o.id] = { ...o, vehicle, seen: now };
      }
      return { mapObjects: next };
    }),

  pruneMapObjects: () =>
    set((s) => {
      const now = performance.now();
      let changed = false;
      const next: Record<number, MapObjectLive> = {};
      for (const o of Object.values(s.mapObjects)) {
        if (now - o.seen < MAP_OBJECT_TTL_MS) next[o.id] = o;
        else changed = true;
      }
      return changed ? { mapObjects: next } : s;
    }),

  // Upsert the ally-marker batch (stamp each with seen=now), keeping any
  // still-fresh marker not in this batch so it doesn't flicker between cycles.
  setAllyMarkers: (items) =>
    set((s) => {
      const now = performance.now();
      const next: Record<string, AllyMarkerLive> = {};
      for (const m of Object.values(s.allyMarkers)) {
        if (now - m.seen < ALLY_MARKER_TTL_MS) next[m.id] = m;
      }
      for (const m of items) next[m.id] = { ...m, seen: now };
      return { allyMarkers: next };
    }),

  pruneAllyMarkers: () =>
    set((s) => {
      const now = performance.now();
      let changed = false;
      const next: Record<string, AllyMarkerLive> = {};
      for (const m of Object.values(s.allyMarkers)) {
        if (now - m.seen < ALLY_MARKER_TTL_MS) next[m.id] = m;
        else changed = true;
      }
      return changed ? { allyMarkers: next } : s;
    }),

  selectTrack: (id) => set({ selectedTrack: id }),
  // Vision stopping clears the ground-localized objects (no fresh batches will
  // arrive; otherwise they'd linger one TTL window before fading on their own).
  setVisionRunning: (v) => set(v ? { visionRunning: true } : { visionRunning: false, mapObjects: {} }),
  setLockActive: (v) => set({ lockActive: v }),
  setVehicleId: (v) => set({ vehicleId: v }),
  setLowBatteryAlert: (v) => set({ lowBatteryAlert: v }),
  setAutotune: (a) =>
    set((s) => ({ autotune: { ...s.autotune, [a.vehicle]: a } })),
  setReadyForFlight: (vehicleId, ready, locked) =>
    set((s) => {
      const prev = s.readyForFlight[vehicleId];
      if (prev && prev.ready === ready && prev.locked === locked) return s;
      return {
        readyForFlight: { ...s.readyForFlight, [vehicleId]: { ready, locked } },
      };
    }),
  setReadyForFlightRemote: async (vehicleId, ready) => {
    // Import lazily to avoid a circular init between store and api client.
    const { api } = await import("../lib/api");
    const res = await api.setReadyForFlight(vehicleId, ready);
    // Optimistic-safe: mirror the server-returned state (which may be locked).
    set((s) => ({
      readyForFlight: {
        ...s.readyForFlight,
        [vehicleId]: { ready: res.ready, locked: res.locked },
      },
    }));
  },
  setMission: (wps) => set({ mission: wps }),
  setMode: (m) => set({ uiMode: m }),
  addSurveyVertex: (p) =>
    set((s) => ({ surveyPolygon: [...s.surveyPolygon, p] })),
  clearSurvey: () => set({ surveyPolygon: [] }),
  setSurveyCandidates: (c) => set({ surveyCandidates: c, surveyChoice: null, surveyPreview: null }),
  setSurveyChoice: (i) => set({ surveyChoice: i }),
  setSurveyPreview: (p) => set({ surveyPreview: p }),
  setFleetRegion: (r) => set({ fleetRegion: r }),
  setFleetZones: (z) => set({ fleetZones: z }),
  setFleetPickCenter: (v) =>
    set((s) => ({ fleetPickCenter: typeof v === "function" ? v(s.fleetPickCenter) : v })),

  // Save a region. Re-using a name (case-insensitive) REPLACES that region in
  // place (keeping its id) so a name is always unique — like the POI rule. The
  // saved region is selected + mirrored into the live preview (`fleetRegion`).
  addRegion: (r) => {
    const clean = (r.name || "").trim();
    let saved!: FleetRegion;
    set((s) => {
      const existing = s.savedRegions.find(
        (x) => x.name.toLowerCase() === clean.toLowerCase(),
      );
      const id = existing?.id ?? newRegionId();
      saved = { ...r, name: clean || `Sector ${s.savedRegions.length + 1}`, id };
      const savedRegions = existing
        ? s.savedRegions.map((x) => (x.id === id ? saved : x))
        : [...s.savedRegions, saved];
      saveRegions(savedRegions);
      return { savedRegions, selectedRegionId: id, fleetRegion: saved };
    });
    return saved;
  },

  updateRegion: (id, patch) =>
    set((s) => {
      let next: FleetRegion | null = null;
      const savedRegions = s.savedRegions.map((x) => {
        if (x.id !== id) return x;
        next = { ...x, ...patch };
        return next;
      });
      if (!next) return s;
      saveRegions(savedRegions);
      // Keep the live preview in sync if this is the selected region.
      const fleetRegion = s.selectedRegionId === id ? next : s.fleetRegion;
      return { savedRegions, fleetRegion };
    }),

  removeRegion: (id) =>
    set((s) => {
      const savedRegions = s.savedRegions.filter((x) => x.id !== id);
      saveRegions(savedRegions);
      const wasSelected = s.selectedRegionId === id;
      return {
        savedRegions,
        selectedRegionId: wasSelected ? null : s.selectedRegionId,
        fleetRegion: wasSelected ? null : s.fleetRegion,
        fleetZones: wasSelected ? [] : s.fleetZones,
      };
    }),

  selectRegion: (id) =>
    set((s) => {
      if (id == null) return { selectedRegionId: null, fleetRegion: null, fleetZones: [] };
      const r = s.savedRegions.find((x) => x.id === id);
      if (!r) return s;
      // Selecting a different region drops the previous region's zone preview.
      const fleetZones = s.selectedRegionId === id ? s.fleetZones : [];
      return { selectedRegionId: id, fleetRegion: r, fleetZones };
    }),

  addPoi: (name, lat, lng) => {
    const clean = name.trim() || `POI ${poiId + 1}`;
    const poi = { id: ++poiId, name: clean, lat, lng };
    set((s) => {
      // No two markers may share a name: re-dropping an existing name REPLACES it
      // (i.e. moves that point) so the live POI store the agent resolves stays
      // unambiguous — "orbit B" can never hit a stale duplicate.
      const pois = [...s.pois.filter((p) => p.name.toLowerCase() !== clean.toLowerCase()), poi];
      savePois(pois);
      return { pois };
    });
    return poi;
  },
  removePoi: (id) =>
    set((s) => {
      const pois = s.pois.filter((p) => p.id !== id);
      savePois(pois);
      return { pois };
    }),
  clearPois: () =>
    set(() => {
      savePois([]);
      return { pois: [] };
    }),

  // Load a single recorded flight into replay mode (the per-flight Replay
  // button). Builds a one-drone mission whose shared clock spans this flight.
  startReplay: (flight) => {
    const drone = buildReplayDrone(flight, 0);
    if (!drone) return;
    const startTs = drone.startTs;
    const endTs = drone.endTs;
    set({
      replay: {
        missionId: null,
        flightId: drone.flightId,
        vehicleName: drone.vehicleName,
        startTs,
        endTs,
        duration: Math.max(1, endTs - startTs),
        drones: [drone],
        path: drone.path,
        times: drone.times,
        actions: drone.actions,
        modeTimeline: drone.modeTimeline,
        playing: true,
        speed: 1,
        t: 0,
      },
    });
  },

  // Load a WHOLE mission into replay: every member drone animates on ONE shared
  // clock keyed on absolute timestamps spanning mission t0..t1. A drone that
  // launched/landed earlier just parks its ghost at its endpoints outside its own
  // window (poseAtTime clamps to the path ends). Each drone keeps its own fleet
  // color (Overwatch teal, Outrider amber, else by index).
  startMissionReplay: (mission) => {
    const drones = mission.flights
      .map((f, i) => buildReplayDrone(f, i))
      .filter((d): d is ReplayDrone => d != null);
    if (drones.length === 0) return;
    // Shared clock origin/end: prefer the mission window, fall back to the span
    // across drones if the backend window is missing.
    const startTs = mission.t0 ?? Math.min(...drones.map((d) => d.startTs));
    const endTs = mission.t1 ?? Math.max(...drones.map((d) => d.endTs));
    const primary = drones[0];
    set({
      replay: {
        missionId: mission.mission_id,
        flightId: primary.flightId,
        vehicleName: drones.map((d) => d.vehicleName).join(" + "),
        startTs,
        endTs,
        duration: Math.max(1, endTs - startTs),
        drones,
        path: primary.path,
        times: primary.times,
        actions: primary.actions,
        modeTimeline: primary.modeTimeline,
        playing: true,
        speed: 1,
        t: 0,
      },
    });
  },
  stopReplay: () => set({ replay: null }),
  setReplayPlaying: (playing) =>
    set((s) => (s.replay ? { replay: { ...s.replay, playing } } : s)),
  setReplaySpeed: (speed) =>
    set((s) => (s.replay ? { replay: { ...s.replay, speed } } : s)),
  setReplayTime: (t) =>
    set((s) =>
      s.replay
        ? { replay: { ...s.replay, t: Math.max(0, Math.min(s.replay.duration, t)) } }
        : s,
    ),

  // Streaming upsert: heard/said fire repeatedly with the growing text for the
  // current utterance. If the last entry is the same streaming role we update it
  // in place; otherwise we open a fresh turn. A tool/assistant entry "closes" the
  // user turn (and vice-versa), so the next partial starts a new bubble.
  convHeard: (text) =>
    set((s) => {
      if (!text) return s;
      const log = s.conversation;
      const last = log[log.length - 1];
      if (last && last.role === "user") {
        // Transcripts stream as incremental fragments — concatenate, don't replace.
        const next = log.slice();
        next[next.length - 1] = { ...last, text: (last.text ?? "") + text, ts: Date.now() };
        return { conversation: next };
      }
      const entry: ConvEntry = { id: ++convId, role: "user", text, ts: Date.now() };
      return { conversation: [...log, entry].slice(-CONV_CAP) };
    }),

  convSaid: (text) =>
    set((s) => {
      if (!text) return s;
      const log = s.conversation;
      const last = log[log.length - 1];
      if (last && last.role === "assistant") {
        // Transcripts stream as incremental fragments — concatenate, don't replace.
        const next = log.slice();
        next[next.length - 1] = { ...last, text: (last.text ?? "") + text, ts: Date.now() };
        return { conversation: next };
      }
      const entry: ConvEntry = { id: ++convId, role: "assistant", text, ts: Date.now() };
      return { conversation: [...log, entry].slice(-CONV_CAP) };
    }),

  convTool: (name, args) => {
    const id = ++convId;
    const entry: ConvEntry = { id, role: "tool", tool: { name, args }, ts: Date.now() };
    set((s) => ({ conversation: [...s.conversation, entry].slice(-CONV_CAP) }));
    return id;
  },

  convToolResult: (id, ok) =>
    set((s) => ({
      conversation: s.conversation.map((e) =>
        e.id === id && e.tool ? { ...e, tool: { ...e.tool, ok } } : e,
      ),
    })),

  convClear: () => set({ conversation: [] }),
}));
