import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { useGcs, EMPTY } from "../src/store/useGcs";
import type { Telemetry, VehicleInfo } from "../src/lib/types";

// ── api mock: capture every command call + its vehicle arg so we can assert that
// fleet/emergency controls target the RIGHT drone(s). All resolve ok. The shared
// `calls` array is created via vi.hoisted so the hoisted vi.mock factory can use it.
const { calls } = vi.hoisted(() => ({ calls: [] as { fn: string; arg: unknown }[] }));
vi.mock("../src/lib/api", () => {
  const r = (fn: string) => (arg?: unknown) => {
    calls.push({ fn, arg });
    return Promise.resolve({ ok: true });
  };
  return {
    MJPEG_URL: "/api/vision/stream.mjpg",
    api: {
      arm: r("arm"),
      disarm: r("disarm"),
      takeoff: (alt: number, v?: string) => {
        calls.push({ fn: "takeoff", arg: { alt, v } });
        return Promise.resolve({ ok: true });
      },
      land: r("land"),
      rtl: r("rtl"),
      hold: r("hold"),
      brake: r("brake"),
      forceDisarm: r("forceDisarm"),
    },
  };
});

function veh(id: string, connected: boolean, active = false): VehicleInfo {
  return { id, name: id.toUpperCase(), kind: "quadcopter", connected, active };
}
function telem(p: Partial<Telemetry> = {}): Telemetry {
  return { ...EMPTY, connected: true, ...p };
}

import StatusBar from "../src/components/StatusBar";
import CommandBar from "../src/components/CommandBar";
import LowBatteryBanner from "../src/components/LowBatteryBanner";
import ErrorBoundary from "../src/components/ErrorBoundary";

beforeEach(() => {
  calls.length = 0;
  useGcs.setState({
    vehicles: [],
    activeVehicle: null,
    telem: EMPTY,
    fleetTelem: {},
    lowBatteryAlert: null,
    socketOpen: true,
    uiMode: "navigate",
  });
});
afterEach(() => cleanup());

// ── TRUTHFUL TELEMETRY ───────────────────────────────────────────────────────
describe("StatusBar telemetry truthfulness (no other-drone data)", () => {
  it("a non-active drone with NO telemetry shows NO LINK, not the active drone's data", () => {
    // Active = overwatch (live, GUIDED, 80%). Outrider is in the roster but has
    // not sent telemetry yet (fleetTelem has no entry for it).
    useGcs.setState({
      vehicles: [veh("overwatch", true, true), veh("outrider", false)],
      activeVehicle: "overwatch",
      telem: telem({ mode: "GUIDED", battery_pct: 80, connected: true }),
      fleetTelem: { overwatch: telem({ mode: "GUIDED", battery_pct: 80 }) },
    });
    render(<StatusBar />);
    // Overwatch's mode is shown for Overwatch.
    expect(screen.getByText("GUIDED")).toBeInTheDocument();
    // Outrider, having no telemetry, must read NO LINK — and must NOT borrow
    // Overwatch's "GUIDED"/80% (there must be exactly ONE "GUIDED").
    expect(screen.getByText("NO LINK")).toBeInTheDocument();
    expect(screen.getAllByText("GUIDED")).toHaveLength(1);
    expect(screen.getAllByText("80%")).toHaveLength(1);
  });

  it("each drone shows its OWN telemetry when both are present", () => {
    useGcs.setState({
      vehicles: [veh("overwatch", true, true), veh("outrider", true)],
      activeVehicle: "overwatch",
      telem: telem({ mode: "GUIDED", battery_pct: 80 }),
      fleetTelem: {
        overwatch: telem({ mode: "GUIDED", battery_pct: 80 }),
        outrider: telem({ mode: "HOLD", battery_pct: 55 }),
      },
    });
    render(<StatusBar />);
    expect(screen.getByText("GUIDED")).toBeInTheDocument();
    expect(screen.getByText("HOLD")).toBeInTheDocument();
    expect(screen.getByText("80%")).toBeInTheDocument();
    expect(screen.getByText("55%")).toBeInTheDocument();
  });
});

