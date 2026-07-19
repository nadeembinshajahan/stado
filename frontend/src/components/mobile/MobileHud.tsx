import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { ChevronUp, ChevronDown, Satellite } from "lucide-react";
import { useGcs } from "../../store/useGcs";
import AttitudeIndicator from "../AttitudeIndicator";

const f = (n: number | null | undefined, d = 1) =>
  n == null ? "--" : n.toFixed(d);

const GPS_FIX = ["NO GPS", "NO FIX", "2D", "3D", "DGPS", "RTK-FLT", "RTK-FIX"];

/**
 * Glanceable HUD strip for mobile. Floats above the bottom sheet at a fixed
 * offset (so it stays clear of the primary command row + the PTT FAB). Always
 * shows mode + altitude + ground-speed + battery + sats. Tap to expand into a
 * compact instrument card with the attitude indicator + a 2x3 telemetry grid.
 * Critical conditions (low batt / no GPS while armed) tint the strip danger.
 */
export default function MobileHud({ bottomOffset }: { bottomOffset: number }) {
  const telem = useGcs((s) => s.telem);
  const [open, setOpen] = useState(false);
  const armed = !!telem.armed;
  const pct = telem.battery_pct;
  const volt = telem.battery_voltage;
  const battCritical =
    pct != null ? pct <= 20 : volt != null ? volt <= 14.0 : false;
  const gpsCritical =
    armed && (telem.gps_fix === 0 || telem.satellites === 0);
  const critical = battCritical || gpsCritical;

  if (!telem.connected) return null;

  return (
    <motion.div
      layout
      // Anchored bottom-left, just above the sheet's primary row. `bottomOffset`
      // is passed in by the shell so the HUD knows where the sheet's resting
      // edge currently sits (closed vs half vs full).
      style={{ bottom: bottomOffset }}
      className="pointer-events-none absolute left-2 right-2 z-30 flex justify-center"
      transition={{ type: "spring", stiffness: 380, damping: 32 }}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        className={`glass instrument tap pointer-events-auto flex max-w-[96vw] flex-col gap-1.5 rounded-xl px-3 py-2 text-left ${
          critical ? "ring-1 ring-danger/60" : ""
        }`}
      >
        {/* compact strip — always visible */}
        <div className="flex items-center gap-3">
          <span className="text-[10px] font-bold tracking-wider text-accent">
            {telem.mode ?? "—"}{armed ? " · ARMED" : ""}
          </span>
          <Stat label="ALT" value={f(telem.alt_rel, 1)} unit="m" />
          <Stat label="GS" value={f(telem.groundspeed, 1)} unit="m/s" />
          <Stat
            label="BAT"
            value={pct != null ? `${pct}` : volt != null ? volt.toFixed(1) : "--"}
            unit={pct != null ? "%" : "V"}
            critical={battCritical}
          />
          <div className="flex items-center gap-1">
            <Satellite size={11} className={(telem.gps_fix ?? 0) >= 3 ? "text-ok" : "text-warn"} />
            <span className="tnum text-[11px] text-slate-200">{telem.satellites ?? "--"}</span>
          </div>
          {open ? (
            <ChevronDown size={14} className="text-slate-400" />
          ) : (
            <ChevronUp size={14} className="text-slate-400" />
          )}
        </div>

        {/* expanded — attitude + extra telemetry */}
        <AnimatePresence>
          {open && (
            <motion.div
              initial={{ height: 0, opacity: 0 }}
              animate={{ height: "auto", opacity: 1 }}
              exit={{ height: 0, opacity: 0 }}
              transition={{ duration: 0.18 }}
              className="flex items-start gap-3 overflow-hidden border-t border-edge/60 pt-2"
            >
              <AttitudeIndicator
                roll={telem.roll ?? null}
                pitch={telem.pitch ?? null}
                size={96}
              />
              <div className="grid flex-1 grid-cols-2 gap-x-3 gap-y-1.5">
                <Stat
                  label="HDG"
                  value={telem.heading == null ? "--" : `${Math.round(telem.heading)}°`}
                  critical={gpsCritical}
                />
                <Stat label="V·SPD" value={f(telem.climb, 1)} unit="m/s" />
                <Stat label="AIR" value={f(telem.airspeed, 1)} unit="m/s" />
                <Stat
                  label="THR"
                  value={telem.throttle == null ? "--" : `${telem.throttle}%`}
                />
                <Stat label="MSL" value={f(telem.alt_msl, 0)} unit="m" />
                <Stat
                  label="GPS"
                  value={GPS_FIX[telem.gps_fix ?? 0] ?? `${telem.gps_fix ?? "--"}`}
                  critical={gpsCritical}
                />
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </button>
    </motion.div>
  );
}

function Stat({
  label,
  value,
  unit,
  critical,
}: {
  label: string;
  value: string;
  unit?: string;
  critical?: boolean;
}) {
  return (
    <div className="flex items-baseline gap-0.5">
      <span className="text-[9px] tracking-wider text-slate-400">{label}</span>
      <span className={`tnum text-xs ${critical ? "hud-critical" : "text-slate-100"}`}>
        {value}
      </span>
      {unit && <span className="text-[9px] text-slate-500">{unit}</span>}
    </div>
  );
}
