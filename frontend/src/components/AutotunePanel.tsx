import { useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CheckCircle2, Loader2, SlidersHorizontal, X, XCircle } from "lucide-react";
import { useGcs } from "../store/useGcs";
import { api } from "../lib/api";
import type { AutotuneState } from "../lib/types";

// The operator-facing safety preconditions, shown in the confirm dialog BEFORE
// any tune is started. Mirrors backend AUTOTUNE_SAFETY (api.py) so the UI, voice,
// and REST refusal all state the same gates.
const SAFETY_LINES = [
  "Drone ARMED and HOVERING in a position-hold mode (Position / Altitude).",
  "Open airspace with room to wobble on every axis.",
  "Operator ready to take MANUAL control at any moment.",
  "Runs ~40 s; the drone oscillates roll, pitch, then yaw.",
  "New gains apply automatically on landing / disarm (no save step).",
];

const EMPTY_STATE = (vehicle: string): AutotuneState => ({
  vehicle,
  state: "IDLE",
  progress: 0,
  axis: null,
  reason: null,
  statustexts: [],
  running: false,
});

/**
 * PX4 multicopter AUTOTUNE control. A single edge button opens the panel; the
 * operator picks a drone (when a fleet is present), reads the safety
 * preconditions, and confirms. Once running it shows a live progress bar (driven
 * by PX4's COMMAND_ACK progress — so it works on Outrider's DDS bridge too), the
 * current axis/state, a live STATUSTEXT feed when available (Overwatch only), a
 * Cancel button, and a clear COMPLETE / FAILED result. The note that gains apply
 * on disarm is always visible. State is fed by the WS `autotune` event.
 */