// ── EMERGENCY CONTROLS TARGET THE RIGHT DRONE(S) ─────────────────────────────
describe("CommandBar fleet/emergency routing", () => {
  it("HOLD/BRAKE/RTL/LAND command ALL drones (not just active)", () => {
    useGcs.setState({
      vehicles: [veh("overwatch", true, true), veh("outrider", true)],
      activeVehicle: "overwatch",
      fleetTelem: { overwatch: telem(), outrider: telem() },
    });
    render(<CommandBar />);
    fireEvent.click(screen.getByText("HOLD"));
    fireEvent.click(screen.getByText("BRAKE"));
    fireEvent.click(screen.getByText("RTL"));
    fireEvent.click(screen.getByText("LAND"));
    // Each safety command must pass "all" so the backend hits every connected drone.
    for (const fn of ["hold", "brake", "rtl", "land"]) {
      const c = calls.find((x) => x.fn === fn);
      expect(c, `${fn} was not called`).toBeTruthy();
      expect(c!.arg, `${fn} must target "all"`).toBe("all");
    }
  });

  it("ARM/DISARM command ALL drones", () => {
    useGcs.setState({
      vehicles: [veh("overwatch", true, true), veh("outrider", true)],
      activeVehicle: "overwatch",
      fleetTelem: { overwatch: telem({ armed: false }), outrider: telem({ armed: false }) },
    });
    render(<CommandBar />);
    fireEvent.click(screen.getByText("ARM"));
    const c = calls.find((x) => x.fn === "arm");
    expect(c).toBeTruthy();
    expect(c!.arg).toBe("all");
  });

  it("DISARM is reachable whenever ANY connected drone is armed", () => {
    // Only the NON-active drone is armed — the toggle must still show DISARM so
    // the operator can always disarm a flying drone.
    useGcs.setState({
      vehicles: [veh("overwatch", true, true), veh("outrider", true)],
      activeVehicle: "overwatch",
      fleetTelem: {
        overwatch: telem({ armed: false }),
        outrider: telem({ armed: true }),
      },
    });
    render(<CommandBar />);
    expect(screen.getByText("DISARM")).toBeInTheDocument();
    fireEvent.click(screen.getByText("DISARM"));
    const c = calls.find((x) => x.fn === "disarm");
    expect(c!.arg).toBe("all");
  });
});

// ── re-takeoff hint: RTL/LAND modes reject a fresh takeoff (no auto-reset) ──────
describe("CommandBar re-takeoff hint (set HOLD first)", () => {
  for (const mode of ["AUTO.RTL", "AUTO.LAND", "RTL", "LAND"]) {
    it(`shows the "set HOLD before re-takeoff" hint when active drone is in ${mode}`, () => {
      useGcs.setState({
        vehicles: [veh("overwatch", true, true)],
        activeVehicle: "overwatch",
        fleetTelem: { overwatch: telem({ mode }) },
      });
      render(<CommandBar />);
      expect(screen.getByText(/set HOLD before re-takeoff/i)).toBeInTheDocument();
      // It's a HINT/affordance — a "Set HOLD" button, NOT an auto-sent command.
      expect(calls.find((c) => c.fn === "hold")).toBeFalsy();
    });
  }

  it("does NOT show the hint in a normal flying mode (HOLD)", () => {
    useGcs.setState({
      vehicles: [veh("overwatch", true, true)],
      activeVehicle: "overwatch",
      fleetTelem: { overwatch: telem({ mode: "HOLD" }) },
    });
    render(<CommandBar />);
    expect(screen.queryByText(/set HOLD before re-takeoff/i)).toBeNull();
  });

  it('the "Set HOLD" affordance targets the ACTIVE drone (only on click — never auto-sent)', () => {
    useGcs.setState({
      vehicles: [veh("overwatch", true, true), veh("outrider", true)],
      activeVehicle: "overwatch",
      fleetTelem: { overwatch: telem({ mode: "AUTO.RTL" }), outrider: telem({ mode: "HOLD" }) },
    });
    render(<CommandBar />);
    // Nothing sent on render.
    expect(calls.find((c) => c.fn === "hold")).toBeFalsy();
    // The "Set HOLD" affordance is the button with the explanatory title (the
    // hint span also contains the words "set HOLD", so match by the button title).
    fireEvent.click(screen.getByTitle(/fresh takeoff is accepted/i));
    const c = calls.find((x) => x.fn === "hold");
    expect(c).toBeTruthy();
    expect(c!.arg).toBe("overwatch"); // the active drone, not "all"
  });
});

