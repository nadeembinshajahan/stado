import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, cleanup, act } from "@testing-library/react";
import { useGcs } from "../src/store/useGcs";
import type { VehicleInfo } from "../src/lib/types";

// ── WebRTC + api mocks ───────────────────────────────────────────────────────
vi.mock("../src/lib/useGo2Rtc", () => ({
  GO2RTC: "http://127.0.0.1:1984",
  useGo2RtcWebRTC: () => "live",
}));

// Controllable vision API so we can simulate the SEED RACE: the first N seed_box
// calls return {ok:false, reason:"no frame yet"} (pipeline has no frame), then a
// success. The retry loop must keep trying and ultimately lock.
const seedBox = vi.fn();
const visionStart = vi.fn(() => Promise.resolve({ ok: true }));
vi.mock("../src/lib/api", () => ({
  MJPEG_URL: "/api/vision/stream.mjpg",
  api: {
    vision: {
      start: (...a: unknown[]) => visionStart(...a),
      seedBox: (...a: unknown[]) => seedBox(...a),
      stop: () => Promise.resolve({ ok: true }),
    },
    record: { status: () => Promise.resolve({ recording: {} }) },
  },
}));

import VideoPanel from "../src/components/VideoPanel";

function veh(id: string, connected: boolean, active = false): VehicleInfo {
  return { id, name: id.toUpperCase(), kind: "hexacopter", connected, active };
}

beforeEach(() => {
  seedBox.mockReset();
  visionStart.mockClear();
  vi.useFakeTimers();
  useGcs.setState({
    vehicles: [veh("overwatch", true, true)],
    activeVehicle: "overwatch",
    socketOpen: true, // live GCS link — precondition for the connected-gate to mount the feed
    visionRunning: false,
    uiMode: "navigate",
    showMainFeed: true,
    focusedFeed: null,
    boxSelecting: false,
    log: [],
  });
});
afterEach(() => {
  vi.useRealTimers();
  cleanup();
});

// Drive the click-to-track seed flow directly through the canvas pointer
// handlers. jsdom lacks layout, so we stub getBoundingClientRect to a known box
// and setPointerCapture (not implemented in jsdom).
async function drawBox(canvas: HTMLCanvasElement) {
  canvas.getBoundingClientRect = () =>
    ({ left: 0, top: 0, width: 1000, height: 1000, right: 1000, bottom: 1000, x: 0, y: 0, toJSON() {} }) as DOMRect;
  // @ts-expect-error jsdom has no pointer capture
  canvas.setPointerCapture = () => {};
  // @ts-expect-error jsdom has no pointer capture
  canvas.releasePointerCapture = () => {};

  const opts = (x: number, y: number) => ({ clientX: x, clientY: y, pointerId: 1, bubbles: true });
  const PointerEventCtor =
    (globalThis as unknown as { PointerEvent?: typeof MouseEvent }).PointerEvent ?? MouseEvent;
  await act(async () => {
    canvas.dispatchEvent(new PointerEventCtor("pointerdown", opts(100, 100)));
    canvas.dispatchEvent(new PointerEventCtor("pointermove", opts(400, 400)));
    canvas.dispatchEvent(new PointerEventCtor("pointerup", opts(400, 400)));
  });
}

describe("click-to-track seed RACE retry", () => {
  it("retries while the pipeline reports 'no frame', then locks", async () => {
    // Fail twice with a retryable 'no frame', then succeed.
    seedBox
      .mockResolvedValueOnce({ ok: false, reason: "no frame yet" })
      .mockResolvedValueOnce({ ok: false, reason: "no frame yet" })
      .mockResolvedValueOnce({ ok: true });

    const { container } = render(<VideoPanel />);
    // Arm SELECT mode (the TRACK button).
    const trackBtn = [...container.querySelectorAll("button")].find((b) =>
      /TRACK/.test(b.textContent || ""),
    )!;
    await act(async () => trackBtn.click());

    const canvas = container.querySelector("canvas")!;
    await drawBox(canvas);

    // Advance through the retry backoff (300ms × attempts).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200);
    });

    expect(seedBox).toHaveBeenCalledTimes(3);
    // The box passed each time must be the SAME normalized ROI (300/1000 = 0.3).
    const box = seedBox.mock.calls[0][0] as { x: number; y: number; w: number; h: number };
    expect(box.x).toBeCloseTo(0.1, 6);
    expect(box.y).toBeCloseTo(0.1, 6);
    expect(box.w).toBeCloseTo(0.3, 6);
    expect(box.h).toBeCloseTo(0.3, 6);
    const log = useGcs.getState().log;
    expect(log.some((l) => /locked/i.test(l.text))).toBe(true);
  });

  it("stops retrying immediately on a NON-'no frame' rejection (terminal)", async () => {
    seedBox.mockResolvedValue({ ok: false, reason: "bad ROI" });
    const { container } = render(<VideoPanel />);
    const trackBtn = [...container.querySelectorAll("button")].find((b) =>
      /TRACK/.test(b.textContent || ""),
    )!;
    await act(async () => trackBtn.click());
    await drawBox(container.querySelector("canvas")!);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200);
    });
    // A terminal failure → exactly one attempt, no retry storm.
    expect(seedBox).toHaveBeenCalledTimes(1);
    expect(useGcs.getState().log.some((l) => /bad ROI/.test(l.text))).toBe(true);
  });

  it("auto-starts the vision pipeline if it wasn't running", async () => {
    seedBox.mockResolvedValue({ ok: true });
    useGcs.setState({ visionRunning: false });
    const { container } = render(<VideoPanel />);
    const trackBtn = [...container.querySelectorAll("button")].find((b) =>
      /TRACK/.test(b.textContent || ""),
    )!;
    await act(async () => trackBtn.click());
    await drawBox(container.querySelector("canvas")!);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(visionStart).toHaveBeenCalledTimes(1);
  });
});
