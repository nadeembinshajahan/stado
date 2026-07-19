import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// Test runner config — kept separate from vite.config.ts so the dev server's
// proxy/server block doesn't leak into the (headless, backend-less) test env.
// Tests live in tests/ (NOT src/) so the app's `tsc --noEmit` build stays clean
// and never picks up test-only globals.
export default defineConfig({
  plugins: [react()],
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
    // jsdom has no canvas/WebRTC/AudioContext; the setup file stubs what the
    // components touch so they mount without a real backend or hardware.
    css: false,
  },
});
