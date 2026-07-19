import { describe, it, expect, beforeEach, vi } from "vitest";
import { useGcs, MAP_OBJECT_TTL_MS } from "../src/store/useGcs";
import type { Telemetry } from "../src/lib/types";

// A full telemetry packet with overridable fields.
function telem(p: Partial<Telemetry> = {}): Telemetry {
  return {
    connected: true,
    armed: false,
    mode: "HOLD",
    lat: null,
    lon: null,
    alt_msl: null,
    alt_rel: null,
    heading: null,
    roll: null,
    pitch: null,
    yaw: null,
    groundspeed: null,
    airspeed: null,
    climb: null,
    throttle: null,
    battery_pct: null,
    battery_voltage: null,
    battery_current: null,
    gps_fix: null,
    satellites: null,
    vx: null,
    vy: null,
    vz: null,
    home_lat: null,
    home_lon: null,
    last_heartbeat: 0,
    ...p,
  };
}

// Snapshot the pristine initial state and restore it before each test, since the
// zustand store is a module singleton shared across cases.
const INITIAL = useGcs.getState();
beforeEach(() => {
  localStorage.clear();
  useGcs.setState(
    {
      ...INITIAL,
      vehicles: [],
      activeVehicle: null,
      fleetTelem: {},
      fleetTrail: {},
      telem: INITIAL.telem,
      trail: [],
      home: null,
      mission: [],
      log: [],
      mapObjects: {},
      allyMarkers: {},
      savedRegions: [],
      selectedRegionId: null,
      pois: [],
      fleetZones: [],
      conversation: [],
      replay: null,
    },
    true,
  );
});

describe("ingestTelemetry", () => {
  it("keeps per-vehicle telemetry for BOTH drones in fleetTelem", () => {
    const s = useGcs.getState();
    s.setActiveVehicle("overwatch");
    s.ingestTelemetry("overwatch", telem({ lat: 1, lon: 1, mode: "AUTO" }));
    s.ingestTelemetry("outrider", telem({ lat: 2, lon: 2, mode: "HOLD" }));
    const st = useGcs.getState();
    expect(st.fleetTelem.overwatch.mode).toBe("AUTO");
    expect(st.fleetTelem.outrider.mode).toBe("HOLD");
    // Active drone drives the main HUD telem; the non-active one does NOT.
    expect(st.telem.mode).toBe("AUTO");
  });

  it("does not let a non-active drone overwrite the active HUD telem", () => {
    const s = useGcs.getState();
    s.setActiveVehicle("overwatch");
    s.ingestTelemetry("overwatch", telem({ lat: 1, lon: 1, mode: "AUTO" }));
    s.ingestTelemetry("outrider", telem({ lat: 9, lon: 9, mode: "RTL" }));
    expect(useGcs.getState().telem.mode).toBe("AUTO"); // still the active one
  });

  it("builds a per-vehicle trail and dedupes near-identical points", () => {
    const s = useGcs.getState();
    s.ingestTelemetry("a", telem({ lat: 1, lon: 1 }));
    s.ingestTelemetry("a", telem({ lat: 1, lon: 1 })); // same point → not appended
    s.ingestTelemetry("a", telem({ lat: 1.001, lon: 1.001 })); // moved → appended
    expect(useGcs.getState().fleetTrail.a).toHaveLength(2);
  });
});

describe("setActiveVehicle", () => {
  it("points the HUD telem/trail/home at the newly active drone's fleet data", () => {
    const s = useGcs.getState();
    s.ingestTelemetry("overwatch", telem({ lat: 1, lon: 1, home_lat: 1, home_lon: 1 }));
    s.ingestTelemetry("outrider", telem({ lat: 2, lon: 2, home_lat: 2, home_lon: 2 }));
    s.setActiveVehicle("outrider");
    const st = useGcs.getState();
    expect(st.activeVehicle).toBe("outrider");
    expect(st.telem.lat).toBe(2);
    expect(st.home).toEqual({ lat: 2, lng: 2 });
    expect(st.mission).toEqual([]); // mission is per-active → cleared
  });

  it("is a no-op when re-selecting the same vehicle", () => {
    const s = useGcs.getState();
    s.setActiveVehicle("overwatch");
    const before = useGcs.getState();
    s.setActiveVehicle("overwatch");
    expect(useGcs.getState()).toBe(before); // identical reference (returned `s`)
  });
});

