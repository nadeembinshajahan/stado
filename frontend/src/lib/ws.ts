import { useGcs } from "../store/useGcs";
import type { ServerMessage } from "./types";

const SEVERITY: Record<number, string> = {
  0: "EMERGENCY", 1: "ALERT", 2: "CRITICAL", 3: "ERROR",
  4: "WARNING", 5: "NOTICE", 6: "INFO", 7: "DEBUG",
};

// MAVLink command id → human name, so the console reads "Takeoff: ACCEPTED"
// instead of "cmd 22: ACCEPTED". Unmapped ids fall back to "cmd <n>".
const CMD_NAMES: Record<number, string> = {
  20: "RTL", 21: "Land", 22: "Takeoff", 24: "Takeoff",
  34: "Orbit", 84: "Takeoff", 85: "Land",
  176: "Set Mode", 178: "Set Speed", 179: "Set Home",
  192: "Goto", 186: "Pause", 300: "Mission Start", 400: "Arm/Disarm",
};
const cmdName = (id: number) => CMD_NAMES[id] ?? `cmd ${id}`;

// Module-level singleton so React StrictMode's dev double-mount (and HMR) reuse
// ONE socket instead of churning connect/disconnect through the Vite proxy.
let ws: WebSocket | null = null;
let retry: ReturnType<typeof setTimeout> | null = null;
let closeTimer: ReturnType<typeof setTimeout> | null = null;
let refs = 0;
let wantOpen = false;
let lastMissionSeq: number | null = null; // dedupe MISSION_CURRENT (PX4 sends it ~1Hz)

