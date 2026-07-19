import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import {
  Ban, Hand, Home, Octagon, PlaneTakeoff, Power, ScanEye, Map as MapIcon,
  Navigation, Crosshair,
} from "lucide-react";
import { api } from "../lib/api";
import { useGcs, type Mode } from "../store/useGcs";
import VoiceButton from "./VoiceButton";

function Btn({
  onClick, icon, label, tone = "default", wide = false, disabled = false,
  title,
}: {
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
  tone?: "default" | "go" | "warn" | "danger";
  wide?: boolean;
  disabled?: boolean;
  title?: string;
}) {
  const tones: Record<string, string> = {
    default: "bg-edge/40 hover:bg-edge/70 text-slate-100",
    go: "bg-accent/20 hover:bg-accent/30 text-accent glow-accent",
    warn: "bg-warn/15 hover:bg-warn/25 text-warn",
    danger: "bg-danger/15 hover:bg-danger/25 text-danger",
  };
  return (
    <motion.button
      whileTap={disabled ? undefined : { scale: 0.94 }}
      onClick={disabled ? undefined : onClick}
      disabled={disabled}
      title={title}
      className={`flex flex-col items-center justify-center gap-1 rounded-lg px-3 py-2 ${
        wide ? "min-w-[84px]" : "min-w-[64px]"
      } ${disabled ? "bg-edge/20 text-slate-500 cursor-not-allowed opacity-60" : tones[tone]} transition-colors`}
    >
      {icon}
      <span className="text-[10px] font-semibold tracking-wide">{label}</span>
    </motion.button>
  );
}

/** Non-critical (per-drone) command button. With a fleet it expands UPWARD into a
 *  drone picker — Overwatch / Outrider / Both — and runs `onPick(target)` with the
 *  chosen drone id (or "all"). With a single drone it just runs the active one.
 *  Critical fleet commands (HOLD/BRAKE/RTL/LAND) use plain <Btn> and hit all.
 *
 *  Ready-for-Flight aware. The picker greys out any drone whose gate is OFF (and
 *  greys "Both" when any drone isn't ready). The single-drone fallback refuses
 *  when the active vehicle is unset or not ready — never a silent "all" leak. */
function DroneBtn({
  icon, label, tone = "default", wide = false, onPick, chipMark,
}: {
  icon: React.ReactNode;
  label: string;
  tone?: "default" | "go" | "warn" | "danger";
  wide?: boolean;
  onPick: (target: string) => void;
  chipMark?: (id: string) => string;
}) {
  const vehicles = useGcs((s) => s.vehicles);
  const activeVehicle = useGcs((s) => s.activeVehicle);
  const readyForFlight = useGcs((s) => s.readyForFlight);
  const pushLog = useGcs((s) => s.pushLog);
  const [open, setOpen] = useState(false);
  const multi = vehicles.length >= 2;
  const anyReady = vehicles.some((v) => v.connected && readyForFlight[v.id]?.ready);
  const activeReady =
    activeVehicle ? Boolean(readyForFlight[activeVehicle]?.ready) : false;
  const allReady =
    vehicles.filter((v) => v.connected).every((v) => readyForFlight[v.id]?.ready);
  const disabled = !anyReady;
  const run = (target: string) => {
    setOpen(false);
    onPick(target);
  };
  const trigger = () => {
    if (multi) {
      setOpen((o) => !o);
      return;
    }
    // Single-drone shell. Must have an active drone AND its gate ON — never a
    // silent "all" fallback (that's the 2026-07-02 rogue-command class).
    if (!activeVehicle) {
      pushLog("safety", `${label} blocked — no active drone`, 2);
      return;
    }
    if (!activeReady) {
      pushLog("safety", `${label} blocked — ready-for-flight OFF`, 2);
      return;
    }
    run(activeVehicle);
  };
  return (
    <div className="relative">
      <AnimatePresence>
        {open && multi && (
          <>
            {/* click-away catcher */}
            <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
            <motion.div
              initial={{ y: 8, opacity: 0, scale: 0.96 }}
              animate={{ y: 0, opacity: 1, scale: 1 }}
              exit={{ y: 8, opacity: 0, scale: 0.96 }}
              transition={{ duration: 0.16, ease: "easeOut" }}
              className="glass absolute bottom-full left-1/2 z-50 mb-2 flex -translate-x-1/2 gap-1 rounded-xl p-1"
            >
              {vehicles.map((v) => {
                const ready = Boolean(readyForFlight[v.id]?.ready);
                return (
                  <button
                    key={v.id}
                    onClick={() => (ready ? run(v.id) : null)}
                    disabled={!ready}
                    title={ready ? "" : "Ready-for-Flight OFF"}
                    className={`whitespace-nowrap rounded-lg px-2.5 py-1.5 text-[11px] font-semibold transition-colors ${
                      ready
                        ? "text-slate-100 hover:bg-accent/20 hover:text-accent"
                        : "text-slate-500 cursor-not-allowed opacity-50"
                    }`}
                  >
                    {v.name}
                    {chipMark ? chipMark(v.id) : ""}
                  </button>
                );
              })}
              <button
                onClick={() => (allReady ? run("all") : null)}
                disabled={!allReady}
                title={allReady ? "" : "Ready-for-Flight OFF for one or more drones"}
                className={`whitespace-nowrap rounded-lg px-2.5 py-1.5 text-[11px] font-bold transition-colors ${
                  allReady
                    ? "text-accent hover:bg-accent/25"
                    : "text-slate-500 cursor-not-allowed opacity-50"
                }`}
              >
                Both
              </button>
            </motion.div>
          </>
        )}
      </AnimatePresence>
      <Btn
        icon={icon}
        label={label}
        tone={tone}
        wide={wide}
        onClick={trigger}
        disabled={disabled}
        title={disabled ? "Enable Ready-for-Flight on a drone before commanding" : undefined}
      />
    </div>
  );
}

