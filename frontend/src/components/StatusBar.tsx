import { useEffect, useState } from "react";
import {
  BatteryFull, BatteryLow, BatteryMedium, Box, ClipboardList, Lock,
  RotateCcw, Satellite, ShieldCheck, ShieldOff, Wifi, WifiOff,
} from "lucide-react";
import { api } from "../lib/api";
import { EMPTY, useGcs } from "../store/useGcs";
import type { Telemetry } from "../lib/types";
import HelpButton from "./demo/HelpButton";

const GPS_FIX = ["NO GPS", "NO FIX", "2D", "3D", "DGPS", "RTK-FLT", "RTK-FIX"];

// Identity colors per drone (match the map markers / HUDs): active = teal, others
// = amber. Keeps the chips legible at a glance against the dark bar.
const ACTIVE_COLOR = "#22e3c4";
const OTHER_COLORS = ["#ffb020", "#7c9cff", "#ff6fae", "#9be870"];

function Battery({ pct }: { pct: number | null }) {
  const v = pct ?? 0;
  const color = v > 50 ? "text-ok" : v > 20 ? "text-warn" : "text-danger";
  const Icon = v > 60 ? BatteryFull : v > 25 ? BatteryMedium : BatteryLow;
  return (
    <div className={`flex items-center gap-1 ${color}`}>
      <Icon size={15} />
      <span className="tnum text-xs">{pct == null ? "--" : `${v}%`}</span>
    </div>
  );
}

/** Per-vehicle Ready-for-Flight toggle. OFF (red) → click to arm gate for flight.
 * ON (green) → click to disable (refused with a toast if the vehicle is locked
 * armed+airborne). LOCKED shows a padlock and is un-clickable. */
function ReadyPill({ vehicleId, name }: { vehicleId: string; name: string }) {
  const state = useGcs((s) => s.readyForFlight[vehicleId]);
  const setRemote = useGcs((s) => s.setReadyForFlightRemote);
  const pushLog = useGcs((s) => s.pushLog);
  const [busy, setBusy] = useState(false);

  const ready = !!state?.ready;
  const locked = !!state?.locked;

  const onClick = async () => {
    if (busy || locked) return;
    setBusy(true);
    try {
      await setRemote(vehicleId, !ready);
      pushLog("safety",
        !ready ? `${name}: ready for flight` : `${name}: flight commands disabled`,
        undefined, vehicleId);
    } catch (err) {
      pushLog("safety", `${name}: ${(err as Error).message}`, 3, vehicleId);
    } finally {
      setBusy(false);
    }
  };

  const title = locked
    ? `${name} is armed and airborne — the gate is locked ON until it lands`
    : ready
    ? `${name} is armed for flight — click to disable (refused mid-flight)`
    : `${name} is NOT armed for flight — click to enable commands`;

  const cls = ready
    ? locked
      ? "bg-ok/25 text-ok border border-ok/40"
      : "bg-ok/20 text-ok hover:bg-ok/30"
    : "bg-danger/20 text-danger hover:bg-danger/30";

  return (
    <button
      onClick={onClick}
      disabled={busy || locked}
      title={title}
      className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-[10px] font-bold uppercase tracking-wide transition-colors ${cls} disabled:cursor-not-allowed`}
    >
      {locked ? <Lock size={12} /> : ready ? <ShieldCheck size={12} /> : <ShieldOff size={12} />}
      <span>{name.slice(0, 3).toUpperCase()}</span>
      <span className="opacity-70">{ready ? (locked ? "FLYING" : "READY") : "SAFE"}</span>
    </button>
  );
}


function Stat({ label, value, unit }: { label: string; value: string; unit?: string }) {
  return (
    <div className="flex items-baseline gap-0.5">
      <span className="text-[9px] text-slate-400">{label}</span>
      <span className="tnum text-xs text-slate-100">{value}</span>
      {unit ? <span className="text-[9px] text-slate-500">{unit}</span> : null}
    </div>
  );
}

function fmt(n: number | null, d: number) {
  return n == null ? "--" : n.toFixed(d);
}

/** Compact per-drone summary: identity dot + name + mode + ALT/GS/sats/battery. */
function DroneChip({ name, color, t }: { name: string; color: string; t: Telemetry }) {
  const fix = t.gps_fix ?? 0;
  return (
    <div className="flex items-center gap-2 rounded-lg bg-ink/50 px-2.5 py-1">
      <span
        className={`h-2 w-2 rounded-full ${t.armed ? "pulse" : ""}`}
        style={{ background: color }}
        title={`${name} · ${t.armed ? "ARMED" : "disarmed"}`}
      />
      <span className="text-xs font-bold" style={{ color }}>{name}</span>
      <span
        className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${
          t.connected ? "bg-accent/15 text-accent" : "bg-edge/40 text-slate-500"
        }`}
      >
        {t.connected ? (t.mode ?? "—") : "NO LINK"}
      </span>
      <Stat label="ALT" value={fmt(t.alt_rel, 1)} unit="m" />
      <Stat label="GS" value={fmt(t.groundspeed, 1)} />
      <div className="flex items-center gap-1 text-slate-300">
        <Satellite size={13} className={fix >= 3 ? "text-ok" : "text-warn"} />
        <span className="tnum text-xs">{t.satellites ?? "--"}</span>
        <span className="text-[9px] text-slate-500">{GPS_FIX[fix] ?? fix}</span>
      </div>
      <Battery pct={t.battery_pct} />
    </div>
  );
}


