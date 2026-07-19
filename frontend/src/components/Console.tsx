import { useGcs } from "../store/useGcs";
import { SEVERITY } from "../lib/ws";

// Vehicle id → signature color class for the per-line prefix tag. Matches the
// fleet color convention used in the HUD/StatusBar (Overwatch = cockpit teal
// `text-accent`, Outrider = amber `text-amber-400`). Unknown ids fall back to a
// neutral slate so a new drone still gets a readable, labeled prefix.
const VEHICLE_COLOR: Record<string, string> = {
  overwatch: "text-accent",
  outrider: "text-amber-400",
};
const vehicleColor = (id: string) => VEHICLE_COLOR[id.toLowerCase()] ?? "text-slate-400";

/** Event console — vehicle/link/mode/mission log for the active drone. Lives in
 *  its own panel (below the conversation window) so the HUD stays clean. */
export default function Console() {
  const log = useGcs((s) => s.log);
  return (
    <div className="glass instrument w-96 rounded-xl p-3">
      <span className="text-[10px] text-slate-400 tracking-wider">CONSOLE</span>
      <div className="mt-1 h-28 overflow-y-auto text-[11px] font-mono space-y-0.5">
        {log.length === 0 && <div className="text-slate-600">awaiting telemetry…</div>}
        {log.map((e) => (
          <div key={e.id} className="flex gap-1.5">
            <span className="text-slate-600">
              {new Date(e.ts).toLocaleTimeString([], { hour12: false })}
            </span>
            <span
              className={
                e.severity != null && e.severity <= 3
                  ? "text-danger"
                  : e.severity === 4
                    ? "text-warn"
                    : "text-slate-300"
              }
            >
              {e.severity != null && e.severity <= 4 ? `[${SEVERITY[e.severity]}] ` : ""}
              {e.vehicle ? (
                <span className={`font-bold ${vehicleColor(e.vehicle)}`}>
                  {e.vehicle.toUpperCase()}{" "}
                </span>
              ) : null}
              {e.text}
              {e.count && e.count > 1 ? <span className="text-slate-500"> ×{e.count}</span> : null}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