export default function AutotunePanel() {
  const vehicles = useGcs((s) => s.vehicles);
  const activeVehicle = useGcs((s) => s.activeVehicle);
  const autotune = useGcs((s) => s.autotune);
  const setAutotune = useGcs((s) => s.setAutotune);
  const pushLog = useGcs((s) => s.pushLog);

  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  // Which drone the panel targets. Default to the active drone; fall back to the
  // first vehicle so the picker always has a selection.
  const [target, setTarget] = useState<string | null>(null);

  const vehicleId = target ?? activeVehicle ?? vehicles[0]?.id ?? null;
  const vehicle = vehicles.find((v) => v.id === vehicleId);
  const at = (vehicleId && autotune[vehicleId]) || (vehicleId ? EMPTY_STATE(vehicleId) : null);
  const running = at?.state === "RUNNING";
  // Capability gate (preflight): a DDS-bridge vehicle (Outrider) can't run PX4
  // autotune over its command link — cmd 212 reaches PX4 but Commander returns
  // UNSUPPORTED, so the backend REFUSES it (422). Surface that here by disabling
  // the Run control rather than letting the operator false-start a tune. A
  // missing flag (older backend) is treated as capable so nothing is wrongly off.
  const canAutotune = vehicle ? vehicle.supports_autotune !== false : true;
  const NO_AUTOTUNE_HINT =
    "Outrider tunes via the MAVLink-on-TELEM2 procedure, not over its command link";

  // A tune is RUNNING somewhere → badge the edge button so the operator notices
  // even with the panel closed.
  const anyRunning = useMemo(
    () => Object.values(autotune).some((a) => a.state === "RUNNING"),
    [autotune],
  );

  // On open (and when the target changes) pull the current status once so the
  // panel reflects a tune already in flight (e.g. started by voice / before a
  // reload), not just future WS events.
  useEffect(() => {
    if (!open || !vehicleId) return;
    let cancelled = false;
    api.autotune
      .status(vehicleId)
      .then((s) => {
        if (cancelled || !s || "vehicles" in s) return;
        setAutotune(s);
      })
      .catch(() => {/* best-effort; WS events drive the live view */});
    return () => {
      cancelled = true;
    };
  }, [open, vehicleId, setAutotune]);

  const start = async () => {
    if (!vehicleId || busy) return;
    setBusy(true);
    try {
      const res = await api.autotune.start(vehicleId);
      if (res?.ok === false) {
        pushLog("error", `Autotune refused: ${(res as { reason?: string }).reason ?? "rejected"}`, 3, vehicleId);
      }
      setConfirming(false);
    } catch (e) {
      // The backend 409s (offline / confirm) come back as a thrown error here.
      pushLog("error", `Autotune: ${(e as Error).message}`, 3, vehicleId);
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (!vehicleId || busy) return;
    setBusy(true);
    try {
      await api.autotune.cancel(vehicleId);
    } catch (e) {
      pushLog("error", `Autotune cancel: ${(e as Error).message}`, 3, vehicleId);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {/* edge button — left rail, below the points affordance */}
      <button
        onClick={() => setOpen((v) => !v)}
        title="Autotune (PX4 rate-controller tune)"
        className={`glass absolute left-3 top-[15.5rem] z-20 rounded-lg p-2 ${
          open || anyRunning ? "text-accent glow-accent" : "text-slate-300"
        }`}
      >
        <div className="relative">
          <SlidersHorizontal size={18} />
          {anyRunning && (
            <span className="absolute -right-1.5 -top-1.5 flex h-3 w-3">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-75" />
              <span className="relative inline-flex h-3 w-3 rounded-full bg-accent" />
            </span>
          )}
        </div>
      </button>

      <AnimatePresence>
        {open && vehicleId && at && (
          <motion.div
            initial={{ x: -16, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            exit={{ x: -16, opacity: 0 }}
            transition={{ duration: 0.16, ease: "easeOut" }}
            className="glass absolute left-16 top-[15.5rem] z-30 w-80 rounded-xl p-3 text-slate-100"
          >
            <div className="mb-2 flex items-center justify-between">
              <div className="flex items-center gap-2 text-sm font-semibold">
                <SlidersHorizontal size={15} className="text-accent" />
                Autotune
              </div>
              <button onClick={() => setOpen(false)} className="text-slate-400 hover:text-slate-100">
                <X size={15} />
              </button>
            </div>

            {/* drone picker (only with a fleet) */}
            {vehicles.length >= 2 && (
              <div className="mb-2 flex gap-1">
                {vehicles.map((v) => {
                  const vCan = v.supports_autotune !== false;
                  return (
                    <button
                      key={v.id}
                      onClick={() => {
                        setTarget(v.id);
                        setConfirming(false);
                      }}
                      title={vCan ? undefined : NO_AUTOTUNE_HINT}
                      className={`flex-1 rounded-md px-2 py-1 text-[11px] font-semibold transition-colors ${
                        v.id === vehicleId
                          ? "bg-accent/25 text-accent"
                          : "bg-ink/60 text-slate-400 hover:text-slate-200"
                      }`}
                    >
                      {v.name}
                      {vCan ? "" : " ⃠"}
                    </button>
                  );
                })}
              </div>
            )}

            {/* ── RUNNING: progress + axis + cancel ─────────────────────────── */}
            {running ? (
              <div className="space-y-2">
                <div className="flex items-center justify-between text-[11px]">
                  <span className="flex items-center gap-1.5 text-accent">
                    <Loader2 size={12} className="animate-spin" /> Tuning
                    {at.axis && at.axis !== "done" ? ` — ${at.axis}` : ""}
                  </span>
                  <span className="tnum font-semibold">{at.progress}%</span>
                </div>
                <div className="h-2 w-full overflow-hidden rounded-full bg-ink/70">
                  <div
                    className="h-full rounded-full bg-accent transition-[width] duration-300"
                    style={{ width: `${Math.max(2, Math.min(100, at.progress))}%` }}
                  />
                </div>
                {/* live STATUSTEXT feed — Overwatch only; stays empty on Outrider */}
                {at.statustexts.length > 0 && (
                  <div className="max-h-20 overflow-y-auto rounded-md bg-ink/50 p-1.5 text-[10px] text-slate-300">
                    {at.statustexts.slice(-6).map((s, i) => (
                      <div key={i} className="truncate font-mono">
                        {s.text}
                      </div>
                    ))}
                  </div>
                )}
                <button
                  onClick={cancel}
                  disabled={busy}
                  className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-danger/15 py-1.5 text-[12px] font-semibold text-danger hover:bg-danger/25 disabled:opacity-50"
                >
                  {busy ? <Loader2 size={13} className="animate-spin" /> : <XCircle size={13} />}
                  Cancel autotune
                </button>
              </div>
            ) : at.state === "COMPLETE" ? (
              <div className="space-y-2">
                <div className="flex items-center gap-1.5 text-[12px] font-semibold text-accent">
                  <CheckCircle2 size={14} /> Autotune complete
                </div>
                <p className="text-[11px] leading-snug text-slate-300">
                  New rate-controller gains computed. They apply automatically on the
                  next landing / disarm.
                </p>
                <button
                  onClick={() => setConfirming(true)}
                  disabled={!canAutotune}
                  title={canAutotune ? undefined : NO_AUTOTUNE_HINT}
                  className="w-full rounded-lg bg-edge/40 py-1.5 text-[12px] font-semibold hover:bg-edge/70 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  Run again
                </button>
              </div>
            ) : at.state === "FAILED" ? (
              <div className="space-y-2">
                <div className="flex items-center gap-1.5 text-[12px] font-semibold text-danger">
                  <XCircle size={14} /> Autotune failed
                </div>
                <p className="text-[11px] leading-snug text-slate-300">{at.reason ?? "PX4 aborted the tune."}</p>
                <button
                  onClick={() => setConfirming(true)}
                  disabled={!canAutotune}
                  title={canAutotune ? undefined : NO_AUTOTUNE_HINT}
                  className="w-full rounded-lg bg-edge/40 py-1.5 text-[12px] font-semibold hover:bg-edge/70 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  Try again
                </button>
              </div>
            ) : confirming ? (
              /* ── CONFIRM dialog: safety preconditions + explicit confirm ──── */
              <div className="space-y-2">
                <div className="flex items-center gap-1.5 text-[12px] font-semibold text-warn">
                  <AlertTriangle size={14} /> In-flight maneuver — confirm
                </div>
                <ul className="space-y-1 text-[11px] leading-snug text-slate-300">
                  {SAFETY_LINES.map((line) => (
                    <li key={line} className="flex gap-1.5">
                      <span className="text-warn">•</span>
                      <span>{line}</span>
                    </li>
                  ))}
                </ul>
                <div className="flex gap-2 pt-1">
                  <button
                    onClick={() => setConfirming(false)}
                    className="flex-1 rounded-lg bg-edge/40 py-1.5 text-[12px] font-semibold hover:bg-edge/70"
                  >
                    Cancel
                  </button>
                  <button
                    onClick={start}
                    disabled={busy}
                    className="flex flex-1 items-center justify-center gap-1.5 rounded-lg bg-accent/20 py-1.5 text-[12px] font-bold text-accent hover:bg-accent/30 disabled:opacity-50"
                  >
                    {busy ? <Loader2 size={13} className="animate-spin" /> : null}
                    Confirm &amp; tune {vehicle?.name ?? ""}
                  </button>
                </div>
              </div>
            ) : (
              /* ── IDLE: the entry point ─────────────────────────────────────── */
              <div className="space-y-2">
                <p className="text-[11px] leading-snug text-slate-400">
                  Excites each axis in flight to compute new PID gains. Gains apply on
                  disarm. {!canAutotune ? (
                    <span className="text-warn">{NO_AUTOTUNE_HINT}.</span>
                  ) : vehicle && !vehicle.connected ? (
                    <span className="text-danger">{vehicle.name} is offline.</span>
                  ) : null}
                </p>
                <button
                  onClick={() => setConfirming(true)}
                  disabled={!canAutotune || (vehicle ? !vehicle.connected : false)}
                  title={canAutotune ? undefined : NO_AUTOTUNE_HINT}
                  className="w-full rounded-lg bg-accent/20 py-1.5 text-[12px] font-bold text-accent hover:bg-accent/30 disabled:opacity-40 disabled:cursor-not-allowed"
                >
                  Run Autotune{vehicle ? ` — ${vehicle.name}` : ""}
                </button>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
