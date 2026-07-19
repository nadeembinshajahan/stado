import { describe, it, expect } from "vitest";
import {
  offsetLatLon,
  orientedRectCorners,
  latLonToRegionLocal,
  regionLocalToLatLon,
  resizeRegionByHandle,
  REGION_HANDLES,
  regionHandlePos,
  splitRectZones,
  lawnmowerPath,
  pointInPolygon,
  bearingDeg,
  poseAtTime,
  zoneColor,
  mapObjectGlyph,
  mapObjectColor,
} from "../src/lib/geo";

const NEAR = (a: number, b: number, eps = 1e-6) => Math.abs(a - b) <= eps;

describe("offsetLatLon", () => {
  it("moves north/east by the expected metres", () => {
    const [lat, lon] = offsetLatLon(0, 0, 111195, 0); // ~1 deg north at equator
    expect(lat).toBeCloseTo(1, 2);
    expect(lon).toBeCloseTo(0, 9);
  });

  it("east offset scales by cos(lat) so high-latitude lon deltas are larger", () => {
    const [, lonEq] = offsetLatLon(0, 0, 0, 1000);
    const [, lonHigh] = offsetLatLon(60, 0, 0, 1000);
    // At 60° latitude the same eastward metres → ~2× the longitude delta.
    expect(Math.abs(lonHigh)).toBeGreaterThan(Math.abs(lonEq) * 1.9);
  });
});

describe("orientedRectCorners", () => {
  it("returns 4 corners centered on the origin point (axis-aligned)", () => {
    const c = orientedRectCorners(10, 20, 200, 100, 0);
    expect(c).toHaveLength(4);
    // centroid ≈ the center
    const cLat = c.reduce((s, p) => s + p[0], 0) / 4;
    const cLon = c.reduce((s, p) => s + p[1], 0) / 4;
    expect(cLat).toBeCloseTo(10, 6);
    expect(cLon).toBeCloseTo(20, 6);
  });

  it("rotation by 90° swaps the apparent width/height extents", () => {
    const flat = orientedRectCorners(0, 0, 200, 100, 0);
    const rot = orientedRectCorners(0, 0, 200, 100, 90);
    const lonSpan = (cs: [number, number][]) =>
      Math.max(...cs.map((p) => p[1])) - Math.min(...cs.map((p) => p[1]));
    // After a 90° rotation the lon (east) extent should reflect the 100m side,
    // i.e. smaller than the un-rotated 200m east extent.
    expect(lonSpan(rot)).toBeLessThan(lonSpan(flat));
  });
});

describe("region local <-> latlon round-trip", () => {
  it("latLonToRegionLocal is the inverse of regionLocalToLatLon", () => {
    const cLat = 25.2;
    const cLon = 55.27;
    const heading = 37;
    for (const [x, y] of [
      [50, -30],
      [-120, 80],
      [0, 0],
      [200, 200],
    ] as [number, number][]) {
      const [lat, lon] = regionLocalToLatLon(x, y, cLat, cLon, heading);
      const [bx, by] = latLonToRegionLocal(lat, lon, cLat, cLon, heading);
      expect(bx).toBeCloseTo(x, 3);
      expect(by).toBeCloseTo(y, 3);
    }
  });

  it("regionHandlePos for a corner matches orientedRectCorners geometry", () => {
    const cLat = 0;
    const cLon = 0;
    const w = 400;
    const h = 200;
    const heading = 20;
    // The 'tr' handle is local (+w/2, +h/2); confirm it round-trips to local.
    const tr = REGION_HANDLES.find((x) => x.id === "tr")!;
    const [lat, lon] = regionHandlePos(tr, cLat, cLon, w, h, heading);
    const [lx, ly] = latLonToRegionLocal(lat, lon, cLat, cLon, heading);
    expect(lx).toBeCloseTo(w / 2, 2);
    expect(ly).toBeCloseTo(h / 2, 2);
  });
});

describe("resizeRegionByHandle", () => {
  it("dragging a corner keeps the opposite corner fixed", () => {
    const cLat = 0;
    const cLon = 0;
    const w = 400;
    const h = 200;
    const heading = 0;
    const tr = REGION_HANDLES.find((x) => x.id === "tr")!; // (+1,+1)
    const bl = REGION_HANDLES.find((x) => x.id === "bl")!; // (-1,-1) opposite

    const blBefore = regionHandlePos(bl, cLat, cLon, w, h, heading);
    // The fixed anchor for the TR handle is the BL corner at local (-200, -100).
    // Drag TR to local (+300, +150) → new W = |300-(-200)| = 500, H = |150-(-100)| = 250.
    const [dragLat, dragLon] = regionLocalToLatLon(300, 150, cLat, cLon, heading);
    const next = resizeRegionByHandle(tr, dragLat, dragLon, cLat, cLon, w, h, heading);

    expect(next.width_m).toBeCloseTo(500, 1);
    expect(next.height_m).toBeCloseTo(250, 1);
    // The opposite (BL) corner must stay put.
    const blAfter = regionHandlePos(
      bl,
      next.center[0],
      next.center[1],
      next.width_m,
      next.height_m,
      heading,
    );
    expect(blAfter[0]).toBeCloseTo(blBefore[0], 5);
    expect(blAfter[1]).toBeCloseTo(blBefore[1], 5);
  });

  it("an edge handle only changes its own axis (keeps the other dimension)", () => {
    // The 'r' edge's fixed anchor is the left edge at local x = -200; dragging
    // it to local x = 250 → new width = |250 - (-200)| = 450; height unchanged.
    const next = resizeRegionByHandle(
      REGION_HANDLES.find((x) => x.id === "r")!, // (+1, 0)
      ...regionLocalToLatLon(250, 0, 0, 0, 0),
      0,
      0,
      400,
      200,
      0,
    );
    expect(next.width_m).toBeCloseTo(450, 1);
    expect(next.height_m).toBeCloseTo(200, 1); // unchanged
  });

  it("clamps dimensions to a small minimum (never collapses to 0)", () => {
    const next = resizeRegionByHandle(
      REGION_HANDLES.find((x) => x.id === "r")!,
      ...regionLocalToLatLon(-200, 0, 0, 0, 0), // drag the right edge across the center
      0,
      0,
      400,
      200,
      0,
    );
    expect(next.width_m).toBeGreaterThanOrEqual(1);
  });
});

