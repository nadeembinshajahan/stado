import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, act, fireEvent, waitFor } from "@testing-library/react";
import { useGcs } from "../src/store/useGcs";
import { connectWs } from "../src/lib/ws";
import { api } from "../src/lib/api";
import type { VehicleInfo } from "../src/lib/types";

// ── api client: the autotune endpoints ───────────────────────────────────────
describe("api.autotune", () => {
  const mockFetch = vi.fn();
  beforeEach(() => {
    vi.stubGlobal("fetch", mockFetch);
    mockFetch.mockReset();
  });
  afterEach(() => vi.unstubAllGlobals());

  function ok(json: unknown) {
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(json),
      text: () => Promise.resolve(JSON.stringify(json)),
    });
  }

  it("start ALWAYS sends confirm:true (never fires a bare call)", async () => {
    mockFetch.mockReturnValueOnce(ok({ ok: true, vehicle: "overwatch", state: "RUNNING" }));
    await api.autotune.start("overwatch");
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/autotune/start");
    expect(JSON.parse(opts.body)).toEqual({ vehicle: "overwatch", confirm: true });
  });

  it("status GETs ?vehicle= for a named drone", async () => {
    mockFetch.mockReturnValueOnce(ok({ vehicle: "outrider", state: "IDLE" }));
    await api.autotune.status("outrider");
    expect(mockFetch).toHaveBeenCalledWith("/api/autotune/status?vehicle=outrider");
  });

  it("cancel POSTs the vehicle in the body", async () => {
    mockFetch.mockReturnValueOnce(ok({ ok: true, vehicle: "overwatch", state: "CANCELLED" }));
    await api.autotune.cancel("overwatch");
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/autotune/cancel");
    expect(JSON.parse(opts.body)).toEqual({ vehicle: "overwatch" });
  });
});

// ── WS routing: the autotune event ────────────────────────────────────────────
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static OPEN = 1;
  url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }
  send() {}
  close() {
    this.readyState = 3;
    this.onclose?.();
  }
  fireOpen() {
    this.readyState = 1;
    this.onopen?.();
  }
  fire(msg: unknown) {
    this.onmessage?.({ data: JSON.stringify(msg) });
  }
}

describe("ws route() autotune event", () => {
  let detach: (() => void) | null = null;

  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    vi.stubGlobal("WebSocket", FakeWebSocket as unknown as typeof WebSocket);
    useGcs.setState({
      socketOpen: false,
      activeVehicle: "overwatch",
      vehicles: [
        { id: "overwatch", name: "Overwatch", kind: "hex", connected: true, active: true },
        { id: "outrider", name: "Outrider", kind: "quad", connected: true, active: false },
      ],
      autotune: {},
      log: [],
    });
  });
  afterEach(() => {
    detach?.();
    detach = null;
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function sock() {
    detach = connectWs();
    const ws = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
    ws.fireOpen();
    return ws;
  }

  it("stores a RUNNING snapshot for the active drone", () => {
    const ws = sock();
    ws.fire({ type: "autotune", vehicle: "overwatch", state: "RUNNING", progress: 35,
              axis: "roll", reason: null, statustexts: [], running: true });
    expect(useGcs.getState().autotune.overwatch.progress).toBe(35);
    expect(useGcs.getState().autotune.overwatch.axis).toBe("roll");
  });

  it("tracks a tune on a NON-active drone (not dropped by the vehicle filter)", () => {
    const ws = sock();
    ws.fire({ type: "autotune", vehicle: "outrider", state: "RUNNING", progress: 50,
              axis: null, reason: null, statustexts: [], running: true });
    // Outrider is NOT the active drone, but its tune must still be tracked.
    expect(useGcs.getState().autotune.outrider?.progress).toBe(50);
  });

  it("logs a FAILED tune with its reason", () => {
    const ws = sock();
    ws.fire({ type: "autotune", vehicle: "overwatch", state: "FAILED", progress: 20,
              axis: null, reason: "no progress for 60s", statustexts: [], running: false });
    const log = useGcs.getState().log;
    expect(log.some((l) => l.text.includes("FAILED") && l.text.includes("no progress"))).toBe(true);
  });
});

// ── component: the AutotunePanel confirm-then-run flow ────────────────────────
// framer-motion's AnimatePresence renders synchronously enough for jsdom; no mock.
import AutotunePanel from "../src/components/AutotunePanel";

function veh(id: string, name: string, connected: boolean, active = false): VehicleInfo {
  return { id, name, kind: "quad", connected, active };
}

