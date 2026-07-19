export interface Telemetry {
  connected: boolean;
  armed: boolean;
  mode: string | null;
  lat: number | null;
  lon: number | null;
  alt_msl: number | null;
  alt_rel: number | null;
  heading: number | null;
  roll: number | null;
  pitch: number | null;
  yaw: number | null;
  groundspeed: number | null;
  airspeed: number | null;
  climb: number | null;
  throttle: number | null;
  battery_pct: number | null;
  battery_voltage: number | null;
  battery_current: number | null;
  gps_fix: number | null;
  satellites: number | null;
  vx: number | null;
  vy: number | null;
  vz: number | null;
  home_lat: number | null;
  home_lon: number | null;
  last_heartbeat: number;
}

export interface LogEvent {
  id: number;
  ts: number;
  kind: string;
  text: string;
  severity?: number;
  count?: number; // consecutive identical lines collapse into one with a count
  // Vehicle this line is about, when the source knows it (e.g. a per-drone
  // command/ack/statustext or a low-battery alert). Undefined → no prefix.
  // Stored as the vehicle id; the console maps it to a name + signature color.
  vehicle?: string;
}

export interface VehicleInfo {
  id: string;
  name: string;
  kind: string;
  connected: boolean;
  active: boolean;
  // Per-vehicle command-transport capabilities, surfaced by GET /api/vehicles so
  // the cockpit UI can grey-out actions a vehicle would REFUSE (preflight H1/H2)
  // instead of letting the operator find out only after the command errors. A
  // DDS-bridge vehicle (Outrider) has supports_offboard=false (no GCS-side
  // OFFBOARD setpoints → no turn / GCS-follow) and supports_autotune=false (cmd
  // 212 is UNSUPPORTED over DDS); a full MAVLink vehicle (Overwatch) has all true.
  // Optional so a roster from an older backend (no flags) still typechecks; the
  // UI treats a missing flag as capable (true) so nothing is wrongly disabled.
  supports_offboard?: boolean;
  supports_missions?: boolean;
  supports_autotune?: boolean;
  // Ready-for-Flight gate state — initial hydration comes from GET /api/vehicles,
  // then telemetry frames keep it live. Optional so older backends still typecheck.
  ready_for_flight?: boolean;
  ready_for_flight_locked?: boolean;
}

// Anonymized vehicle-ID record for the currently locked car (owner is masked
// upstream by the backend). Mirrors the card the backend burns into the TRACK
// MJPEG so the FPV WebRTC overlay can render the same info.
export interface VehicleId {
  plate: string;
  info: {
    plate: string;
    valid: boolean;
    state: string;
    rto_code: string;
    maker_model?: string;
    fuel?: string;
    reg_year?: string;
    vehicle_class?: string;
    owner?: string;
    source?: string;
  };
}

// One vehicle's PX4 autotune state — the shape the controller's snapshot returns
// (REST /autotune/status) AND the payload of the WS `autotune` event. Driven by
// PX4's COMMAND_ACK progress (works on Outrider too); STATUSTEXT is supplementary
// (the `statustexts` feed stays empty on the DDS-bridge transport).
export interface AutotuneState {
  vehicle: string;
  state: "IDLE" | "RUNNING" | "COMPLETE" | "FAILED" | "CANCELLED";
  progress: number; // 0..100, from the ACK
  axis: string | null; // last axis from STATUSTEXT (roll/pitch/yaw/done), if any
  reason: string | null; // failure/completion detail for the UI
  statustexts: { severity: number; text: string; ts: number }[];
  running: boolean;
}

