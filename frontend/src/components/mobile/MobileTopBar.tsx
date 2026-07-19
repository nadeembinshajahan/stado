import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  BatteryFull, BatteryLow, BatteryMedium, Box, ChevronDown, ClipboardList,
  MoreHorizontal, Video, Wifi, WifiOff, X,
} from "lucide-react";
import { EMPTY, useGcs } from "../../store/useGcs";
import { api } from "../../lib/api";
import HelpButton from "../demo/HelpButton";

const ACTIVE_COLOR = "#22e3c4";
const OTHER_COLOR = "#ffb020";

function BattIcon({ pct }: { pct: number | null | undefined }) {
  const v = pct ?? 0;
  const cls = v > 50 ? "text-ok" : v > 20 ? "text-warn" : "text-danger";
  const Icon = v > 60 ? BatteryFull : v > 25 ? BatteryMedium : BatteryLow;
  return <Icon size={14} className={cls} />;
}

/**
 * Mobile top bar — pinned under the notch (safe-area). Compact identity strip:
 *   [link]  [vehicle pill ▾]   [2D/3D]   [⋯ more]
 * The vehicle pill expands to a per-drone picker (active vs other-fleet color
 * preserved from desktop). The ⋯ menu opens Reports + the Video sheet toggle.
 */
export default function MobileTopBar({
  onOpenVideo,
}: {
  onOpenVideo: () => void;
}) {
  const { telem, socketOpen, view3d, toggle3d, setReportOpen } = useGcs();
  const vehicles = useGcs((s) => s.vehicles);
  const activeVehicle = useGcs((s) => s.activeVehicle);
  const fleetTelem = useGcs((s) => s.fleetTelem);
  const setActive = useGcs((s) => s.setActiveVehicle);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);

  // Active drone's telemetry feeds the pill. Falls back to the live `telem`
  // when there's no per-vehicle data yet (single-drone case).
  const activeT =
    (activeVehicle && fleetTelem[activeVehicle]) ||
    (activeVehicle == null ? telem : EMPTY);
  const activeVeh = vehicles.find((v) => v.id === activeVehicle) ?? null;
  const activeName = activeVeh?.name ?? "DRONE";
  const mode = activeT.connected ? activeT.mode ?? "—" : "OFFLINE";

  const pickActive = async (id: string) => {
    setPickerOpen(false);
    if (id === activeVehicle) return;
    // Drive both store (UI) + backend (commands target this drone).
    setActive(id);
    try {
      await api.setActiveVehicle(id);
    } catch {
      /* best effort — store update already moved the HUDs/commands */
    }
  };

  return (
    <div
      className="safe-top safe-x pointer-events-none absolute inset-x-0 top-0 z-40"
    >
      <div className="pointer-events-auto mx-2 mt-2 flex items-center gap-2">
        {/* link status */}
        <div className="glass instrument tap flex items-center gap-1.5 rounded-xl px-2.5">
          {socketOpen ? (
            <Wifi size={14} className="text-ok" />
          ) : (
            <WifiOff size={14} className="text-danger" />
          )}
          <span className={`text-[10px] font-bold tracking-wider ${socketOpen ? "text-ok" : "text-danger"}`}>
            {socketOpen ? "LIVE" : "OFFLINE"}
          </span>
        </div>

        {/* vehicle pill — tap to swap (only when there's a fleet) */}
        <button
          onClick={() => vehicles.length >= 2 && setPickerOpen((o) => !o)}
          className="glass instrument tap relative flex flex-1 items-center gap-2 rounded-xl px-2.5 py-1"
        >
          <span
            className={`h-2.5 w-2.5 shrink-0 rounded-full ${activeT.armed ? "pulse" : ""}`}
            style={{ background: activeT.connected ? ACTIVE_COLOR : "#475569" }}
          />
          <span className="truncate text-xs font-bold" style={{ color: ACTIVE_COLOR }}>
            {activeName.toUpperCase()}
          </span>
          <span className="rounded bg-accent/15 px-1.5 py-0.5 text-[10px] font-semibold text-accent">
            {mode}
          </span>
          <div className="ml-auto flex items-center gap-1">
            <BattIcon pct={activeT.battery_pct} />
            <span className="tnum text-[11px] text-slate-200">
              {activeT.battery_pct == null ? "--" : `${activeT.battery_pct}%`}
            </span>
            {vehicles.length >= 2 && (
              <ChevronDown size={12} className="text-slate-400" />
            )}
          </div>
        </button>

        {/* 2D/3D */}
        <button
          onClick={toggle3d}
          className={`glass instrument tap flex shrink-0 items-center gap-1 rounded-xl px-2.5 ${
            view3d ? "text-accent glow-accent" : "text-slate-300"
          }`}
        >
          <Box size={14} />
          <span className="text-[10px] font-bold">{view3d ? "3D" : "2D"}</span>
        </button>

        <HelpButton />

        {/* overflow menu */}
        <button
          onClick={() => setMenuOpen((o) => !o)}
          className="glass instrument tap flex shrink-0 items-center justify-center rounded-xl px-2.5"
        >
          <MoreHorizontal size={16} className="text-slate-200" />
        </button>
      </div>

      {/* per-drone picker */}
      <AnimatePresence>
        {pickerOpen && vehicles.length >= 2 && (
          <>
            <div
              className="fixed inset-0 z-30"
              onClick={() => setPickerOpen(false)}
            />
            <motion.div
              initial={{ opacity: 0, y: -8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.16 }}
              className="glass pointer-events-auto absolute left-2 right-2 z-50 mt-1 rounded-xl p-1"
            >
              {vehicles.map((v) => {
                const t = fleetTelem[v.id] ?? EMPTY;
                const isAct = v.id === activeVehicle;
                return (
                  <button
                    key={v.id}
                    onClick={() => pickActive(v.id)}
                    className="tap flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left hover:bg-edge/40"
                  >
                    <span
                      className="h-2.5 w-2.5 rounded-full"
                      style={{ background: isAct ? ACTIVE_COLOR : OTHER_COLOR }}
                    />
                    <span
                      className="text-sm font-bold"
                      style={{ color: isAct ? ACTIVE_COLOR : OTHER_COLOR }}
                    >
                      {v.name}
                    </span>
                    <span
                      className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
                        t.connected ? "bg-accent/15 text-accent" : "bg-edge/40 text-slate-500"
                      }`}
                    >
                      {t.connected ? t.mode ?? "—" : "NO LINK"}
                    </span>
                    <div className="ml-auto flex items-center gap-1">
                      <BattIcon pct={t.battery_pct} />
                      <span className="tnum text-xs text-slate-300">
                        {t.battery_pct == null ? "--" : `${t.battery_pct}%`}
                      </span>
                    </div>
                  </button>
                );
              })}
            </motion.div>
          </>
        )}
      </AnimatePresence>

      {/* overflow menu (reports / video) */}
      <AnimatePresence>
        {menuOpen && (
          <>
            <div
              className="fixed inset-0 z-30"
              onClick={() => setMenuOpen(false)}
            />
            <motion.div
              initial={{ opacity: 0, y: -8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -8 }}
              transition={{ duration: 0.16 }}
              className="glass pointer-events-auto absolute right-2 top-14 z-50 w-48 rounded-xl p-1"
            >
              <button
                onClick={() => { setMenuOpen(false); onOpenVideo(); }}
                className="tap flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-slate-100 hover:bg-edge/40"
              >
                <Video size={15} className="text-accent" />
                <span className="text-sm">Video feeds</span>
              </button>
              <button
                onClick={() => { setMenuOpen(false); setReportOpen(true); }}
                className="tap flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-slate-100 hover:bg-edge/40"
              >
                <ClipboardList size={15} className="text-accent" />
                <span className="text-sm">Mission reports</span>
              </button>
              <button
                onClick={() => setMenuOpen(false)}
                className="tap flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-slate-500 hover:bg-edge/40"
              >
                <X size={14} />
                <span className="text-sm">Close</span>
              </button>
            </motion.div>
          </>
        )}
      </AnimatePresence>
    </div>
  );
}
