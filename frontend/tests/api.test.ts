import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { api } from "../src/lib/api";

// Mock fetch per-test; the api module reads the global `fetch`.
const mockFetch = vi.fn();

beforeEach(() => {
  vi.stubGlobal("fetch", mockFetch);
  mockFetch.mockReset();
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function ok(json: unknown) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(json),
    text: () => Promise.resolve(JSON.stringify(json)),
  });
}
function fail(status: number, body: string) {
  return Promise.resolve({
    ok: false,
    status,
    statusText: `HTTP ${status}`,
    json: () => Promise.reject(new Error("not json")),
    text: () => Promise.resolve(body),
  });
}

describe("api POST commands", () => {
  it("arm POSTs to /api/command/arm and returns the parsed body", async () => {
    mockFetch.mockReturnValueOnce(ok({ ok: true, armed: true }));
    const res = await api.arm();
    expect(mockFetch).toHaveBeenCalledWith(
      "/api/command/arm",
      expect.objectContaining({ method: "POST" }),
    );
    expect(res).toEqual({ ok: true, armed: true });
  });

  it("arm('all') appends the vehicle query param", async () => {
    mockFetch.mockReturnValueOnce(ok({ ok: true }));
    await api.arm("all");
    expect(mockFetch).toHaveBeenCalledWith("/api/command/arm?vehicle=all", expect.anything());
  });

  it("takeoff sends altitude + vehicle in the JSON body", async () => {
    mockFetch.mockReturnValueOnce(ok({ ok: true }));
    await api.takeoff(25, "overwatch");
    const [, opts] = mockFetch.mock.calls[0];
    expect(JSON.parse(opts.body)).toEqual({ altitude: 25, vehicle: "overwatch" });
  });

  it("THROWS (does not swallow) on a non-ok response, surfacing the body", async () => {
    mockFetch.mockReturnValueOnce(fail(400, "motors already armed"));
    await expect(api.land()).rejects.toThrow(/motors already armed/);
  });

  it("a thrown error still names the failing path", async () => {
    mockFetch.mockReturnValueOnce(fail(500, "boom"));
    await expect(api.rtl()).rejects.toThrow(/\/command\/rtl failed/);
  });
});

describe("api GET helpers", () => {
  it("vehicles() fetches /api/vehicles", async () => {
    mockFetch.mockReturnValueOnce(ok([{ id: "overwatch" }]));
    const v = await api.vehicles();
    expect(mockFetch).toHaveBeenCalledWith("/api/vehicles");
    expect(v).toEqual([{ id: "overwatch" }]);
  });

  it("vehicles() carries through the per-vehicle capability flags", async () => {
    // The backend now adds supports_offboard/missions/autotune to each item so
    // the cockpit UI can grey-out unsupported actions; they must survive the
    // fetch->VehicleInfo[] mapping unchanged (no client-side stripping).
    mockFetch.mockReturnValueOnce(
      ok([
        { id: "overwatch", supports_offboard: true, supports_missions: true, supports_autotune: true },
        { id: "outrider", supports_offboard: false, supports_missions: false, supports_autotune: false },
      ]),
    );
    const v = await api.vehicles();
    const by = Object.fromEntries(v.map((x) => [x.id, x]));
    expect(by.overwatch.supports_autotune).toBe(true);
    expect(by.outrider.supports_offboard).toBe(false);
    expect(by.outrider.supports_missions).toBe(false);
    expect(by.outrider.supports_autotune).toBe(false);
  });

  it("flight(id) rejects when the detail is missing (404)", async () => {
    mockFetch.mockReturnValueOnce(fail(404, "nope"));
    await expect(api.flight("x")).rejects.toThrow(/flight x not found/);
  });
});

describe("api.vision.seedBox (click-to-track)", () => {
  it("POSTs the normalized box to /api/vision/seed_box", async () => {
    mockFetch.mockReturnValueOnce(ok({ ok: true }));
    const box = { x: 0.1, y: 0.2, w: 0.3, h: 0.4, label: "car" };
    await api.vision.seedBox(box);
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toBe("/api/vision/seed_box");
    expect(JSON.parse(opts.body)).toEqual(box);
  });
});

describe("api.record", () => {
  it("start returns the {ok,file,already} shape", async () => {
    mockFetch.mockReturnValueOnce(ok({ ok: true, file: "/tmp/x.mp4" }));
    const r = await api.record.start("drone");
    expect(r).toEqual({ ok: true, file: "/tmp/x.mp4" });
  });
});
