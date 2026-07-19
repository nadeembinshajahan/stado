import { useEffect } from "react";
import { connectWs } from "./lib/ws";
import { api } from "./lib/api";
import { useGcs } from "./store/useGcs";
import { useIsMobile } from "./lib/useIsMobile";
import StatusBar from "./components/StatusBar";
import MapView from "./components/MapView";
import Hud from "./components/Hud";
import CommandBar from "./components/CommandBar";
import ConversationPanel from "./components/ConversationPanel";
import Console from "./components/Console";
import ReportPage from "./components/ReportPage";
import ReplayPlayer from "./components/ReplayPlayer";
import LowBatteryBanner from "./components/LowBatteryBanner";
import AutotunePanel from "./components/AutotunePanel";
import ErrorBoundary from "./components/ErrorBoundary";
import MobileShell from "./components/mobile/MobileShell";
import OnboardingBanner from "./components/demo/OnboardingBanner";

export default function App() {
  useEffect(() => connectWs(), []);
  const isMobile = useIsMobile();
  const view3d = useGcs((s) => s.view3d);
  const vehicles = useGcs((s) => s.vehicles);
  const pois = useGcs((s) => s.pois);
  const savedRegions = useGcs((s) => s.savedRegions);
  const socketOpen = useGcs((s) => s.socketOpen);
  // Keep the backend (and thus the voice agent's context) in sync with the
  // operator's markers, so commands like "orbit Sector 1" resolve. Re-syncing on
  // `socketOpen` means the markers are re-pushed whenever the link (re)connects —
  // e.g. after a backend restart — not just on change or full page reload.
  useEffect(() => {
    if (socketOpen) api.setPois(pois).catch(() => {});
  }, [pois, socketOpen]);
  // Same for the named SEARCH AREAS, so "survey Sector 1" resolves on the agent.
  useEffect(() => {
    if (socketOpen) api.setRegions(savedRegions).catch(() => {});
  }, [savedRegions, socketOpen]);

  // Poll the fleet roster (~5s) so the HUDs + CommandBar per-drone picker know
  // which drones exist/are connected. (Was done by the old FleetSelector chip,
  // now removed — drone selection lives on the CommandBar's per-drone buttons.)
  const setVehicles = useGcs((s) => s.setVehicles);
  const setActiveVehicle = useGcs((s) => s.setActiveVehicle);
  const setReadyForFlight = useGcs((s) => s.setReadyForFlight);
  useEffect(() => {
    let alive = true;
    const refresh = async () => {
      try {
        const list = await api.vehicles();
        if (!alive) return;
        setVehicles(list);
        // Hydrate the Ready-for-Flight gate state from the roster so the UI
        // has the right disabled/enabled state on first paint (not just after
        // the first telemetry frame arrives).
        for (const v of list) {
          if (typeof v.ready_for_flight === "boolean") {
            setReadyForFlight(v.id, v.ready_for_flight,
                              Boolean(v.ready_for_flight_locked));
          }
        }
        if (useGcs.getState().activeVehicle == null) {
          const act = list.find((v) => v.active);
          if (act) setActiveVehicle(act.id);
        }
      } catch {
        /* fleet endpoint unavailable — keep last known list */
      }
    };
    refresh();
    const t = setInterval(refresh, 5000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [setVehicles, setActiveVehicle, setReadyForFlight]);

  // Mobile (<= 767px) gets a phone-shaped cockpit shell — bottom sheet for
  // commands, floating PTT, compact HUD strip. The store/websocket/api are
  // shared so feature parity comes for free. Desktop layout below is
  // untouched (pixel-identical for >= 1024).
  if (isMobile) return <MobileShell />;

  return (
    <div className="relative h-full w-full overflow-hidden">
      <OnboardingBanner />
      {/* base layer — isolated so a map error never blanks the cockpit. The Map
          genuinely owns the whole viewport, so its fallback is fullscreen; every
          OTHER boundary uses the default CONTAINED card so a panel throw degrades
          only that panel and leaves the cockpit + emergency controls intact. */}
      <ErrorBoundary key={view3d ? "3d" : "2d"} label="Map" fullscreen>
        <MapView />
      </ErrorBoundary>

      {/* floating video — each in its own boundary so a render throw in one
          feed (bad telemetry, overlay math) never blanks the whole cockpit. */}
      <ErrorBoundary label="Overwatch feed">
        <></>{/* DEMO: no camera in SITL */}
      </ErrorBoundary>

      {/* operator-configurable second feed (rtsp/https over WiFi) */}
      <ErrorBoundary label="Outrider feed">
        <></>
      </ErrorBoundary>

      {/* bottom-right: voice transcript, with the event console beneath it */}
      <div className="absolute bottom-3 right-3 z-30 flex w-96 flex-col gap-2">
        <ErrorBoundary label="Conversation">
          <ConversationPanel />
        </ErrorBoundary>
        <ErrorBoundary label="Console">
          <Console />
        </ErrorBoundary>
      </div>

      {/* top status bar */}
      <div className="absolute left-3 right-3 top-3 z-30 flex items-start gap-3">
        <div className="flex-1">
          <ErrorBoundary label="Status bar">
            <StatusBar />
          </ErrorBoundary>
        </div>
      </div>

      {/* bottom-left HUD — one per CONNECTED drone when a fleet is present (each
          Hud self-gates on its vehicle being connected, so both render when both
          are live); a single HUD otherwise. The flex column STACKS them
          vertically with a gap so two HUD chips never overlap. The whole column
          is boundary-wrapped so a bad telemetry value degrades only the HUDs. */}
      <ErrorBoundary label="HUD">
        <div className="absolute left-3 bottom-3 z-30 flex flex-col-reverse gap-2">
          {vehicles.length >= 2 ? (
            vehicles.map((v) => <Hud key={v.id} vehicleId={v.id} />)
          ) : (
            <Hud />
          )}
        </div>
      </ErrorBoundary>

      {/* bottom-center command bar */}
      <div className="absolute left-1/2 bottom-3 z-30 -translate-x-1/2">
        <ErrorBoundary label="Command bar">
          <CommandBar />
        </ErrorBoundary>
      </div>

      {/* left-rail Autotune control (PX4 rate-controller tune; confirm-gated) */}
      <ErrorBoundary label="Autotune">
        <AutotunePanel />
      </ErrorBoundary>

      {/* smart-RTL low-battery alert banner (top-center; voice confirms RTL) */}
      <LowBatteryBanner />

      {/* mission-replay transport (bottom-center; only shown in replay mode) */}
      <ReplayPlayer />

      {/* full-screen mission report overlay (toggled via reportOpen) */}
      <ErrorBoundary label="Report">
        <ReportPage />
      </ErrorBoundary>
    </div>
  );
}