describe("map objects TTL", () => {
  it("upserts a batch and prunes objects older than the TTL", () => {
    const nowSpy = vi.spyOn(performance, "now");
    nowSpy.mockReturnValue(0);
    const s = useGcs.getState();
    s.setMapObjects([
      { id: 1, label: "car", lat: 0, lon: 0, conf: 0.9, tracked: false },
      { id: 2, label: "person", lat: 0, lon: 0, conf: 0.8, tracked: true },
    ]);
    expect(Object.keys(useGcs.getState().mapObjects)).toHaveLength(2);

    // Advance past the TTL and push a batch that only re-sees object 1.
    nowSpy.mockReturnValue(MAP_OBJECT_TTL_MS + 1);
    s.setMapObjects([{ id: 1, label: "car", lat: 1, lon: 1, conf: 0.95, tracked: false }]);
    const objs = useGcs.getState().mapObjects;
    expect(objs[1].lat).toBe(1); // refreshed
    expect(objs[2]).toBeUndefined(); // aged out
  });

  it("pruneMapObjects drops stale objects and is identity when nothing changed", () => {
    const nowSpy = vi.spyOn(performance, "now");
    nowSpy.mockReturnValue(0);
    const s = useGcs.getState();
    s.setMapObjects([{ id: 1, label: "car", lat: 0, lon: 0, conf: 1, tracked: false }]);
    const before = useGcs.getState().mapObjects;
    s.pruneMapObjects(); // within TTL → unchanged reference
    expect(useGcs.getState().mapObjects).toBe(before);
    nowSpy.mockReturnValue(MAP_OBJECT_TTL_MS + 1);
    s.pruneMapObjects();
    expect(useGcs.getState().mapObjects).toEqual({});
  });
});

describe("setVisionRunning", () => {
  it("clears map objects when vision stops", () => {
    const s = useGcs.getState();
    vi.spyOn(performance, "now").mockReturnValue(0);
    s.setMapObjects([{ id: 1, label: "car", lat: 0, lon: 0, conf: 1, tracked: false }]);
    s.setVisionRunning(false);
    expect(useGcs.getState().mapObjects).toEqual({});
    expect(useGcs.getState().visionRunning).toBe(false);
  });
});

describe("pushLog coalescing", () => {
  it("collapses identical consecutive lines (same vehicle) into a count", () => {
    const s = useGcs.getState();
    s.pushLog("ack", "Takeoff: ACCEPTED", undefined, "overwatch");
    s.pushLog("ack", "Takeoff: ACCEPTED", undefined, "overwatch");
    s.pushLog("ack", "Takeoff: ACCEPTED", undefined, "overwatch");
    const log = useGcs.getState().log;
    expect(log).toHaveLength(1);
    expect(log[0].count).toBe(3);
  });

  it("does NOT coalesce identical text from different vehicles", () => {
    const s = useGcs.getState();
    s.pushLog("ack", "Takeoff: ACCEPTED", undefined, "overwatch");
    s.pushLog("ack", "Takeoff: ACCEPTED", undefined, "outrider");
    expect(useGcs.getState().log).toHaveLength(2);
  });
});

describe("addPoi", () => {
  it("re-dropping the same name REPLACES (moves) the point", () => {
    const s = useGcs.getState();
    s.addPoi("Base", 1, 1);
    s.addPoi("base", 2, 2); // case-insensitive collision
    const pois = useGcs.getState().pois;
    expect(pois).toHaveLength(1);
    expect(pois[0].lat).toBe(2);
  });

  it("auto-names an unnamed POI", () => {
    const s = useGcs.getState();
    const p = s.addPoi("", 1, 1);
    expect(p.name).toMatch(/^POI /);
  });
});