/** Demo-only "Reset Sim" pill. Hard-restarts the SITL container by asking the
 * backend to signal PID 1; Docker's restart policy respawns in ≈60 s. Shows a
 * live countdown while waiting, then reloads the page. Confirm-guarded so a
 * stray click during a demo doesn't kill an in-progress take. */
function ResetSimPill() {
  const [phase, setPhase] = useState<"idle" | "confirm" | "waiting">("idle");
  const [remain, setRemain] = useState(60);
  useEffect(() => {
    if (phase !== "waiting") return;
    const t = setInterval(() => setRemain((s) => Math.max(0, s - 1)), 1000);
    return () => clearInterval(t);
  }, [phase]);
  useEffect(() => {
    if (phase === "waiting" && remain === 0) window.location.reload();
  }, [phase, remain]);
  const trigger = async () => {
    try {
      await api.resetSim();
      setPhase("waiting");
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error("reset sim failed:", e);
      setPhase("idle");
    }
  };
  if (phase === "waiting") {
    return (
      <div
        className="flex items-center gap-1.5 rounded-md bg-warn/25 px-2 py-1 text-[10px] font-bold uppercase tracking-wide text-warn"
        title="SITL is restarting — page will reload automatically"
      >
        <RotateCcw size={12} className="animate-spin" />
        <span>reset · {remain}s</span>
      </div>
    );
  }
  if (phase === "confirm") {
    return (
      <div className="flex items-center gap-1">
        <button
          onClick={trigger}
          className="rounded-md bg-danger/30 px-2 py-1 text-[10px] font-bold text-danger hover:bg-danger/50"
        >
          confirm reset
        </button>
        <button
          onClick={() => setPhase("idle")}
          className="rounded-md bg-edge/40 px-2 py-1 text-[10px] font-semibold text-slate-300 hover:bg-edge/60"
        >
          cancel
        </button>
      </div>
    );
  }
  return (
    <button
      onClick={() => setPhase("confirm")}
      title="Restart the SITL container — drones back at spawn, all state cleared (~60 s)"
      className="flex items-center gap-1.5 rounded-md bg-edge/40 px-2 py-1 text-[10px] font-bold uppercase tracking-wide text-slate-300 transition-colors hover:bg-warn/25 hover:text-warn"
    >
      <RotateCcw size={12} />
      <span>reset sim</span>
    </button>
  );
}


