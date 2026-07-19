import { motion, AnimatePresence } from "framer-motion";
import { BatteryWarning, X } from "lucide-react";
import { useGcs } from "../store/useGcs";
import { api } from "../lib/api";

/** Smart-RTL notification. Raised by the backend "low_battery" event when an
 *  armed drone hits the battery floor. STADO also speaks it and asks to confirm
 *  an RTL by voice; this banner is the visual notification + a manual override. */
export default function LowBatteryBanner() {
  const alert = useGcs((s) => s.lowBatteryAlert);
  const dismiss = useGcs((s) => s.setLowBatteryAlert);

  return (
    <AnimatePresence>
      {alert && (
        <motion.div
          initial={{ y: -80, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={{ y: -80, opacity: 0 }}
          className="absolute left-1/2 top-16 z-50 -translate-x-1/2"
        >
          <div className="flex items-center gap-3 rounded-xl border border-danger/60 bg-danger/20 px-4 py-2.5 backdrop-blur-md shadow-[0_0_28px_rgba(239,68,68,0.4)]">
            <BatteryWarning size={22} className="text-danger pulse" />
            <div className="text-sm leading-tight">
              <div>
                <span className="font-bold text-danger">LOW BATTERY</span>{" "}
                <span className="text-slate-100">
                  {alert.name} at {Math.round(alert.battery_pct)}% (floor {Math.round(alert.threshold)}%)
                </span>
              </div>
              <div className="text-[11px] text-slate-300">
                STADO is asking to confirm RTL — say “yes” to return home.
              </div>
            </div>
            <button
              onClick={() => {
                // RTL the drone that's actually low (the alert is per-vehicle),
                // not the active/default one.
                api.rtl(alert.vehicle).catch(() => {});
                dismiss(null);
              }}
              className="rounded-md bg-danger/30 px-3 py-1 text-xs font-semibold text-danger hover:bg-danger/40"
              title="Return to launch now"
            >
              RTL NOW
            </button>
            <button
              onClick={() => dismiss(null)}
              className="text-slate-400 hover:text-slate-200"
              title="Dismiss"
            >
              <X size={16} />
            </button>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
