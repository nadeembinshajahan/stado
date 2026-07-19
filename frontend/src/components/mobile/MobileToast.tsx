import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, X } from "lucide-react";
import { useGcs } from "../../store/useGcs";

/**
 * Mobile snackbar — surfaces the most recent error/critical log line so a
 * command rejection is never silent. Subscribes to the shared `log` array so
 * every existing `pushLog("error", …)` (CommandBar's run(), Autotune refused,
 * voice errors) lights up automatically. No new error pipeline required.
 *
 * Dismisses on tap or after 4 s. One toast at a time (the most recent error
 * wins) — chaining toasts on a fast cockpit is hostile.
 */
export default function MobileToast() {
  const log = useGcs((s) => s.log);
  // last *error* (severity ≤ 3) we've already shown, by id, so we don't re-pop
  // when the log array re-renders for an unrelated line.
  const lastShownId = useRef<number>(0);
  const [current, setCurrent] = useState<null | { id: number; text: string }>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    // log[0] is freshest. Only fire for genuine errors so we don't toast info
    // chatter (telemetry events have no severity).
    const top = log[0];
    if (!top || top.severity == null || top.severity > 3) return;
    if (top.id <= lastShownId.current) return;
    lastShownId.current = top.id;
    const text = top.vehicle ? `${top.vehicle.toUpperCase()}: ${top.text}` : top.text;
    setCurrent({ id: top.id, text });
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCurrent(null), 4000);
  }, [log]);

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  return (
    <AnimatePresence>
      {current && (
        <motion.div
          key={current.id}
          initial={{ y: -24, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={{ y: -24, opacity: 0 }}
          transition={{ duration: 0.2, ease: "easeOut" }}
          // Pinned just below the top bar; pointer-events on the inner card only
          // so the rest of the screen stays interactive.
          className="pointer-events-none fixed left-3 right-3 z-[80] flex justify-center"
          style={{ top: "calc(env(safe-area-inset-top, 0px) + 64px)" }}
        >
          <button
            onClick={() => setCurrent(null)}
            className="tap pointer-events-auto flex max-w-[92vw] items-start gap-2 rounded-xl border border-danger/60 bg-ink/90 px-3 py-2 text-left shadow-[0_0_24px_rgba(255,77,94,0.35)] backdrop-blur"
          >
            <AlertTriangle size={16} className="mt-0.5 shrink-0 text-danger" />
            <span className="flex-1 text-sm leading-snug text-slate-100">{current.text}</span>
            <X size={14} className="mt-0.5 shrink-0 text-slate-400" />
          </button>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
