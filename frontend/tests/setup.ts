import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// Reset the DOM + any per-test spies between tests so component state never
// leaks across cases.
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// ── jsdom shims for browser APIs the components touch ────────────────────────
// These are NOT exercised by the logic tests, but importing the components
// pulls them in, so they must at least exist as no-ops.

// matchMedia (framer-motion's initPrefersReducedMotion reads it on first motion
// mount). jsdom doesn't implement it; define it unconditionally so every test
// gets a complete MediaQueryList stub (with the legacy add/removeListener).
Object.defineProperty(window, "matchMedia", {
  writable: true,
  configurable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});

// ResizeObserver (framer-motion `layout`).
if (!("ResizeObserver" in globalThis)) {
  // @ts-expect-error minimal stub
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

// requestAnimationFrame / cancelAnimationFrame (canvas rAF loops).
if (!globalThis.requestAnimationFrame) {
  globalThis.requestAnimationFrame = ((cb: FrameRequestCallback) =>
    setTimeout(() => cb(performance.now()), 0) as unknown as number) as typeof requestAnimationFrame;
  globalThis.cancelAnimationFrame = ((id: number) => clearTimeout(id)) as typeof cancelAnimationFrame;
}

// Canvas 2D context — VideoPanel/SecondFeedPanel grab one; jsdom returns null.
// A permissive proxy stub keeps the reticle/draw code from throwing.
HTMLCanvasElement.prototype.getContext = vi.fn(() =>
  new Proxy(
    {},
    {
      get: (_t, prop) => {
        if (prop === "canvas") return undefined;
        if (prop === "measureText") return () => ({ width: 0 });
        if (prop === "getImageData") return () => ({ data: [] });
        return () => undefined;
      },
    },
  ),
) as unknown as typeof HTMLCanvasElement.prototype.getContext;