function route(msg: ServerMessage) {
  const st = useGcs.getState();

  // Telemetry from EVERY drone is kept (so the map can show both); but the
  // HUD/console "chatter" events (mode/armed/ack/statustext/mission/...) are
  // filtered to the active drone so they don't mix. So: drop non-active messages
  // EXCEPT telemetry (which ingestTelemetry routes per-vehicle).
  if (
    msg.type !== "telemetry" &&
    msg.type !== "low_battery" && // a low battery on ANY drone must alert, active or not
    msg.type !== "autotune" && // per-drone tune progress is tracked for both drones
    "vehicle" in msg &&
    msg.vehicle != null &&
    st.activeVehicle != null &&
    msg.vehicle !== st.activeVehicle
  ) {
    return;
  }

  switch (msg.type) {
    case "telemetry":
      st.ingestTelemetry(msg.vehicle, msg.data);
      // Ready-for-Flight gate state is echoed on every telemetry frame so the UI
      // reacts instantly to auto-lock transitions (armed+airborne) without polling.
      if (msg.vehicle != null && typeof msg.ready_for_flight === "boolean") {
        st.setReadyForFlight(msg.vehicle, msg.ready_for_flight,
                             Boolean(msg.ready_for_flight_locked));
      }
      break;
    case "link":
      st.pushLog("link", msg.connected ? "Vehicle link up" : "Vehicle link lost", undefined, msg.vehicle);
      break;
    case "mode":
      st.pushLog("mode", `Mode → ${msg.mode}`, undefined, msg.vehicle);
      break;
    case "armed":
      st.pushLog("armed", msg.armed ? "ARMED" : "DISARMED", undefined, msg.vehicle);
      break;
    case "ack":
      st.pushLog("ack", `${cmdName(msg.command)}: ${msg.result}`, undefined, msg.vehicle);
      break;
    case "statustext":
      st.pushLog("vehicle", msg.text, msg.severity, msg.vehicle);
      break;
    case "mission_current":
      // Only log when the active waypoint actually changes (not every 1Hz tick).
      if (msg.seq !== lastMissionSeq) {
        lastMissionSeq = msg.seq;
        st.pushLog("mission", `Heading to waypoint ${msg.seq + 1}`, undefined, msg.vehicle);
      }
      break;
    case "waypoint_reached":
      st.pushLog("mission", `Reached waypoint ${msg.seq}`, undefined, msg.vehicle);
      break;
    case "tracks":
      st.setTracks(msg.tracks, msg.frame_w, msg.frame_h);
      break;
    case "map_objects":
      st.setMapObjects(msg.objects, msg.vehicle);
      break;
    case "ally_overlay":
      // AR ally marker(s) projected into the active camera frame (Outrider in
      // Overwatch's feed). Stored with a short TTL so a stale marker fades.
      st.setAllyMarkers(msg.items);
      break;
    case "follow":
      st.setLockActive(true);
      break;
    case "target_lost":
      st.setLockActive(false);
      st.setVehicleId(null);
      st.pushLog("track", `Lock lost${msg.label ? ` (${msg.label})` : ""} — re-acquiring`);
      break;
    case "vision":
      st.setVisionRunning(msg.status === "running");
      if (msg.status !== "running") st.setVehicleId(null);
      st.pushLog("vision", `Vision ${msg.status}`);
      break;
    case "vehicle_id":
      st.setVehicleId({ plate: msg.plate, info: msg.info });
      break;
    case "mission":
      st.setMission(msg.waypoints.map(([lat, lng]) => ({ lat, lng })));
      st.pushLog("mission", `Mission uploaded — ${msg.waypoints.length} waypoints`, undefined, msg.vehicle);
      break;
    case "survey_perimeters":
      // Voice asked for survey options — show them on the satellite map and
      // make the picker visible (survey mode, 2D so polygons render).
      st.setSurveyCandidates(msg.perimeters);
      if (st.view3d) st.set2d();
      st.setMode("survey");
      st.pushLog(
        "vision",
        `${msg.perimeters.length} survey area(s)${msg.around ? ` around ${msg.around}` : ""} on map — tap one to pick`,
      );
      break;
    case "fleet_zones": {
      // Voice planned/confirmed a coordinated FLEET/region survey (e.g. "survey
      // Sector 1"). Render the divided zones + each drone's lawnmower path in its
      // fleet color via the same fleetZones path the panel uses. flying:false =
      // a PLAN preview awaiting confirm; flying:true = the confirmed survey.
      const flying = !!msg.flying;
      st.setFleetZones(
        (msg.zones || []).map((z) => ({
          vehicle: z.vehicle,
          name: z.name,
          polygon: z.polygon,
          path: z.path,
          flying,
        })),
      );
      if (msg.zones && msg.zones.length) {
        const names = msg.zones.map((z) => z.name).join(" + ");
        st.pushLog(
          "mission",
          flying
            ? `Fleet survey "${msg.label ?? ""}" confirmed — ${names} flying`
            : `Fleet survey "${msg.label ?? ""}" planned — ${names} — confirm to fly`,
        );
      }
      break;
    }
    case "survey_selected":
      st.setSurveyChoice(msg.choice);
      break;
    case "survey_pending":
      // Voice planned a survey: highlight the candidate, show the lawnmower
      // preview + the "Confirm & fly" gate (the same gate the tap path uses).
      st.setSurveyChoice(msg.choice);
      st.setSurveyPreview({
        label: msg.label,
        choice: msg.choice,
        polygon: msg.polygon,
        path: msg.path,
        waypoints: msg.waypoints,
      });
      st.pushLog("mission", `Survey "${msg.label}" planned (${msg.waypoints} wp) — confirm to fly`);
      break;
    case "survey_committed":
      // The staged survey is flying (confirmed by voice or the map button). Drop
      // the preview; the uploaded "mission" event draws the real flown path.
      st.setSurveyPreview(null);
      st.pushLog("cmd", `Survey "${msg.label}" confirmed — flying`);
      break;
    case "survey_cancelled":
      st.setSurveyPreview(null);
      st.pushLog("mission", "Survey plan cancelled");
      break;
    case "flight_complete": {
      const f = msg.flight;
      const dur = f.duration_s != null ? `${Math.round(f.duration_s / 60)}m` : "—";
      st.pushLog(
        "flight",
        `Flight complete — ${f.vehicle_name} (${dur}, ${(f.distance_m / 1000).toFixed(2)} km) — open Reports`,
      );
      break;
    }
    case "autotune": {
      // PX4 autotune progress/terminal state for a vehicle (driven by the ACK, so
      // it works on Outrider too). Store the snapshot for the panel; log terminal
      // transitions so the console reflects the outcome even with the panel closed.
      const { statustext: _st, type: _t, ...snap } = msg;
      st.setAutotune(snap);
      const labelFor = (v: string) =>
        useGcs.getState().vehicles.find((x) => x.id === v)?.name ?? v;
      if (msg.state === "RUNNING" && msg.progress === 0) {
        st.pushLog("autotune", `Autotune started on ${labelFor(msg.vehicle)}`, undefined, msg.vehicle);
      } else if (msg.state === "COMPLETE") {
        st.pushLog("autotune", `Autotune complete — gains apply on disarm`, undefined, msg.vehicle);
      } else if (msg.state === "FAILED") {
        st.pushLog("autotune", `Autotune FAILED: ${msg.reason ?? "unknown"}`, 3, msg.vehicle);
      } else if (msg.state === "CANCELLED") {
        st.pushLog("autotune", `Autotune cancelled`, undefined, msg.vehicle);
      }
      break;
    }
    case "low_battery":
      // Smart-RTL: raise the alert banner + a console EMERGENCY line. STADO also
      // speaks it and asks to confirm RTL (handled in the voice session).
      st.setLowBatteryAlert({
        vehicle: msg.vehicle,
        name: msg.name,
        battery_pct: msg.battery_pct,
        threshold: msg.threshold,
      });
      st.pushLog("vehicle", `LOW BATTERY — ${msg.name} at ${Math.round(msg.battery_pct)}% (RTL?)`, 0, msg.vehicle);
      break;
  }
}

function open() {
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
  ws = new WebSocket(url);

  ws.onopen = () => useGcs.getState().setSocketOpen(true);
  ws.onmessage = (ev) => {
    let msg: ServerMessage;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    route(msg);
  };
  ws.onclose = () => {
    useGcs.getState().setSocketOpen(false);
    ws = null;
    if (wantOpen) retry = setTimeout(open, 1500);
  };
  ws.onerror = () => ws?.close();
}

/** Connect (or attach to the existing) backend WebSocket. Returns a detach fn.
 *  Ref-counted + a short grace period so StrictMode mount→unmount→mount doesn't
 *  actually drop the socket. */
export function connectWs(): () => void {
  refs++;
  wantOpen = true;
  if (closeTimer) {
    clearTimeout(closeTimer);
    closeTimer = null;
  }
  if (!ws) open();

  return () => {
    refs = Math.max(0, refs - 1);
    if (refs > 0) return;
    // Last consumer gone — wait a beat; a StrictMode remount will cancel this.
    closeTimer = setTimeout(() => {
      wantOpen = false;
      if (retry) clearTimeout(retry);
      ws?.close();
      ws = null;
    }, 300);
  };
}

export { SEVERITY };