export type ServerMessage =
  | {
      type: "telemetry";
      data: Telemetry;
      vehicle?: string;
      max_altitude_m?: number | null;
      // Per-vehicle Ready-for-Flight gate — echoed on every telemetry frame so
      // the UI reacts instantly to auto-lock transitions.
      ready_for_flight?: boolean;
      ready_for_flight_locked?: boolean;
    }
  | { type: "link"; connected: boolean; vehicle?: string }
  | { type: "mode"; mode: string; vehicle?: string }
  | { type: "armed"; armed: boolean; vehicle?: string }
  | { type: "ack"; command: number; result: string; vehicle?: string }
  | { type: "statustext"; severity: number; text: string; vehicle?: string }
  | { type: "mission_current"; seq: number; vehicle?: string }
  | { type: "waypoint_reached"; seq: number; vehicle?: string }
  | { type: "tracks"; tracks: Track[]; frame_w: number; frame_h: number }
  | { type: "map_objects"; vehicle?: string; objects: MapObject[] }
  | { type: "follow"; setpoint: { vx: number; vy: number; vz: number; yaw_rate: number }; target: number }
  | { type: "target_lost"; label?: string }
  | { type: "vision"; status: string; source?: string }
  | { type: "vehicle_id"; plate: string; info: VehicleId["info"] }
  | { type: "mission"; waypoints: [number, number][]; commands?: number[]; vehicle?: string }
  | { type: "survey_perimeters"; perimeters: SurveyCandidate[]; center?: [number, number]; around?: string | null }
  | { type: "fleet_zones"; label?: string; flying?: boolean; zones: FleetZoneMsg[] }
  | { type: "survey_selected"; choice: number }
  | { type: "survey_pending"; label: string; choice: number; polygon: [number, number][]; path: [number, number][]; waypoints: number }
  | { type: "survey_committed"; label: string }
  | { type: "survey_cancelled" }
  | { type: "flight_complete"; flight: FlightSummary }
  | { type: "ally_overlay"; items: AllyMarker[] }
  | ({ type: "autotune"; statustext?: { severity: number; text: string; ts: number } } & AutotuneState)
  | { type: "low_battery"; vehicle: string; name: string; battery_pct: number; threshold: number };

export interface Coord {
  lat: number | null;
  lon: number | null;
}

export interface FlightSummary {
  id: string;
  vehicle_id: string;
  vehicle_name: string;
  start_ts: number; // seconds (unix)
  end_ts: number | null;
  duration_s: number | null;
  max_alt_m: number;
  distance_m: number;
  max_speed_ms: number;
  battery_start_pct: number | null;
  battery_min_pct: number | null;
  battery_used_pct: number | null;
  takeoff: Coord | null;
  landing: Coord | null;
  event_count: number;
  action_count?: number;
}

// One timestamped agent (STADO) action — a voice tool call executed on the drone.
export interface FlightAction {
  ts: number;
  name: string;
  label: string;
  ok: boolean;
}

export interface FlightDetail extends FlightSummary {
  path: [number, number, number][]; // [lat, lon, alt_rel]
  mode_timeline: { mode: string; ts: number }[];
  events: { ts: number; severity: number | null; text: string; kind: string }[];
  actions?: FlightAction[];
  summary?: string | null;
}

// A MISSION = the set of per-vehicle flights that flew TOGETHER (their active
// windows overlap in time). Summary form for the report list.
export interface MissionSummary {
  mission_id: string;
  t0: number; // unix seconds — earliest member start
  t1: number; // unix seconds — latest member end
  duration_s: number;
  vehicles: string[]; // vehicle ids, first-seen order
  names: string[]; // vehicle names, first-seen order
  flight_ids: string[];
  flight_count: number;
}

// Full mission detail: the same window + the FULL FlightDetail of every member
// flight, so the report can render side-by-side and replay all drones together.
export interface MissionDetail {
  mission_id: string;
  t0: number;
  t1: number;
  duration_s: number;
  vehicles: string[];
  names: string[];
  flight_ids: string[];
  flights: FlightDetail[];
}

// One drone's track within a multi-drone replay. Path/times/actions/modes are
// absolute-unix-second aligned; `color` is the drone's fleet color.
export interface ReplayDrone {
  flightId: string;
  vehicleId: string;
  vehicleName: string;
  color: string;
  startTs: number; // unix seconds of this drone's first sample
  endTs: number; // unix seconds of this drone's last sample
  path: [number, number, number][]; // [lat, lon, alt_rel]
  times: number[]; // per-sample unix-second timeline, 1:1 with path
  actions: FlightAction[];
  modeTimeline: { mode: string; ts: number }[];
}

