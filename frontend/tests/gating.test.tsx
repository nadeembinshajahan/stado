import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup, act } from "@testing-library/react";
import { useGcs } from "../src/store/useGcs";
import type { VehicleInfo } from "../src/lib/types";

// WebRTC is hardware/browser-only — stub the hook so the panels mount without a
// real peer connection. We keep a spy so we can assert mount/unmount = teardown.
const useGo2RtcWebRTC = vi.fn(() => "live");
vi.mock("../src/lib/useGo2Rtc", () => ({
  GO2RTC: "http://127.0.0.1:1984",
  useGo2RtcWebRTC: (...args: unknown[]) => useGo2RtcWebRTC(...args),
}));

// RecordButton hits /api/record/status on mount; stub fetch so it no-ops.
beforeEach(() => {
  useGo2RtcWebRTC.mockClear();
  useGo2RtcWebRTC.mockReturnValue("live"); // default: media delivering frames
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ recording: {} }),
        text: () => Promise.resolve("{}"),
      }),
    ),
  );
  // socketOpen=true is the real precondition for any roster `connected` flag to
  // be trustworthy (the gate treats a dropped GCS socket as not-connected).
  useGcs.setState({ vehicles: [], socketOpen: true, showMainFeed: true, showSecondFeed: true, uiMode: "navigate", focusedFeed: null });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function veh(id: string, connected: boolean, active = false): VehicleInfo {
  return { id, name: id.toUpperCase(), kind: "quadcopter", connected, active };
}

// Import AFTER the vi.mock above is registered.
import VideoPanel from "../src/components/VideoPanel";
import SecondFeedPanel from "../src/components/SecondFeedPanel";
import Hud from "../src/components/Hud";

describe("VideoPanel gating (Overwatch feed)", () => {
  it("renders nothing when Overwatch is NOT connected", () => {
    useGcs.setState({ vehicles: [veh("overwatch", false)] });
    const { container } = render(<VideoPanel />);
    expect(container).toBeEmptyDOMElement();
    expect(useGo2RtcWebRTC).not.toHaveBeenCalled();
  });

  it("renders the feed (mounts the WebRTC hook) when Overwatch IS connected", () => {
    useGcs.setState({ vehicles: [veh("overwatch", true, true)] });
    render(<VideoPanel />);
    expect(useGo2RtcWebRTC).toHaveBeenCalled();
  });

  it("C1 FIX: a transient connected=false does NOT unmount the panel (WebRTC survives)", () => {
    useGcs.setState({ vehicles: [veh("overwatch", true, true)] });
    const { container, rerender } = render(<VideoPanel />);
    // Capture the SAME <video> node we'll check survives the flip. The
    // RTCPeerConnection lives in the hook's effect, which only tears down when
    // this element unmounts — so node identity surviving = the pc survives.
    const videoBefore = container.querySelector("video");
    expect(videoBefore).not.toBeNull();
    // Simulate a momentary roster blip flipping connected → false (poll race).
    act(() => {
      useGcs.setState({ vehicles: [veh("overwatch", false, true)] });
    });
    rerender(<VideoPanel />);
    const videoAfter = container.querySelector("video");
    // The <video> MUST still be mounted AND be the very SAME node (not a
    // remount) — the debounced connected-gate keeps it alive across a transient
    // flip, so the feed never re-handshakes. Pins the review C1 fix.
    expect(videoAfter).not.toBeNull();
    expect(videoAfter).toBe(videoBefore);
  });

  it("C1 FIX: stays mounted even after the roster fully drops the vehicle", () => {
    useGcs.setState({ vehicles: [veh("overwatch", true, true)] });
    const { container, rerender } = render(<VideoPanel />);
    expect(container.querySelector("video")).not.toBeNull();
    // The vehicle disappears from the roster entirely (not just connected:false).
    act(() => {
      useGcs.setState({ vehicles: [] });
    });
    rerender(<VideoPanel />);
    // Once mounted, the feed stays mounted (no unmount = no WebRTC teardown).
    expect(container.querySelector("video")).not.toBeNull();
  });

  it("a dropped GCS socket keeps the feed MOUNTED (WebRTC survives) for instant recovery", () => {
    useGcs.setState({ vehicles: [veh("overwatch", true, true)], socketOpen: true });
    const { container, rerender } = render(<VideoPanel />);
    const videoBefore = container.querySelector("video");
    expect(videoBefore).not.toBeNull();
    // GCS backend WebSocket drops — telemetry/roster freeze. The feed must NOT be
    // torn down (so reconnect is instant); the offline overlay handles truthfulness.
    act(() => {
      useGcs.setState({ socketOpen: false });
    });
    rerender(<VideoPanel />);
    const videoAfter = container.querySelector("video");
    expect(videoAfter).not.toBeNull();
    expect(videoAfter).toBe(videoBefore);
  });
});