export default function StatusBar() {
  const { telem, socketOpen, view3d, toggle3d, setReportOpen } = useGcs();
  const fleetTelem = useGcs((s) => s.fleetTelem);
  const vehicles = useGcs((s) => s.vehicles);
  const activeVehicle = useGcs((s) => s.activeVehicle);

  // One chip per known drone. Telemetry truthfulness: a chip shows its OWN
  // per-vehicle telemetry (`fleetTelem[id]`). The ACTIVE drone may fall back to
  // the live `telem` stream (which also drives it). A NON-active drone with no
  // telemetry of its own yet falls back to a blank, disconnected EMPTY — NEVER
  // the active drone's data — so it reads a truthful "NO LINK" instead of
  // borrowing another vehicle's mode/battery/connected state.
  let others = 0;
  const chips = vehicles.length
    ? vehicles.map((v) => ({
        id: v.id,
        name: v.name,
        t: fleetTelem[v.id] ?? (v.id === activeVehicle ? telem : EMPTY),
        color: v.id === activeVehicle ? ACTIVE_COLOR : OTHER_COLORS[others++ % OTHER_COLORS.length],
      }))
    : [{ id: "active", name: "DRONE", t: telem, color: ACTIVE_COLOR }];

  return (
    <div className="glass instrument relative flex items-center gap-3 px-4 h-12 rounded-xl">
      {/* left: logo + reports */}
      <div className="flex items-center gap-3 pr-3 border-r border-edge/60">
        <img
          src="/strato-logo.png"
          alt="StratoFirma Autonomy Labs"
          className="h-[26px] w-auto select-none"
          draggable={false}
          style={{
            filter:
              "brightness(0) invert(1) drop-shadow(0 0 5px rgba(34,227,196,0.65)) drop-shadow(0 0 16px rgba(34,227,196,0.35))",
          }}
        />
        <button
          onClick={() => setReportOpen(true)}
          className="flex items-center gap-1.5 rounded-md bg-edge/40 px-2 py-1 text-xs font-semibold text-slate-300 transition-colors hover:bg-accent/20 hover:text-accent"
          title="Open mission reports"
        >
          <ClipboardList size={14} />
          Reports
        </button>
        <HelpButton />
      </div>

      {/* GCS socket */}
      <div className="flex items-center gap-1.5">
        {socketOpen ? <Wifi size={16} className="text-ok" /> : <WifiOff size={16} className="text-danger" />}
        <span className={`text-xs ${socketOpen ? "text-ok" : "text-danger"}`}>
          {socketOpen ? "GCS" : "OFFLINE"}
        </span>
      </div>

      {/* 2D / 3D toggle */}
      <button
        onClick={toggle3d}
        className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-bold transition-colors ${
          view3d ? "bg-accent/20 text-accent glow-accent" : "bg-edge/40 text-slate-300 hover:bg-edge/60"
        }`}
        title="Toggle photorealistic 3D"
      >
        <Box size={14} />
        {view3d ? "3D" : "2D"}
      </button>

      {/* Per-vehicle Ready-for-Flight gate. OFF at boot; must be flipped ON before
          any flight command reaches the drone. Auto-locks ON armed+airborne. */}
      {vehicles.length > 0 && (
        <div className="flex items-center gap-1.5 border-l border-edge/60 pl-3">
          {vehicles.map((v) => (
            <ReadyPill key={v.id} vehicleId={v.id} name={v.name} />
          ))}
        </div>
      )}

      {/* Demo Reset — hard-restart the SITL container for a clean take. */}
      <div className="border-l border-edge/60 pl-3">
        <ResetSimPill />
      </div>

      <div className="flex-1" />

      {/* per-drone summaries — both drones, neatly */}
      <div className="flex items-center gap-2">
        {chips.map((c) => (
          <DroneChip key={c.id} name={c.name} color={c.color} t={c.t} />
        ))}
      </div>
    </div>
  );
}