// ── force-disarm: confirm-gated emergency, targets the ACTIVE drone (never "all") ─
describe("CommandBar force-disarm (confirm-gated)", () => {
  it("does NOT force-disarm until confirmed; confirm targets the ACTIVE drone only", () => {
    useGcs.setState({
      vehicles: [veh("overwatch", true, true), veh("outrider", true)],
      activeVehicle: "overwatch",
      fleetTelem: { overwatch: telem({ armed: true }), outrider: telem({ armed: true }) },
    });
    render(<CommandBar />);
    // Tapping FORCE opens the confirm — it must NOT disarm yet.
    fireEvent.click(screen.getByText("FORCE"));
    expect(calls.find((c) => c.fn === "forceDisarm")).toBeFalsy();
    expect(screen.getByText(/Force-disarm OVERWATCH\?/i)).toBeInTheDocument();
    // Confirming force-disarms the ACTIVE drone ONLY (never "all" — must not cut a
    // healthy flying drone's motors).
    fireEvent.click(screen.getByText("FORCE-DISARM"));
    const c = calls.find((x) => x.fn === "forceDisarm");
    expect(c, "force-disarm not called on confirm").toBeTruthy();
    expect(c!.arg).toBe("overwatch");
  });

  it("Cancel dismisses the confirm WITHOUT force-disarming", () => {
    useGcs.setState({
      vehicles: [veh("overwatch", true, true)],
      activeVehicle: "overwatch",
      fleetTelem: { overwatch: telem({ armed: true }) },
    });
    render(<CommandBar />);
    fireEvent.click(screen.getByText("FORCE"));
    fireEvent.click(screen.getByText("Cancel"));
    expect(calls.find((c) => c.fn === "forceDisarm")).toBeFalsy();
  });
});

// ── ERRORBOUNDARY CONTAINMENT (a panel throw must not blank the cockpit) ─────
function Boom(): JSX.Element {
  throw new Error("render exploded");
}
describe("ErrorBoundary containment", () => {
  it("a default (contained) boundary shows an inline card, NOT a full-screen cover", () => {
    // Suppress React's error logging noise for this expected throw.
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const { container } = render(
      <ErrorBoundary label="HUD">
        <Boom />
      </ErrorBoundary>,
    );
    // The failed-panel card is shown...
    expect(screen.getByText("HUD failed")).toBeInTheDocument();
    // ...but it is NOT an `absolute inset-0` full-viewport cover (which would
    // blank the cockpit + hide the emergency controls). The card's root element
    // must not carry the fullscreen positioning classes.
    const root = container.firstElementChild as HTMLElement;
    expect(root.className).not.toContain("absolute");
    expect(root.className).not.toContain("inset-0");
    spy.mockRestore();
  });

  it("a contained boundary failure leaves SIBLING controls (e.g. CommandBar) mounted + reachable", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    useGcs.setState({
      vehicles: [veh("overwatch", true, true), veh("outrider", true)],
      activeVehicle: "overwatch",
      fleetTelem: { overwatch: telem(), outrider: telem() },
    });
    render(
      <div>
        <ErrorBoundary label="HUD">
          <Boom />
        </ErrorBoundary>
        <ErrorBoundary label="Command bar">
          <CommandBar />
        </ErrorBoundary>
      </div>,
    );
    // The HUD failed, but the emergency LAND control is still present + works.
    expect(screen.getByText("HUD failed")).toBeInTheDocument();
    fireEvent.click(screen.getByText("LAND"));
    expect(calls.find((x) => x.fn === "land")?.arg).toBe("all");
    spy.mockRestore();
  });
});

// ── LOW-BATTERY BANNER RTLs THE ACTUALLY-LOW DRONE ───────────────────────────
describe("LowBatteryBanner targets the low drone", () => {
  it("RTL NOW commands the drone named in the alert, not the active one", () => {
    // Active drone is overwatch; the LOW one is outrider.
    useGcs.setState({
      vehicles: [veh("overwatch", true, true), veh("outrider", true)],
      activeVehicle: "overwatch",
      lowBatteryAlert: { vehicle: "outrider", name: "OUTRIDER", battery_pct: 18, threshold: 20 },
    });
    render(<LowBatteryBanner />);
    expect(screen.getByText(/LOW BATTERY/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("RTL NOW"));
    const c = calls.find((x) => x.fn === "rtl");
    expect(c, "RTL was not called").toBeTruthy();
    // MUST RTL outrider (the low drone), NOT the active overwatch.
    expect(c!.arg).toBe("outrider");
  });
});