describe("SecondFeedPanel gating (Outrider feed)", () => {
  it("renders nothing when Outrider is NOT connected", () => {
    useGcs.setState({ vehicles: [veh("outrider", false)] });
    const { container } = render(<SecondFeedPanel />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders when Outrider IS connected", () => {
    useGcs.setState({ vehicles: [veh("outrider", true)] });
    const { container } = render(<SecondFeedPanel />);
    expect(container.querySelector("video")).not.toBeNull();
  });
});

// Telemetry-stale decoupling: the VIDEO streams independently of telemetry over
// go2rtc/WebRTC. When telemetry wedges (sustained `connected:false` past the
// debounce) the feed must STAY visible with a NON-blocking "telemetry stale"
// badge — NOT a blocking "reconnecting…" curtain that blacks out a live frame.
describe("telemetry decoupled from feed visibility (sustained disconnect)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  // The connected-gate debounces `offline` by graceMs (default 4000ms) before it
  // flips. Drive a continuous-disconnect window past that with fake timers.
  const advancePastGrace = () => {
    act(() => {
      vi.advanceTimersByTime(5000);
    });
  };

  it("VideoPanel: a sustained disconnect keeps the LIVE feed visible with a non-blocking 'TELEMETRY STALE' badge (no black curtain)", () => {
    useGo2RtcWebRTC.mockReturnValue("live"); // media is still delivering frames
    useGcs.setState({ vehicles: [veh("overwatch", true, true)] });
    const { container, rerender } = render(<VideoPanel />);
    const videoBefore = container.querySelector("video");
    expect(videoBefore).not.toBeNull();

    // Telemetry wedges and STAYS down past the debounce window.
    act(() => {
      useGcs.setState({ vehicles: [veh("overwatch", false, true)] });
    });
    advancePastGrace();
    rerender(<VideoPanel />);

    // SAME <video> node = WebRTC never torn down; the picture keeps playing.
    const videoAfter = container.querySelector("video");
    expect(videoAfter).toBe(videoBefore);
    // Non-blocking stale badge is shown…
    expect(screen.getByText(/telemetry stale/i)).toBeInTheDocument();
    // …and the blocking "reconnecting…" curtain is NOT (a live frame is never blacked out).
    expect(screen.queryByText(/reconnecting/i)).toBeNull();
  });

  it("VideoPanel: a sustained disconnect with NO live frame DOES show the dimming 'reconnecting…' curtain", () => {
    useGo2RtcWebRTC.mockReturnValue("error"); // no media frames to protect
    useGcs.setState({ vehicles: [veh("overwatch", true, true)] });
    const { rerender } = render(<VideoPanel />);
    act(() => {
      useGcs.setState({ vehicles: [veh("overwatch", false, true)] });
    });
    advancePastGrace();
    rerender(<VideoPanel />);
    expect(screen.getByText(/reconnecting/i)).toBeInTheDocument();
    expect(screen.queryByText(/telemetry stale/i)).toBeNull();
  });

  it("SecondFeedPanel: a sustained Outrider disconnect keeps the LIVE feed visible with the stale badge (no black curtain)", () => {
    useGo2RtcWebRTC.mockReturnValue("live");
    useGcs.setState({ vehicles: [veh("outrider", true)] });
    const { container, rerender } = render(<SecondFeedPanel />);
    const videoBefore = container.querySelector("video");
    expect(videoBefore).not.toBeNull();

    act(() => {
      useGcs.setState({ vehicles: [veh("outrider", false)] });
    });
    advancePastGrace();
    rerender(<SecondFeedPanel />);

    expect(container.querySelector("video")).toBe(videoBefore);
    expect(screen.getByText(/telemetry stale/i)).toBeInTheDocument();
    expect(screen.queryByText(/reconnecting/i)).toBeNull();
  });
});

describe("Hud gating (per-connected drone)", () => {
  it("renders nothing for a disconnected vehicle", () => {
    useGcs.setState({ vehicles: [veh("overwatch", false)] });
    const { container } = render(<Hud vehicleId="overwatch" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the instrument for a connected vehicle (name shown)", () => {
    useGcs.setState({
      vehicles: [veh("overwatch", true, true)],
      activeVehicle: "overwatch",
    });
    render(<Hud vehicleId="overwatch" />);
    // The collapsed chip shows the (uppercased) drone name.
    expect(screen.getByText("OVERWATCH")).toBeInTheDocument();
  });

  it("renders BOTH connected drones' HUDs at once", () => {
    useGcs.setState({
      vehicles: [veh("overwatch", true, true), veh("outrider", true)],
      activeVehicle: "overwatch",
    });
    render(
      <>
        <Hud vehicleId="overwatch" />
        <Hud vehicleId="outrider" />
      </>,
    );
    expect(screen.getByText("OVERWATCH")).toBeInTheDocument();
    expect(screen.getByText("OUTRIDER")).toBeInTheDocument();
  });

  it("does not crash on fully-null/partial telemetry", () => {
    useGcs.setState({
      vehicles: [veh("overwatch", true, true)],
      activeVehicle: "overwatch",
      fleetTelem: {},
    });
    expect(() => render(<Hud vehicleId="overwatch" />)).not.toThrow();
  });
});