const MODES: { id: Mode; icon: React.ReactNode; label: string }[] = [
  { id: "navigate", icon: <Navigation size={14} />, label: "NAV" },
  { id: "survey", icon: <MapIcon size={14} />, label: "SURVEY" },
  { id: "track", icon: <Crosshair size={14} />, label: "TRACK" },
];

// PX4 modes a re-takeoff is REJECTED from: PX4 only accepts VTOL_TAKEOFF/NAV_TAKEOFF
// from a non-auto-landing state, so a fresh TAKEOFF while the drone is returning or
// landing is refused with no auto-reset. The operator must drop to HOLD first.
const NO_RETAKEOFF_MODES = new Set(["AUTO.RTL", "AUTO.LAND", "RTL", "LAND"]);

export default function CommandBar() {
  const { uiMode, setMode } = useGcs();
  const fleetTelem = useGcs((s) => s.fleetTelem);
  const activeVehicle = useGcs((s) => s.activeVehicle);
  const readyForFlight = useGcs((s) => s.readyForFlight);
  const pushLog = useGcs((s) => s.pushLog);
  const [alt, setAlt] = useState(15);
  const [forceConfirm, setForceConfirm] = useState(false);
  const vehicles = useGcs((s) => s.vehicles);
  const activeName =
    vehicles.find((v) => v.id === activeVehicle)?.name ?? "the active drone";

  // Ready-for-Flight gate: is this vehicle (or all connected ones for "all"/"both")
  // authorized to accept flight commands? A single-vehicle target checks that
  // drone's gate; a fleet target requires EVERY connected drone's gate ON. When
  // OFF the button is disabled and the short-circuit here refuses even a JS-side
  // programmatic click. The backend enforces this too (belt AND braces) —
  // relying on either alone would be brittle.
  const canCommand = (target: string): { ok: true } | { ok: false; reason: string } => {
    if (target === "all" || target === "both") {
      const notReady: string[] = [];
      for (const v of vehicles) {
        if (!v.connected) continue;
        if (!readyForFlight[v.id]?.ready) notReady.push(v.name);
      }
      if (notReady.length > 0) {
        return { ok: false, reason: `ready-for-flight OFF: ${notReady.join(", ")}` };
      }
      return { ok: true };
    }
    if (!readyForFlight[target]?.ready) {
      const name = vehicles.find((v) => v.id === target)?.name ?? target;
      return { ok: false, reason: `ready-for-flight OFF for ${name}` };
    }
    return { ok: true };
  };

  const run = (label: string, fn: () => Promise<unknown>) => async () => {
    try {
      await fn();
    } catch (e) {
      pushLog("error", `${label}: ${(e as Error).message}`, 3);
    }
  };

  // Wraps a flight-authorizing action with the gate short-circuit. Recovery
  // commands (HOLD/BRAKE/RTL/LAND/force-disarm) do NOT go through this — they
  // must always work even with the gate off, so the operator can recover a drone
  // that's already in the air.
  const gated = (label: string, target: string, fn: () => Promise<unknown>) =>
    async () => {
      const chk = canCommand(target);
      if (!chk.ok) {
        pushLog("safety", `${label} blocked — ${chk.reason}`, 2);
        return;
      }
      await run(label, fn)();
    };

  // Re-takeoff hint: when the active drone is in an RTL/LAND mode, a fresh TAKEOFF
  // is rejected by PX4 (no auto-reset). Surface a HINT + a one-click "Set HOLD"
  // affordance — we NEVER silently send HOLD; the operator decides. The hint reads
  // the ACTIVE drone (TAKEOFF defaults to it when there's no fleet picker open).
  const activeMode = activeVehicle ? fleetTelem[activeVehicle]?.mode ?? null : null;
  const needsHoldBeforeTakeoff = activeMode != null && NO_RETAKEOFF_MODES.has(activeMode);

  // ARM is a CRITICAL fleet command (like HOLD/BRAKE/RTL/LAND): it hits ALL
  // drones. The toggle reads the FLEET, not the active drone — show DISARM when
  // ANY connected vehicle is armed (so the operator can always disarm), else ARM.
  const anyArmed = Object.values(fleetTelem).some((t) => t.connected && t.armed);

  // Arm/disarm ALL drones, then surface a truthful result. arm/disarm now return
  // { ok, armed, reason } (PX4 COMMAND_ACK + STATUSTEXT); when ok === false the
  // motors did NOT spin up, so report the reason instead of pretending it armed.
  // ARM is gated by Ready-for-Flight; DISARM bypasses (it's a recovery action).
  const armFn = async () => {
    const verb = anyArmed ? "Disarm" : "Arm";
    const res = (await (anyArmed ? api.disarm("all") : api.arm("all"))) as {
      ok?: boolean;
      armed?: boolean;
      reason?: string;
    } | null;
    if (res && res.ok === false) {
      pushLog("error", `${verb} denied: ${res.reason || "rejected"}`, 3);
    }
  };
  const armToggle = anyArmed
    ? run("disarm", armFn)
    : gated("arm", "all", armFn);
  // Whether the arm button should APPEAR disabled — only when we're about to arm
  // and the gate is off. Disarm stays clickable so recovery is always available.
  const armDisabled = !anyArmed && !canCommand("all").ok;

  return (
    <div className="glass instrument relative rounded-2xl px-3 py-2 flex items-center gap-3">
      {/* UI mode switch */}
      <div className="flex gap-1 rounded-lg bg-ink/60 p-1">
        {MODES.map((m) => (
          <button
            key={m.id}
            onClick={() => setMode(m.id)}
            className={`flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-semibold transition-colors ${
              uiMode === m.id ? "bg-accent/25 text-accent" : "text-slate-400 hover:text-slate-200"
            }`}
          >
            {m.icon}
            {m.label}
          </button>
        ))}
      </div>

      <div className="w-px h-9 bg-edge/60" />

      {/* ARM/DISARM — CRITICAL fleet command: always commands ALL connected drones.
          Toggles on the fleet's armed state (danger/red = armed → click to disarm). */}
      <Btn
        tone={anyArmed ? "danger" : "default"}
        icon={<Power size={18} />}
        label={anyArmed ? "DISARM" : "ARM"}
        onClick={armToggle}
        disabled={armDisabled}
        title={
          armDisabled
            ? "Enable Ready-for-Flight for every connected drone before arming"
            : anyArmed
            ? "Disarm ALL drones"
            : "Arm ALL drones"
        }
      />
      {/* TAKEOFF — PER-DRONE (expand to pick a drone or Both). When the active
          drone is in RTL/LAND, a fresh takeoff is rejected by PX4 — show a hint +
          a one-click "Set HOLD" (never auto-sent) above the button. */}
      <div className="relative flex flex-col items-center gap-1">
        {needsHoldBeforeTakeoff && (
          <div className="glass absolute bottom-full left-1/2 z-50 mb-2 flex w-44 -translate-x-1/2 flex-col items-center gap-1 rounded-lg px-2 py-1.5 text-center">
            <span className="text-[10px] leading-tight text-warn">
              In {activeMode} — set HOLD before re-takeoff
            </span>
            <button
              onClick={run("hold", () => api.hold(activeVehicle ?? undefined))}
              className="flex items-center gap-1 rounded-md bg-warn/15 px-2 py-1 text-[10px] font-semibold text-warn hover:bg-warn/25"
              title="Switch the active drone to HOLD so a fresh takeoff is accepted"
            >
              <Hand size={12} /> Set HOLD
            </button>
          </div>
        )}
        <DroneBtn
          tone="go"
          icon={<PlaneTakeoff size={18} />}
          label="TAKEOFF"
          onPick={(t) => run("takeoff", () => api.takeoff(alt, t))()}
        />
        <input
          type="number"
          value={alt}
          min={2}
          max={120}
          onChange={(e) => setAlt(Number(e.target.value))}
          className="w-16 bg-ink/70 border border-edge/60 rounded text-center tnum text-xs py-0.5 text-slate-200"
          title="Takeoff altitude (m)"
        />
      </div>

      <div className="w-px h-9 bg-edge/60" />

      {/* critical / fleet-wide — always command ALL connected drones ("all") */}
      <Btn icon={<Hand size={18} />} label="HOLD" tone="warn" onClick={run("hold", () => api.hold("all"))} />
      <Btn icon={<Octagon size={18} />} label="BRAKE" tone="warn" onClick={run("brake", () => api.brake("all"))} />
      <Btn icon={<Home size={18} />} label="RTL" tone="warn" onClick={run("rtl", () => api.rtl("all"))} />
      <Btn icon={<ScanEye size={18} />} label="LAND" tone="danger" wide onClick={run("land", () => api.land("all"))} />

      <div className="w-px h-9 bg-edge/60" />

      {/* FORCE-DISARM — EMERGENCY. Bypasses PX4's "Disarming denied: not landed"
          (the 2026-05-26 stuck-armed cause) via the force magic. Targets the ACTIVE
          drone ONLY (never "all" — must not cut a healthy flying drone). Confirm-gated. */}
      <div className="relative flex flex-col items-center">
        <AnimatePresence>
          {forceConfirm && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setForceConfirm(false)} />
              <motion.div
                initial={{ y: 8, opacity: 0, scale: 0.96 }}
                animate={{ y: 0, opacity: 1, scale: 1 }}
                exit={{ y: 8, opacity: 0, scale: 0.96 }}
                transition={{ duration: 0.16, ease: "easeOut" }}
                className="glass absolute bottom-full left-1/2 z-50 mb-2 flex w-56 -translate-x-1/2 flex-col items-center gap-1.5 rounded-xl p-2.5 text-center"
              >
                <span className="text-[11px] font-bold leading-tight text-danger">
                  Force-disarm {activeName}?
                </span>
                <span className="text-[9px] leading-snug text-slate-400">
                  Cuts motors IMMEDIATELY — even in flight. Emergency only: a drone stuck
                  armed on the ground (bypasses PX4's "not landed" block).
                </span>
                <div className="mt-0.5 flex gap-2">
                  <button
                    onClick={() => {
                      setForceConfirm(false);
                      run("force-disarm", () => api.forceDisarm(activeVehicle ?? undefined))();
                    }}
                    className="rounded-md bg-danger/30 px-3 py-1 text-[10px] font-bold text-danger hover:bg-danger/50"
                  >
                    FORCE-DISARM
                  </button>
                  <button
                    onClick={() => setForceConfirm(false)}
                    className="rounded-md bg-edge/40 px-3 py-1 text-[10px] font-semibold text-slate-200 hover:bg-edge/70"
                  >
                    Cancel
                  </button>
                </div>
              </motion.div>
            </>
          )}
        </AnimatePresence>
        <Btn
          icon={<Ban size={18} />}
          label="FORCE"
          tone="danger"
          onClick={() => setForceConfirm(true)}
        />
      </div>

      <div className="w-px h-9 bg-edge/60" />

      <VoiceButton />
    </div>
  );
}
