import { useState } from "react";
import MapView from "../MapView";
import ErrorBoundary from "../ErrorBoundary";
import LowBatteryBanner from "../LowBatteryBanner";
import ReportPage from "../ReportPage";
import ReplayPlayer from "../ReplayPlayer";
import MobileTopBar from "./MobileTopBar";
import MobileHud from "./MobileHud";
import MobileCommandSheet from "./MobileCommandSheet";
import MobilePttFab from "./MobilePttFab";
import MobileVideoSheet from "./MobileVideoSheet";
import MobileToast from "./MobileToast";
import OnboardingBanner from "../demo/OnboardingBanner";

/**
 * Mobile-only cockpit shell. Renders the map as the full canvas, with all
 * controls layered as floating sheets/pills sized for thumbs. The shell is
 * mounted by `App.tsx` whenever `useIsMobile()` returns true; the desktop
 * shell is rendered otherwise. They share the same store + websocket, so the
 * URL/state survives a layout swap (e.g. rotating an iPad).
 *
 * Layering (z-order, low→high):
 *   z-0   MapView canvas (full viewport)
 *   z-30  MobileHud floating strip
 *   z-40  MobileTopBar
 *   z-50  MobileCommandSheet
 *   z-55  MobilePttFab
 *   z-65+ MobileVideoSheet (modal)
 *   z-80  MobileToast (top-of-screen)
 *  z-[100]+ Preloader (already handled outside the shell), ReportPage
 */
export default function MobileShell() {
  const [sheetHeight, setSheetHeight] = useState(168);
  const [videoOpen, setVideoOpen] = useState(false);

  return (
    <div className="relative h-full w-full overflow-hidden bg-ink">
      <OnboardingBanner />
      {/* MAP — full canvas. Owns clicks, gestures, pan/zoom. */}
      <ErrorBoundary label="Map" fullscreen>
        <MapView />
      </ErrorBoundary>

      {/* Top bar (status / vehicle pill / 2D-3D / more) */}
      <ErrorBoundary label="Top bar">
        <MobileTopBar onOpenVideo={() => setVideoOpen(true)} />
      </ErrorBoundary>

      {/* Floating glanceable HUD — sits above the resting sheet edge */}
      <ErrorBoundary label="HUD">
        <MobileHud bottomOffset={sheetHeight + 6} />
      </ErrorBoundary>

      {/* Primary commands + secondary tray (bottom sheet) */}
      <ErrorBoundary label="Commands">
        <MobileCommandSheet onHeightChange={setSheetHeight} />
      </ErrorBoundary>

      {/* Large floating push-to-talk FAB */}
      <ErrorBoundary label="Voice">
        <MobilePttFab />
      </ErrorBoundary>

      {/* Video feeds (modal sheet) */}
      <ErrorBoundary label="Video">
        <MobileVideoSheet open={videoOpen} onClose={() => setVideoOpen(false)} />
      </ErrorBoundary>

      {/* Cross-shell affordances — these already render at viewport scope */}
      <LowBatteryBanner />
      <ReplayPlayer />
      <ErrorBoundary label="Report">
        <ReportPage />
      </ErrorBoundary>

      {/* Snackbar for command failures (subscribes to the log) */}
      <MobileToast />
    </div>
  );
}