// Active mission-replay session. The clock is keyed on ABSOLUTE timestamps
// spanning t0..t1 across ALL member drones; `t` is seconds since `startTs` (=t0)
// so a drone that launched/landed earlier simply parks its ghost at its
// endpoints outside its own window. The map views read this to animate one ghost
// + flown-path PER drone in its fleet color.
export interface ReplayState {
  missionId: string | null; // null for a legacy single-flight replay
  // Back-compat fields the player/map read for the *primary* drone (drones[0]).
  flightId: string;
  vehicleName: string;
  startTs: number; // unix seconds — mission t0 (shared clock origin)
  endTs: number; // unix seconds — mission t1
  duration: number; // seconds (endTs - startTs) — the shared timeline length
  // The drones replayed on the shared clock (1 entry for single-flight replay).
  drones: ReplayDrone[];
  // Primary drone's path/times/actions/modes — kept for the existing single
  // -flight map code paths (they mirror drones[0]).
  path: [number, number, number][];
  times: number[];
  actions: FlightAction[];
  modeTimeline: { mode: string; ts: number }[];
  playing: boolean;
  speed: number; // playback multiplier
  t: number; // current playback time, seconds since startTs (0..duration)
}

export interface SurveyCandidate {
  label: string;
  description: string;
  polygon: [number, number][]; // [lat, lon]
}

// One per-drone zone in a coordinated FLEET/region survey, as pushed by the
// backend `fleet_zones` event (the voice "survey Sector 1" plan/preview path).
// Mirrors the store's FleetZone; `path` is this drone's planned lawnmower grid.
export interface FleetZoneMsg {
  vehicle: string;
  name: string;
  polygon: [number, number][]; // [lat, lon]
  path?: [number, number][]; // [lat, lon] lawnmower turn points
  altitude?: number;
}

// A PLANNED-but-not-yet-flown survey: the tidied polygon + the lawnmower flight
// path, awaiting the operator's "Confirm & fly". Drawn as a preview on the map.
export interface SurveyPreview {
  label: string;
  choice: number | null; // candidate index this came from (if any)
  polygon: [number, number][]; // [lat, lon] — the cleaned ring
  path: [number, number][]; // [lat, lon] — lawnmower turn points (open polyline)
  waypoints: number; // full takeoff→grid→RTL mission waypoint count
}

export interface Track {
  id: number;
  label: string;
  conf: number;
  // normalized [0..1] box: x,y top-left, w,h
  x: number;
  y: number;
  w: number;
  h: number;
}

// A detected object the drone camera localized to the GROUND (estimated lat/lon
// from the box bottom-center projected onto a flat ground plane). Published by
// the backend `map_objects` event at ~3-4 Hz and rendered as a per-class icon on
// the 2D + 3D maps. `tracked` flags the locked follow target (republished each
// cycle so it animates in real time).
export type MapObjectClass =
  | "car"
  | "person"
  | "bicycle"
  | "motorcycle"
  | "truck"
  | "bus";

export interface MapObject {
  id: number;
  label: MapObjectClass;
  lat: number;
  lon: number;
  conf: number;
  tracked: boolean;
}

// Store-side detected object: a MapObject plus the wall-clock time it was last
// seen, so a per-object TTL can fade/remove stale detections. Keyed by id.
export interface MapObjectLive extends MapObject {
  vehicle?: string;
  seen: number; // performance.now() ms when last received
}

// An augmented-reality "ally" marker: another fleet vehicle (Outrider) projected
// into the observer's (Overwatch) camera frame from its GPS. (u, v) are
// normalized image coords in [0..1]; `range_m` is the slant range; `in_view` is
// false when it falls outside the FOV; `behind` when it's behind the camera.
// Published by the backend `ally_overlay` event at ~6 Hz.
export interface AllyMarker {
  id: string;
  label: string;
  u: number;
  v: number;
  range_m: number;
  in_view: boolean;
  behind: boolean;
}

// Store-side ally marker: an AllyMarker plus the time it was last seen, for a
// short TTL so a stale marker fades when the backend stops publishing.
export interface AllyMarkerLive extends AllyMarker {
  seen: number; // performance.now() ms when last received
}
