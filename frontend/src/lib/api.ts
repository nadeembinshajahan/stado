import type {
  AutotuneState, FlightDetail, FlightSummary, MissionDetail, MissionSummary, VehicleInfo,
} from "./types";

async function post(path: string, body?: unknown) {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => res.statusText);
    throw new Error(`${path} failed: ${detail}`);
  }
  return res.json();
}

export const api = {
  // vehicle: a drone id, "all"/"both", or undefined (active). arm/disarm pass it
  // as a query param; takeoff in the body.
  arm: (vehicle?: string) => post(`/command/arm${vehicle ? `?vehicle=${vehicle}` : ""}`),
  disarm: (vehicle?: string) => post(`/command/disarm${vehicle ? `?vehicle=${vehicle}` : ""}`),
  // FORCE disarm — sends PX4's "force" magic (param2=21196), which BYPASSES the
  // "Disarming denied: not landed" check that rejects a normal disarm when the land
  // detector wrongly believes the drone is airborne (the 2026-05-26 stuck-armed
  // incident: home/EKF ~2.8 m off → PX4 thought it took off → normal disarm denied →
  // battery pull). EMERGENCY use, confirm-gated in the UI. Targets ONE drone (never
  // "all" — must not cut a healthy flying drone's motors).
  forceDisarm: (vehicle?: string) =>
    post(`/command/disarm?force=true${vehicle ? `&vehicle=${vehicle}` : ""}`),
  takeoff: (altitude = 10, vehicle?: string) => post("/command/takeoff", { altitude, vehicle }),
  // hold/brake/rtl/land take an optional `vehicle` ("all"/"both" or a drone id)
  // in the body — backend applies "all" to every connected drone (matches arm).
  land: (vehicle?: string) => post("/command/land", { vehicle }),
  rtl: (vehicle?: string) => post("/command/rtl", { vehicle }),
  hold: (vehicle?: string) => post("/command/hold", { vehicle }),
  brake: (vehicle?: string) => post("/command/brake", { vehicle }),
  mode: (name: string) => post("/command/mode", { name }),
  goto: (lat: number, lon: number, alt = 20) =>
    post("/command/goto", { lat, lon, alt }),
  orbit: (
    lat: number,
    lon: number,
    alt = 20,
    radius = 20,
    velocity = 3,
    clockwise = true,
  ) => post("/command/orbit", { lat, lon, alt, radius, velocity, clockwise }),
  setHome: (lat: number, lon: number) =>
    post("/command/set_home", { lat, lon }),
  setSpeed: (speed: number) => post("/command/speed", { speed }),
  setPois: (pois: { id: number; name: string; lat: number; lng: number }[]) =>
    post("/pois", { pois }),
  // Sync the operator's named SEARCH AREAS so the voice agent knows them by name
  // (e.g. "survey Sector 1"). Mirrors setPois.
  setRegions: (
    regions: { id?: string; name: string; center: [number, number]; width_m: number; height_m: number; heading_deg: number }[],
  ) => post("/regions", { regions }),
  survey: (
    polygon: [number, number][],
    opts: { altitude?: number; line_spacing_m?: number; heading_deg?: number; execute?: boolean } = {},
  ) => post("/survey", { polygon, ...opts }),
  // PLAN-ONLY preview: tidy the (operator-edited) polygon + return the lawnmower
  // path. No upload/fly. Used by the plan-then-confirm survey flow.
  surveyPlan: (
    polygon: [number, number][],
    opts: { altitude?: number; line_spacing_m?: number; heading_deg?: number } = {},
  ) =>
    post("/survey/plan", { polygon, ...opts }) as Promise<{
      ok: boolean;
      polygon: [number, number][];
      path: [number, number][];
      grid: number;
      waypoints: number;
    }>,
  // Stage a planned survey for confirm-then-fly (shares the voice agent's pending
  // slot, so a spoken "confirm" flies what the map staged and vice versa).
  surveyStage: (
    polygon: [number, number][],
    opts: { label?: string; altitude?: number; vehicle?: string } = {},
  ) => post("/survey/stage", { polygon, ...opts }),
  // CONFIRM & FLY the staged survey: upload + start the mission.
  surveyCommit: () => post("/survey/commit"),
  // Discard a staged-but-unconfirmed survey.
  surveyCancel: () => post("/survey/cancel"),
  // Split a region into one zone per drone (coordinated fleet survey). Backend
  // returns per-vehicle polygon assignments + waypoints.
  surveyCoordinated: (body: {
    name: string;
    center: [number, number]; // [lat, lon]
    width_m: number;
    height_m: number;
    heading_deg?: number;
    vehicles?: string[];
    altitude?: number;
    line_spacing_m?: number;
    min_separation_m?: number;
  }) =>
    post("/survey/coordinated", body) as Promise<{
      name: string;
      assignments: {
        vehicle: string;
        name: string;
        polygon: [number, number][]; // [lat, lon]
        altitude?: number;
        waypoints?: [number, number][];
      }[];
    }>,
  surveyPerimeters: (
    body:
      | { image_b64: string; bounds: { north: number; south: number; east: number; west: number }; max_regions?: number }
      | { lat: number; lon: number; zoom?: number; size?: number; max_regions?: number },
  ) => post("/survey/perimeters", body) as Promise<{
    perimeters: { label: string; description: string; polygon: [number, number][] }[];
    bounds: { north: number; south: number; east: number; west: number };
  }>,
  missionStart: () => post("/mission/start"),
  missionClear: () => post("/mission/clear"),
  config: () => fetch("/api/config").then((r) => r.json()),

  vehicles: () => fetch("/api/vehicles").then((r) => r.json()) as Promise<VehicleInfo[]>,
  setActiveVehicle: (id: string) => post("/vehicle/active", { id }),

  // Ready-for-Flight per-vehicle safety gate. GET returns every vehicle's state;
  // PUT toggles one and returns the new state (or 409 if the drone is armed+
  // airborne — the store's setter surfaces the backend message).
  readyForFlight: () =>
    fetch("/api/safety/ready_for_flight").then((r) => r.json()) as
      Promise<{ vehicles: { vehicle: string; ready: boolean; locked: boolean }[] }>,
  setReadyForFlight: async (vehicle: string, ready: boolean) => {
    const res = await fetch("/api/safety/ready_for_flight", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vehicle, ready }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const msg = body?.detail?.message
        ?? body?.detail?.error
        ?? `ready_for_flight ${ready ? "enable" : "disable"} failed (HTTP ${res.status})`;
      throw new Error(msg);
    }
    return res.json() as Promise<{ ok: true; vehicle: string; ready: boolean; locked: boolean }>;
  },

  flights: () => fetch("/api/flights").then((r) => r.json()) as Promise<FlightSummary[]>,
  flight: (id: string) =>
    fetch(`/api/flights/${id}`).then((r) => {
      if (!r.ok) throw new Error(`flight ${id} not found`);
      return r.json();
    }) as Promise<FlightDetail>,

  // Missions = groups of per-vehicle flights that flew together (overlapping
  // time windows). The list is for the report index; the detail carries every
  // member flight's FULL detail for side-by-side reports + multi-drone replay.
  missions: () => fetch("/api/missions").then((r) => r.json()) as Promise<MissionSummary[]>,
  mission: (id: string) =>
    fetch(`/api/missions/${id}`).then((r) => {
      if (!r.ok) throw new Error(`mission ${id} not found`);
      return r.json();
    }) as Promise<MissionDetail>,
  flightSummary: (id: string) =>
    fetch(`/api/flights/${id}/summary`).then((r) => {
      if (!r.ok) throw new Error(`summary ${id} unavailable`);
      return r.json();
    }) as Promise<{ summary: string; cached: boolean }>,

  vision: {
    start: (source?: string) => post("/vision/start", source ? { source } : {}),
    stop: () => post("/vision/stop"),
    status: () => fetch("/api/vision/status").then((r) => r.json()),
    select: (track_id: number | null) => post("/vision/select", { track_id }),
    follow: (enable: boolean) => post("/vision/follow", { enable }),
    acquire: (description: string, backend?: string) =>
      post("/vision/acquire", { description, backend }),
    // Click-to-track: seed the CSRT tracker DIRECTLY from a drag-drawn box,
    // normalized 0..1 of the current frame ((x,y) top-left, (w,h) size). No
    // VLM text needed — the backend locks onto that ROI like a successful
    // acquire and then reports the tracked box in `tracks`/`follow`.
    seedBox: (box: { x: number; y: number; w: number; h: number; label?: string }) =>
      post("/vision/seed_box", box),
    orbitTarget: () => post("/vision/orbit"),
  },

  // Outrider ONBOARD tracker (on the Jetson, UDP :8771). A box drawn on
  // Outrider's feed locks the tracker ONBOARD (lowest latency); the lock reticle
  // is burned into the RGB stream. Box is normalized 0..1 of Outrider's frame.
  outrider: {
    track: (box: { x: number; y: number; w: number; h: number }) =>
      post("/outrider/track", box),
    clearTrack: () => post("/outrider/track/clear"),
    // Enable/disable the ONBOARD follow controller (UDP :8771 FOLLOW 1/0) — flies
    // Outrider toward the locked target.
    follow: (enable: boolean) => post("/outrider/follow", { enable }),
  },

  // PX4 multicopter AUTOTUNE (an in-flight rate-controller tune). start REQUIRES
  // confirm:true (it is an in-flight oscillation maneuver) — a bare call comes
  // back 409 with the safety preconditions. status reports the live state +
  // progress (also pushed over the WS `autotune` event); cancel disables the tune.
  autotune: {
    start: (vehicle?: string) =>
      post("/autotune/start", { vehicle, confirm: true }) as Promise<{
        ok: boolean;
        vehicle: string;
        state: string;
        progress: number;
        running: boolean;
        note?: string;
      }>,
    status: (vehicle?: string) =>
      fetch(`/api/autotune/status${vehicle ? `?vehicle=${vehicle}` : ""}`).then((r) =>
        r.json(),
      ) as Promise<AutotuneState | { vehicles: AutotuneState[] }>,
    cancel: (vehicle?: string) =>
      post("/autotune/cancel", { vehicle }) as Promise<{
        ok: boolean;
        vehicle: string;
        state: string;
        running: boolean;
      }>,
  },

  // Local MP4 recording of a go2rtc restream (ffmpeg -c copy on the Mac). start
  // → {ok, file}; stop → {ok, file, recording:false}; status → {recording:
  // {<stream>: {file, since_unix}}} for live captures.
  record: {
    start: (stream: string) =>
      post("/record/start", { stream }) as Promise<{ ok: boolean; file?: string; already?: boolean }>,
    stop: (stream: string) =>
      post("/record/stop", { stream }) as Promise<{ ok: boolean; file?: string; recording: boolean }>,
    status: () =>
      fetch("/api/record/status").then((r) => r.json()) as Promise<{
        recording: Record<string, { file: string; since_unix: number }>;
      }>,
  },
};

export const MJPEG_URL = "/api/vision/stream.mjpg";
