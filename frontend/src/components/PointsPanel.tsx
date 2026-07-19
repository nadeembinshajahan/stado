import { useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { MapPin, Trash2, X } from "lucide-react";
import { useMap } from "@vis.gl/react-google-maps";
import { useGcs } from "../store/useGcs";

/**
 * Operator points-of-interest manager. Lists every dropped POI with a per-point
 * delete (×) and a "Clear all points" control. Works alongside the in-map
 * affordances (right-click a 2D marker also removes it). Pois are persisted in
 * the store, so deletions survive a refresh and re-sync to the backend.
 *
 * `useMap()` is optional — when rendered inside the 2D Map it pans to a POI on
 * click; in 3D (no map context) that is a no-op.
 */
export default function PointsPanel() {
  const pois = useGcs((s) => s.pois);
  const removePoi = useGcs((s) => s.removePoi);
  const clearPois = useGcs((s) => s.clearPois);
  const [open, setOpen] = useState(false);
  // null when rendered outside a 2D <Map> (e.g. the 3D view) — pan becomes a no-op.
  const map = useMap();

  return (
    <>
      {/* Points affordance — sits below the Fleet search-area button on the left
          edge (the rail order is: map controls → search areas → points). */}
      <button
        onClick={() => setOpen((v) => !v)}
        title="Marked points"
        className={`glass absolute left-3 top-[12.5rem] z-20 rounded-lg p-2 ${
          open || pois.length ? "text-accent glow-accent" : "text-slate-300"
        }`}
      >
        <div className="relative">
          <MapPin size={18} />
          {pois.length > 0 && (
            <span className="absolute -right-2 -top-2 flex h-4 min-w-4 items-center justify-center rounded-full bg-accent px-1 text-[9px] font-bold text-ink">
              {pois.length}
            </span>
          )}
        </div>
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ x: -12, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: -12, opacity: 0 }}
            className="glass absolute left-[20rem] top-[12.5rem] z-20 w-60 rounded-xl p-3 flex flex-col gap-2"
          >
            <div className="flex items-center justify-between">
              <span className="text-[11px] uppercase tracking-wide text-accent font-semibold">
                Marked Points
              </span>
              <button className="text-slate-500 hover:text-slate-200" onClick={() => setOpen(false)}>
                <X size={14} />
              </button>
            </div>

            {pois.length === 0 ? (
              <p className="text-[11px] leading-tight text-slate-500">
                No points yet. Click the map and choose <b>Mark</b> to drop a
                persistent named point.
              </p>
            ) : (
              <>
                <div className="flex max-h-56 flex-col gap-1 overflow-y-auto">
                  {pois.map((p) => (
                    <div
                      key={p.id}
                      className="group flex items-center gap-2 rounded-md bg-edge/30 px-2 py-1.5"
                    >
                      <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-[#b07cff]" />
                      <button
                        className="min-w-0 flex-1 text-left"
                        onClick={() => map?.panTo({ lat: p.lat, lng: p.lng })}
                        title="Center map on this point"
                      >
                        <span className="block truncate text-xs font-semibold text-slate-100">
                          {p.name}
                        </span>
                        <span className="tnum block text-[10px] text-slate-500">
                          {p.lat.toFixed(5)}, {p.lng.toFixed(5)}
                        </span>
                      </button>
                      <button
                        onClick={() => removePoi(p.id)}
                        className="shrink-0 text-slate-500 transition-colors hover:text-danger"
                        title="Delete this point"
                      >
                        <X size={14} />
                      </button>
                    </div>
                  ))}
                </div>
                <button
                  onClick={clearPois}
                  className="flex items-center justify-center gap-1.5 rounded-md bg-edge/40 px-3 py-1.5 text-[11px] font-semibold text-slate-300 transition-colors hover:bg-danger/20 hover:text-danger"
                >
                  <Trash2 size={13} /> Clear all points
                </button>
              </>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
