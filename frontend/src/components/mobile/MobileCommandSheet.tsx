import { useEffect, useRef, useState } from "react";
import { motion, useAnimation, useDragControls, type PanInfo } from "framer-motion";
import {
  AlertTriangle, Ban, ChevronUp, Crosshair, Hand, Home, Map as MapIcon,
  MessagesSquare, Navigation, Octagon, PlaneTakeoff, Power, ScanEye,
  SlidersHorizontal, Terminal,
} from "lucide-react";
import { api } from "../../lib/api";
import { useGcs, type Mode } from "../../store/useGcs";

// --- snap geometry ----------------------------------------------------------
// `closed` exposes just the primary row above the home indicator. `half`
// reveals the secondary tray. `full` adds the console + conversation log.
// Heights are computed at runtime so we adapt to dvh + safe-area.
type Snap = "closed" | "half" | "full";

const MODES: { id: Mode; icon: React.ReactNode; label: string }[] = [
  { id: "navigate", icon: <Navigation size={14} />, label: "NAV" },
  { id: "survey", icon: <MapIcon size={14} />, label: "SURVEY" },
  { id: "track", icon: <Crosshair size={14} />, label: "TRACK" },
];

// PX4 modes a re-takeoff is REJECTED from (mirrors the desktop CommandBar).
const NO_RETAKEOFF_MODES = new Set(["AUTO.RTL", "AUTO.LAND", "RTL", "LAND"]);

/** In-sheet PX4 autotune launcher. Read-only mirror of `AutotunePanel`'s state
 *  machine (IDLE / CONFIRM / RUNNING / COMPLETE / FAILED) using the shared
 *  store + api. The Outrider DDS-bridge case still surfaces a refusal — same
 *  text as the desktop panel — so the operator isn't confused. */