describe("AutotunePanel", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        // status GET on open → IDLE; start/cancel POST → ok.
        if (typeof url === "string" && url.includes("/autotune/status")) {
          return Promise.resolve({
            ok: true,
            json: () => Promise.resolve({ vehicle: "overwatch", state: "IDLE", progress: 0,
                                          axis: null, reason: null, statustexts: [], running: false }),
            text: () => Promise.resolve("{}"),
          });
        }
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ ok: true, vehicle: "overwatch", state: "RUNNING" }),
          text: () => Promise.resolve("{}"),
        });
      }),
    );
    useGcs.setState({
      vehicles: [veh("overwatch", "Overwatch", true, true)],
      activeVehicle: "overwatch",
      autotune: {},
    });
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  // Open the panel and flush the mount-time status fetch's state update so the
  // async resolution doesn't trip React's act() warning.
  async function openPanel() {
    await act(async () => {
      fireEvent.click(screen.getByTitle(/Autotune/i));
      await Promise.resolve();
    });
  }

  it("opens to the IDLE entry point with a Run button, NOT auto-firing", async () => {
    render(<AutotunePanel />);
    await openPanel();
    expect(screen.getByText(/Run Autotune/i)).toBeInTheDocument();
  });

  it("requires the confirm dialog before starting (no fire on the first click)", async () => {
    const fetchSpy = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    render(<AutotunePanel />);
    await openPanel();
    // First click → CONFIRM dialog with the safety preconditions, NOT a start POST.
    fireEvent.click(screen.getByText(/Run Autotune/i));
    expect(screen.getByText(/In-flight maneuver/i)).toBeInTheDocument();
    expect(screen.getByText(/HOVERING/i)).toBeInTheDocument();
    const startCalls = fetchSpy.mock.calls.filter(
      (c) => typeof c[0] === "string" && c[0].includes("/autotune/start"),
    );
    expect(startCalls).toHaveLength(0); // nothing started yet — only the confirm UI

    // Confirm → NOW it POSTs to /autotune/start.
    fireEvent.click(screen.getByText(/Confirm & tune/i));
    await waitFor(() => {
      const after = fetchSpy.mock.calls.filter(
        (c) => typeof c[0] === "string" && c[0].includes("/autotune/start"),
      );
      expect(after.length).toBe(1);
    });
  });

  it("shows the live progress bar + cancel when RUNNING (from the store)", async () => {
    render(<AutotunePanel />);
    await openPanel();
    act(() => {
      useGcs.getState().setAutotune({
        vehicle: "overwatch", state: "RUNNING", progress: 60, axis: "pitch",
        reason: null, statustexts: [{ severity: 6, text: "Autotune: pitch", ts: 0 }], running: true,
      });
    });
    expect(screen.getByText("60%")).toBeInTheDocument();
    expect(screen.getByText(/Cancel autotune/i)).toBeInTheDocument();
    // The supplementary STATUSTEXT feed is shown when present (Overwatch).
    expect(screen.getByText("Autotune: pitch")).toBeInTheDocument();
  });

  it("shows COMPLETE with the gains-apply-on-disarm note", async () => {
    render(<AutotunePanel />);
    await openPanel();
    act(() => {
      useGcs.getState().setAutotune({
        vehicle: "overwatch", state: "COMPLETE", progress: 100, axis: "done",
        reason: null, statustexts: [], running: false,
      });
    });
    expect(screen.getByText(/Autotune complete/i)).toBeInTheDocument();
    expect(screen.getByText(/apply automatically on the next landing/i)).toBeInTheDocument();
  });

  it("disables Run when the target drone is offline", async () => {
    useGcs.setState({ vehicles: [veh("overwatch", "Overwatch", false, true)] });
    render(<AutotunePanel />);
    await openPanel();
    const btn = screen.getByText(/Run Autotune/i).closest("button")!;
    expect(btn).toBeDisabled();
  });

  // ── capability gate: a DDS-bridge vehicle (supports_autotune=false) ──────────
  it("disables Run + shows the TELEM2 hint when the drone can't autotune", async () => {
    // Outrider: connected but supports_autotune=false (cmd 212 UNSUPPORTED over DDS).
    useGcs.setState({
      vehicles: [{ ...veh("outrider", "Outrider", true, true), supports_autotune: false }],
      activeVehicle: "outrider",
    });
    render(<AutotunePanel />);
    await openPanel();
    const btn = screen.getByText(/Run Autotune/i).closest("button")!;
    expect(btn).toBeDisabled();
    // The grey-out carries the explanatory tooltip (not a bare disabled button).
    expect(btn).toHaveAttribute("title", expect.stringMatching(/MAVLink-on-TELEM2/i));
    // And the in-panel copy states WHY rather than the generic offline message.
    expect(screen.getByText(/MAVLink-on-TELEM2/i)).toBeInTheDocument();
  });

  it("keeps Run ENABLED for a fully-capable drone (supports_autotune true)", async () => {
    useGcs.setState({
      vehicles: [{ ...veh("overwatch", "Overwatch", true, true), supports_autotune: true }],
      activeVehicle: "overwatch",
    });
    render(<AutotunePanel />);
    await openPanel();
    const btn = screen.getByText(/Run Autotune/i).closest("button")!;
    expect(btn).not.toBeDisabled();
    expect(btn).not.toHaveAttribute("title");
  });

  it("treats a missing flag (older backend) as capable — Run stays enabled", async () => {
    // No supports_autotune field at all → must NOT be wrongly disabled.
    useGcs.setState({ vehicles: [veh("overwatch", "Overwatch", true, true)] });
    render(<AutotunePanel />);
    await openPanel();
    expect(screen.getByText(/Run Autotune/i).closest("button")!).not.toBeDisabled();
  });
});