describe("savedRegions CRUD", () => {
  it("addRegion assigns an id, selects it, and mirrors to fleetRegion", () => {
    const s = useGcs.getState();
    const r = s.addRegion({ name: "Sector 1", center: [1, 1], width_m: 100, height_m: 100, heading_deg: 0 });
    const st = useGcs.getState();
    expect(r.id).toBeTruthy();
    expect(st.selectedRegionId).toBe(r.id);
    expect(st.fleetRegion?.id).toBe(r.id);
    expect(st.savedRegions).toHaveLength(1);
  });

  it("addRegion with a colliding name UPDATES in place (keeps the id)", () => {
    const s = useGcs.getState();
    const a = s.addRegion({ name: "Sector 1", center: [1, 1], width_m: 100, height_m: 100, heading_deg: 0 });
    const b = s.addRegion({ name: "sector 1", center: [2, 2], width_m: 200, height_m: 200, heading_deg: 0 });
    expect(b.id).toBe(a.id);
    expect(useGcs.getState().savedRegions).toHaveLength(1);
    expect(useGcs.getState().savedRegions[0].center).toEqual([2, 2]);
  });

  it("removeRegion clears selection + zones if the removed region was selected", () => {
    const s = useGcs.getState();
    const r = s.addRegion({ name: "Z", center: [0, 0], width_m: 10, height_m: 10, heading_deg: 0 });
    s.setFleetZones([{ vehicle: "a", name: "A", polygon: [[0, 0], [0, 1], [1, 1]] }]);
    s.removeRegion(r.id!);
    const st = useGcs.getState();
    expect(st.savedRegions).toHaveLength(0);
    expect(st.selectedRegionId).toBeNull();
    expect(st.fleetZones).toEqual([]);
  });
});

describe("replay clamp", () => {
  it("setReplayTime clamps to [0, duration]", () => {
    useGcs.setState({
      replay: {
        missionId: null,
        flightId: "f1",
        vehicleName: "X",
        startTs: 0,
        endTs: 100,
        duration: 100,
        drones: [],
        path: [],
        times: [],
        actions: [],
        modeTimeline: [],
        playing: false,
        speed: 1,
        t: 0,
      },
    });
    const s = useGcs.getState();
    s.setReplayTime(-50);
    expect(useGcs.getState().replay!.t).toBe(0);
    s.setReplayTime(9999);
    expect(useGcs.getState().replay!.t).toBe(100);
  });

  it("replay setters are no-ops when not in replay mode", () => {
    const s = useGcs.getState();
    s.setReplayPlaying(true);
    s.setReplayTime(5);
    expect(useGcs.getState().replay).toBeNull();
  });
});

describe("conversation streaming", () => {
  it("convHeard concatenates consecutive user fragments", () => {
    const s = useGcs.getState();
    s.convHeard("take ");
    s.convHeard("off now");
    const conv = useGcs.getState().conversation;
    expect(conv).toHaveLength(1);
    expect(conv[0].text).toBe("take off now");
  });

  it("a tool entry closes the user turn so the next heard opens a new bubble", () => {
    const s = useGcs.getState();
    s.convHeard("orbit");
    s.convTool("orbit", { lat: 1 });
    s.convHeard("the tower");
    const conv = useGcs.getState().conversation;
    expect(conv).toHaveLength(3);
    expect(conv[0].role).toBe("user");
    expect(conv[1].role).toBe("tool");
    expect(conv[2].role).toBe("user");
  });

  it("convToolResult sets the ok flag on the matching tool entry", () => {
    const s = useGcs.getState();
    const id = s.convTool("land", {});
    s.convToolResult(id, false);
    const tool = useGcs.getState().conversation.find((e) => e.id === id);
    expect(tool?.tool?.ok).toBe(false);
  });
});