describe("splitRectZones", () => {
  it("splits the longer (height) dimension into n strips", () => {
    const zones = splitRectZones(0, 0, 100, 400, 0, 2, 5);
    expect(zones).toHaveLength(2);
    zones.forEach((z) => expect(z).toHaveLength(4));
  });

  it("returns empty when strips would be non-positive", () => {
    // gap eats the whole length → no room for strips.
    expect(splitRectZones(0, 0, 100, 10, 0, 4, 50)).toEqual([]);
  });

  it("returns empty for n < 1", () => {
    expect(splitRectZones(0, 0, 100, 100, 0, 0, 5)).toEqual([]);
  });
});

describe("lawnmowerPath", () => {
  it("returns an even number of turn points for a simple rectangle", () => {
    const rect: [number, number][] = orientedRectCorners(0, 0, 200, 200, 0);
    const path = lawnmowerPath(rect, 20);
    expect(path.length).toBeGreaterThan(0);
    expect(path.length % 2).toBe(0); // entry/exit pairs per scan line
  });

  it("returns [] for a degenerate polygon (< 3 verts)", () => {
    expect(lawnmowerPath([[0, 0], [0, 1]], 20)).toEqual([]);
  });
});

describe("pointInPolygon", () => {
  const sq: [number, number][] = [
    [0, 0],
    [0, 10],
    [10, 10],
    [10, 0],
  ];
  it("detects an interior point", () => {
    expect(pointInPolygon(5, 5, sq)).toBe(true);
  });
  it("rejects an exterior point", () => {
    expect(pointInPolygon(20, 20, sq)).toBe(false);
  });
});

describe("bearingDeg", () => {
  it("due north is ~0°, due east ~90°", () => {
    expect(NEAR(bearingDeg(0, 0, 1, 0), 0, 0.5)).toBe(true);
    expect(Math.round(bearingDeg(0, 0, 0, 1))).toBe(90);
  });
});

describe("poseAtTime", () => {
  const path: [number, number, number][] = [
    [0, 0, 0],
    [0, 1, 100],
    [0, 2, 200],
  ];
  const times = [1000, 1010, 1020];
  const startTs = 1000;

  it("interpolates the midpoint of a segment", () => {
    const p = poseAtTime(path, times, startTs, 5); // 5s in = halfway to t=1010
    expect(p).not.toBeNull();
    expect(p!.lon).toBeCloseTo(0.5, 6);
    expect(p!.alt).toBeCloseTo(50, 6);
    expect(p!.index).toBe(0);
  });

  it("clamps before the start", () => {
    const p = poseAtTime(path, times, startTs, -50);
    expect(p!.lon).toBeCloseTo(0, 6);
  });

  it("clamps past the end (parks at the last sample)", () => {
    const p = poseAtTime(path, times, startTs, 9999);
    expect(p!.lon).toBeCloseTo(2, 6);
  });

  it("returns null for an empty path", () => {
    expect(poseAtTime([], [], 0, 0)).toBeNull();
  });

  it("handles a single-sample path without dividing by zero", () => {
    const p = poseAtTime([[5, 6, 7]], [1000], 1000, 3);
    expect(p).toEqual({ lat: 5, lon: 6, alt: 7, heading: 0, index: 0 });
  });
});

describe("color/glyph helpers", () => {
  it("named drones get their signature colors", () => {
    expect(zoneColor("overwatch", 3)).toBe("#22e3c4");
    expect(zoneColor("Outrider", 0)).toBe("#ffb020");
  });
  it("unknown names fall back through the palette by index", () => {
    expect(zoneColor("zeta", 0)).toBe("#22e3c4");
    expect(zoneColor("zeta", 1)).toBe("#ffb020");
  });
  it("glyph + color fall back for unknown classes", () => {
    expect(mapObjectGlyph("spaceship")).toBe(mapObjectGlyph("car"));
    expect(mapObjectColor("spaceship")).toBe("#7c9cff");
    expect(mapObjectColor("person")).toBe("#ff8a3d");
  });
});