function MobileAutotuneCard() {
  const vehicles = useGcs((s) => s.vehicles);
  const activeVehicle = useGcs((s) => s.activeVehicle);
  const autotune = useGcs((s) => s.autotune);
  const pushLog = useGcs((s) => s.pushLog);
  const target = activeVehicle ?? vehicles[0]?.id ?? null;
  const veh = vehicles.find((v) => v.id === target);
  const at = target ? autotune[target] : undefined;
  const running = at?.state === "RUNNING";
  const canAutotune = veh ? veh.supports_autotune !== false : true;
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);

  const start = async () => {
    if (!target || busy) return;
    setBusy(true);
    try {
      const res = await api.autotune.start(target);
      if (res?.ok === false) {
        pushLog(
          "error",
          `Autotune refused: ${(res as { reason?: string }).reason ?? "rejected"}`,
          3,
          target,
        );
      }
      setConfirming(false);
    } catch (e) {
      pushLog("error", `Autotune: ${(e as Error).message}`, 3, target);
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (!target || busy) return;
    setBusy(true);
    try {
      await api.autotune.cancel(target);
    } catch (e) {
      pushLog("error", `Autotune cancel: ${(e as Error).message}`, 3, target);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-3 rounded-lg bg-ink/50 p-2">
      <div className="flex items-center gap-2">
        <SlidersHorizontal size={14} className="text-accent" />
        <span className="text-xs font-bold text-slate-200">
          Autotune{veh ? ` — ${veh.name}` : ""}
        </span>
        {running && (
          <span className="ml-auto tnum text-[11px] font-bold text-accent">
            {at?.progress ?? 0}%
          </span>
        )}
      </div>
      {running ? (
        <>
          <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-edge/40">
            <div
              className="h-full rounded-full bg-accent transition-[width]"
              style={{ width: `${Math.max(2, Math.min(100, at?.progress ?? 0))}%` }}
            />
          </div>
          <button
            onClick={cancel}
            disabled={busy}
            className="tap mt-2 w-full rounded-lg bg-danger/20 py-1.5 text-[11px] font-bold text-danger disabled:opacity-50"
          >
            Cancel autotune
          </button>
        </>
      ) : confirming ? (
        <div className="mt-2 space-y-1.5">
          <div className="text-[10px] leading-snug text-warn">
            In-flight maneuver — drone must be ARMED + HOVERING in PosHold. Runs ~40 s.
          </div>
          <div className="flex gap-2">
            <button
              onClick={() => setConfirming(false)}
              className="tap flex-1 rounded-lg bg-edge/50 py-1.5 text-[11px] font-bold text-slate-100"
            >
              Cancel
            </button>
            <button
              onClick={start}
              disabled={busy}
              className="tap flex-1 rounded-lg bg-accent/25 py-1.5 text-[11px] font-bold text-accent disabled:opacity-50"
            >
              {busy ? "…" : "Confirm & tune"}
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={() => setConfirming(true)}
          disabled={!canAutotune || !veh?.connected}
          title={canAutotune ? undefined : "Outrider tunes via TELEM2, not over its command link"}
          className="tap mt-2 w-full rounded-lg bg-accent/15 py-1.5 text-[11px] font-bold text-accent disabled:cursor-not-allowed disabled:opacity-40"
        >
          {!canAutotune
            ? "Not supported on this drone"
            : !veh?.connected
              ? "Drone offline"
              : `Run Autotune${at?.state === "COMPLETE" ? " again" : at?.state === "FAILED" ? " (retry)" : ""}`}
        </button>
      )}
      {at?.state === "FAILED" && (
        <div className="mt-1 text-[10px] text-danger">{at.reason ?? "PX4 aborted the tune."}</div>
      )}
    </div>
  );
}

/**
 * Big touch-friendly action button. Tones map to the desktop convention so
 * the operator's color memory transfers (danger = red, warn = amber, go =
 * teal). 64pt min height clears the 44pt iOS HIG floor comfortably.
 */
function ActionBtn({
  icon,
  label,
  tone = "default",
  onClick,
  disabled,
}: {
  icon: React.ReactNode;
  label: string;
  tone?: "default" | "go" | "warn" | "danger";
  onClick: () => void;
  disabled?: boolean;
}) {
  const tones: Record<string, string> = {
    default: "bg-edge/60 text-slate-100",
    go: "bg-accent/25 text-accent glow-accent",
    warn: "bg-warn/15 text-warn",
    danger: "bg-danger/20 text-danger",
  };
  return (
    <motion.button
      whileTap={{ scale: 0.94 }}
      onClick={onClick}
      disabled={disabled}
      className={`tap flex flex-1 flex-col items-center justify-center gap-1 rounded-xl py-3 text-center ${tones[tone]} disabled:opacity-40`}
      style={{ minHeight: 64 }}
    >
      {icon}
      <span className="text-[11px] font-bold tracking-wide">{label}</span>
    </motion.button>
  );
}

/**
 * Bottom sheet — primary flight commands + secondary tray. Pinned at the
 * bottom safe area; drag the header up/down to snap closed/half/full. The
 * primary row (ARM/TAKEOFF/HOLD/BRAKE/RTL/LAND) is ALWAYS visible because
 * those are the emergency-reachable commands. Force-disarm lives in the
 * secondary tray behind a confirm card (same gate logic as desktop).
 *
 * Reports the current resting bottom-offset via `onHeightChange` so the HUD
 * strip + PTT FAB can sit just above it without overlap.
 */
export default function MobileCommandSheet({
  onHeightChange,
}: {
  onHeightChange: (px: number) => void;
}) {
  const { uiMode, setMode } = useGcs();
  const fleetTelem = useGcs((s) => s.fleetTelem);
  const activeVehicle = useGcs((s) => s.activeVehicle);
  const pushLog = useGcs((s) => s.pushLog);
  const vehicles = useGcs((s) => s.vehicles);
  const conversation = useGcs((s) => s.conversation);
  const log = useGcs((s) => s.log);
  const surveyPreview = useGcs((s) => s.surveyPreview);
  const setSurveyPreview = useGcs((s) => s.setSurveyPreview);

  const activeName =
    vehicles.find((v) => v.id === activeVehicle)?.name ?? "the active drone";

  // --- snap state -----------------------------------------------------------
  const [snap, setSnap] = useState<Snap>("closed");
  const [sheetHeight, setSheetHeight] = useState<number>(window.innerHeight);
  const controls = useAnimation();
  const dragControls = useDragControls();
  const sheetRef = useRef<HTMLDivElement>(null);

  // Heights for each snap point (px). The full sheet is the natural content
  // height (capped); half exposes the primary row + the secondary tabs; closed
  // exposes only the primary row. We recompute on resize so a rotation works.
  const computeY = (s: Snap, vh: number): number => {
    // y is the translation from "fully open" (y=0 → sheet top at top edge of
    // its container). A larger y pushes it DOWN, hiding more of it.
    if (s === "full") return Math.max(0, vh - Math.min(640, vh * 0.92));
    if (s === "half") return Math.max(0, vh - Math.min(420, vh * 0.62));
    // closed = primary row + handle only (≈160px including safe area)
    return Math.max(0, vh - 168);
  };

  useEffect(() => {
    const measure = () => setSheetHeight(window.innerHeight);
    measure();
    window.addEventListener("resize", measure);
    window.addEventListener("orientationchange", measure);
    return () => {
      window.removeEventListener("resize", measure);
      window.removeEventListener("orientationchange", measure);
    };
  }, []);

  // Animate to the current snap point whenever it (or the viewport) changes.
  useEffect(() => {
    const y = computeY(snap, sheetHeight);
    void controls.start({ y, transition: { type: "spring", stiffness: 380, damping: 38 } });
    // Tell the parent how many px from the bottom are "covered" by the sheet —
    // the HUD chip + PTT FAB use this to stay above the resting edge.
    onHeightChange(sheetHeight - y);
  }, [snap, sheetHeight, controls, onHeightChange]);

  const onDragEnd = (_: unknown, info: PanInfo) => {
    // Pick the nearest snap weighted by velocity (so a flick beats a small
    // drag). Velocity is positive when moving down.
    const yNow = computeY(snap, sheetHeight) + info.offset.y;
    const candidates: Snap[] = ["closed", "half", "full"];
    const dists = candidates.map((s) => Math.abs(computeY(s, sheetHeight) - yNow));
    // Bias by velocity — a quick downward flick pulls us closer to "closed".
    const v = info.velocity.y;
    const biasedIdx =
      v > 600 ? 0 : v < -600 ? 2 : dists.indexOf(Math.min(...dists));
    setSnap(candidates[biasedIdx]);
  };

  // --- commands -------------------------------------------------------------
  const run = (label: string, fn: () => Promise<unknown>) => async () => {
    try {
      await fn();
    } catch (e) {
      pushLog("error", `${label}: ${(e as Error).message}`, 3);
    }
  };

  const anyArmed = Object.values(fleetTelem).some((t) => t.connected && t.armed);
  const activeMode =
    activeVehicle ? fleetTelem[activeVehicle]?.mode ?? null : null;
  const needsHoldBeforeTakeoff =
    activeMode != null && NO_RETAKEOFF_MODES.has(activeMode);

  const armToggle = run(anyArmed ? "disarm" : "arm", async () => {
    const verb = anyArmed ? "Disarm" : "Arm";
    const res = (await (anyArmed ? api.disarm("all") : api.arm("all"))) as
      | { ok?: boolean; armed?: boolean; reason?: string }
      | null;
    if (res && res.ok === false) {
      pushLog("error", `${verb} denied: ${res.reason || "rejected"}`, 3);
    }
  });

  const [alt, setAlt] = useState(15);
  const [forceConfirm, setForceConfirm] = useState(false);

  return (
    <motion.div
      ref={sheetRef}
      animate={controls}
      initial={{ y: sheetHeight - 168 }}
      drag="y"
      // Only the explicit drag handle area initiates a drag — the inner content
      // is free to scroll/tap without hijacking the gesture.
      dragControls={dragControls}
      dragListener={false}
      dragConstraints={{ top: 0, bottom: sheetHeight - 100 }}
      dragElastic={0.04}
      dragMomentum={false}
      onDragEnd={onDragEnd}
      className="glass safe-bottom fixed inset-x-0 bottom-0 z-50 flex flex-col rounded-t-2xl shadow-[0_-12px_36px_rgba(0,0,0,0.6)]"
      style={{ height: sheetHeight }}
    >
      {/* drag handle — wide 60pt hit area; tap cycles snap, drag re-positions */}
      <div
        className="tap flex items-center justify-center pt-3 pb-2"
        style={{ touchAction: "none" }}
        onPointerDown={(e) => dragControls.start(e)}
        onClick={() => setSnap((s) => (s === "closed" ? "half" : s === "half" ? "full" : "closed"))}
      >
        <div className="h-1.5 w-12 rounded-full bg-slate-500/70" />
      </div>

      {/* primary row — ALWAYS visible */}
      <div className="px-3 pb-2">
        {/* Re-takeoff hint */}
        {needsHoldBeforeTakeoff && (
          <div className="mb-2 flex items-center gap-2 rounded-lg bg-warn/15 px-2 py-1.5 text-[11px] text-warn">
            <AlertTriangle size={13} />
            <span>
              In {activeMode} — set HOLD before re-takeoff.
            </span>
            <button
              onClick={run("hold", () => api.hold(activeVehicle ?? undefined))}
              className="ml-auto rounded bg-warn/25 px-2 py-0.5 text-[11px] font-bold"
            >
              Set HOLD
            </button>
          </div>
        )}

        <div className="flex gap-2">
          <ActionBtn
            tone={anyArmed ? "danger" : "default"}
            icon={<Power size={20} />}
            label={anyArmed ? "DISARM" : "ARM"}
            onClick={armToggle}
          />
          <ActionBtn
            tone="go"
            icon={<PlaneTakeoff size={20} />}
            label="TAKEOFF"
            onClick={run("takeoff", () =>
              api.takeoff(alt, activeVehicle ?? undefined),
            )}
          />
          <ActionBtn
            tone="warn"
            icon={<Hand size={20} />}
            label="HOLD"
            onClick={run("hold", () => api.hold("all"))}
          />
          <ActionBtn
            tone="warn"
            icon={<Home size={20} />}
            label="RTL"
            onClick={run("rtl", () => api.rtl("all"))}
          />
          <ActionBtn
            tone="danger"
            icon={<ScanEye size={20} />}
            label="LAND"
            onClick={run("land", () => api.land("all"))}
          />
        </div>

        {/* takeoff altitude stepper — compact, inline */}
        <div className="mt-2 flex items-center justify-between rounded-lg bg-ink/60 px-2 py-1">
          <span className="text-[10px] font-bold tracking-wider text-slate-400">
            TAKEOFF ALT
          </span>
          <div className="flex items-center gap-1.5">
            <button
              onClick={() => setAlt((a) => Math.max(2, a - 1))}
              className="tap rounded bg-edge/50 px-2 py-0.5 text-sm font-bold text-slate-100"
            >
              −
            </button>
            <span className="tnum w-12 text-center text-sm font-bold text-accent">
              {alt} m
            </span>
            <button
              onClick={() => setAlt((a) => Math.min(120, a + 1))}
              className="tap rounded bg-edge/50 px-2 py-0.5 text-sm font-bold text-slate-100"
            >
              +
            </button>
          </div>
        </div>
      </div>

      {/* secondary tray — visible when half/full */}
      <div className="flex-1 overflow-y-auto px-3 pb-3">
        {/* UI mode tabs */}
        <div className="mt-1 flex gap-1 rounded-lg bg-ink/60 p-1">
          {MODES.map((m) => (
            <button
              key={m.id}
              onClick={() => setMode(m.id)}
              className={`tap flex flex-1 items-center justify-center gap-1 rounded-md py-1.5 text-[11px] font-bold transition-colors ${
                uiMode === m.id
                  ? "bg-accent/25 text-accent"
                  : "text-slate-300"
              }`}
            >
              {m.icon}
              {m.label}
            </button>
          ))}
        </div>

        {/* BRAKE + FORCE — secondary critical row */}
        <div className="mt-2 flex gap-2">
          <ActionBtn
            tone="warn"
            icon={<Octagon size={18} />}
            label="BRAKE"
            onClick={run("brake", () => api.brake("all"))}
          />
          {/* FORCE-DISARM — emergency, confirm-gated. Targets the ACTIVE drone
              only (never "all" — must not cut a healthy flying drone). */}
          <ActionBtn
            tone="danger"
            icon={<Ban size={18} />}
            label="FORCE-DISARM"
            onClick={() => setForceConfirm(true)}
          />
        </div>

        {forceConfirm && (
          <motion.div
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            className="mt-2 rounded-xl border border-danger/50 bg-danger/10 p-3 text-center"
          >
            <div className="text-xs font-bold text-danger">
              Force-disarm {activeName}?
            </div>
            <div className="mt-1 text-[11px] leading-snug text-slate-300">
              Cuts motors IMMEDIATELY — even in flight. Emergency only:
              bypasses PX4's "not landed" block (the 2026-05-26 stuck-armed cause).
            </div>
            <div className="mt-2 flex gap-2">
              <button
                onClick={() => setForceConfirm(false)}
                className="tap flex-1 rounded-lg bg-edge/50 py-2 text-xs font-bold text-slate-100"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  setForceConfirm(false);
                  void run("force-disarm", () =>
                    api.forceDisarm(activeVehicle ?? undefined),
                  )();
                }}
                className="tap flex-1 rounded-lg bg-danger/40 py-2 text-xs font-bold text-danger"
              >
                FORCE-DISARM
              </button>
            </div>
          </motion.div>
        )}

        {/* Survey "Confirm & fly" gate — surfaces when a survey has been staged
            (tap-path or voice), mirrors the desktop ActionCard's commit gate. */}
        {surveyPreview && (
          <div className="mt-2 rounded-xl border border-accent/50 bg-accent/10 p-3">
            <div className="flex items-center gap-2 text-xs font-bold text-accent">
              <MapIcon size={14} /> Survey staged
            </div>
            <div className="mt-1 text-[11px] leading-snug text-slate-300">
              {surveyPreview.waypoints} waypoints planned.
            </div>
            <div className="mt-2 flex gap-2">
              <button
                onClick={() => {
                  setSurveyPreview(null);
                  void run("survey-cancel", () => api.surveyCancel())();
                }}
                className="tap flex-1 rounded-lg bg-edge/50 py-2 text-xs font-bold text-slate-100"
              >
                Cancel
              </button>
              <button
                onClick={() => {
                  void run("survey-commit", () => api.surveyCommit())();
                }}
                className="tap flex-1 rounded-lg bg-accent/30 py-2 text-xs font-bold text-accent"
              >
                Confirm & fly
              </button>
            </div>
          </div>
        )}

        {/* Autotune — a compact in-sheet launcher. Mobile autotune is rare (most
            operators tune on desktop where the live STATUSTEXT feed is visible),
            so we expose just the start + cancel actions plus the live progress
            bar via the shared store. Confirmation is in-line; cancellation
            mid-tune is a single tap. */}
        <MobileAutotuneCard />

        {/* Conversation tail — last 3 turns, compact */}
        <div className="mt-3">
          <div className="mb-1 flex items-center gap-1.5 text-[10px] font-bold tracking-wider text-slate-400">
            <MessagesSquare size={11} className="text-accent" />
            CONVERSATION
          </div>
          <div className="space-y-1 rounded-lg bg-ink/50 p-2 text-[11px]">
            {conversation.length === 0 ? (
              <span className="text-slate-500">Hold the mic and speak…</span>
            ) : (
              conversation.slice(-3).map((c) => (
                <div
                  key={c.id}
                  className={`truncate ${
                    c.role === "user"
                      ? "text-slate-200"
                      : c.role === "assistant"
                        ? "text-accent"
                        : "font-mono text-slate-400"
                  }`}
                >
                  {c.role === "tool"
                    ? `▸ ${c.tool?.name}${c.tool?.ok === false ? " ✗" : c.tool?.ok ? " ✓" : ""}`
                    : c.text}
                </div>
              ))
            )}
          </div>
        </div>

        {/* Console tail — last 5 lines */}
        <div className="mt-3">
          <div className="mb-1 flex items-center gap-1.5 text-[10px] font-bold tracking-wider text-slate-400">
            <Terminal size={11} className="text-accent" />
            CONSOLE
          </div>
          <div className="space-y-0.5 rounded-lg bg-ink/50 p-2 font-mono text-[10px]">
            {log.length === 0 ? (
              <span className="text-slate-500">awaiting telemetry…</span>
            ) : (
              log.slice(0, 5).map((e) => (
                <div
                  key={e.id}
                  className={`truncate ${
                    e.severity != null && e.severity <= 3
                      ? "text-danger"
                      : e.severity === 4
                        ? "text-warn"
                        : "text-slate-300"
                  }`}
                >
                  {e.vehicle ? `[${e.vehicle.toUpperCase()}] ` : ""}
                  {e.text}
                </div>
              ))
            )}
          </div>
        </div>

        {/* small footer affordance — drag-up cue */}
        {snap !== "full" && (
          <button
            onClick={() => setSnap(snap === "closed" ? "half" : "full")}
            className="tap mt-3 flex w-full items-center justify-center gap-1 rounded-lg py-2 text-[11px] font-bold text-slate-500"
          >
            <ChevronUp size={12} />
            {snap === "closed" ? "Drag up for more" : "Show full log"}
          </button>
        )}
      </div>
    </motion.div>
  );
}
