import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useGcs } from "../src/store/useGcs";
import { connectWs } from "../src/lib/ws";

// A minimal fake WebSocket that captures handlers so the test can drive
// onopen/onmessage/onclose directly (no real socket, no backend).
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  sent: string[] = [];
  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
  // helpers
  fireOpen() {
    this.readyState = 1;
    this.onopen?.();
  }
  fire(msg: unknown) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
}

let detach: (() => void) | null = null;

beforeEach(() => {
  // ws.ts keeps ONE module-level socket (ref-counted with a 300ms close grace).
  // Fake timers let us flush that grace in afterEach so each test starts clean.
  vi.useFakeTimers();
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
  // Reset only the slices the router touches.
  useGcs.setState({
    socketOpen: false,
    activeVehicle: "overwatch",
    fleetTelem: {},
    fleetTrail: {},
    log: [],
    lowBatteryAlert: null,
    visionRunning: false,
    lockActive: false,
    vehicleId: null,
  });
});

afterEach(() => {
  detach?.();
  detach = null;
  // Flush the 300ms close grace + any reconnect timer so the module socket is
  // fully torn down before the next test creates a fresh one.
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

function sock() {
  detach = connectWs();
  // Use the most-recent instance: a prior test's socket may linger one tick.
  const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
  ws.fireOpen();
  return ws;
}

describe("connectWs lifecycle", () => {
  it("sets socketOpen on open", () => {
    sock();
    expect(useGcs.getState().socketOpen).toBe(true);
  });
});

describe("route() vehicle filtering", () => {
  it("keeps telemetry from a NON-active drone (so both show on the map)", () => {
    const ws = sock();
    ws.fire({ type: "telemetry", vehicle: "outrider", data: { lat: 5, lon: 5, connected: true } });
    expect(useGcs.getState().fleetTelem.outrider).toBeTruthy();
    expect(useGcs.getState().fleetTelem.outrider.lat).toBe(5);
  });

  it("DROPS non-telemetry chatter from a non-active drone", () => {
    const ws = sock();
    ws.fire({ type: "mode", mode: "RTL", vehicle: "outrider" }); // non-active → ignored
    expect(useGcs.getState().log).toHaveLength(0);
  });

  it("logs chatter from the ACTIVE drone", () => {
    const ws = sock();
    ws.fire({ type: "mode", mode: "AUTO", vehicle: "overwatch" });
    const log = useGcs.getState().log;
    expect(log).toHaveLength(1);
    expect(log[0].text).toContain("AUTO");
  });

  it("raises a low_battery alert for ANY drone, even a non-active one", () => {
    const ws = sock();
    ws.fire({
      type: "low_battery",
      vehicle: "outrider",
      name: "Outrider",
      battery_pct: 12,
      threshold: 20,
    });
    expect(useGcs.getState().lowBatteryAlert?.vehicle).toBe("outrider");
  });

  it("decodes a command ACK into a human-readable line", () => {
    const ws = sock();
    ws.fire({ type: "ack", command: 22, result: "ACCEPTED", vehicle: "overwatch" });
    expect(useGcs.getState().log[0].text).toBe("Takeoff: ACCEPTED");
  });

  it("vision stop clears the locked vehicle id", () => {
    useGcs.setState({ vehicleId: { plate: "X", info: { plate: "X", valid: true, state: "", rto_code: "" } } });
    const ws = sock();
    ws.fire({ type: "vision", status: "stopped" });
    expect(useGcs.getState().visionRunning).toBe(false);
    expect(useGcs.getState().vehicleId).toBeNull();
  });

  it("ignores malformed JSON without throwing", () => {
    const ws = sock();
    expect(() => ws.onmessage?.({ data: "{not json" })).not.toThrow();
  });
});
