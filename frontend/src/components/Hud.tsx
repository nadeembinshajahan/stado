import { useRef, useState } from "react";
import { motion } from "framer-motion";
import { useGcs } from "../store/useGcs";
import { useConnectedGate } from "../lib/useConnectedGate";
import AttitudeIndicator from "./AttitudeIndicator";

// Top-view multirotor glyph: `rotors` arms + rotor discs around a hub. Quad
// draws an X (4), hexa a flat hexagon (6) — so Overwatch (hexa) vs Outrider
// (quad) read at a glance next to the name.
const ROTORS: Record<string, number> = { quadcopter: 4, hexacopter: 6, octocopter: 8 };
function RotorIcon({
  rotors = 4,
  size = 15,
  className = "",
  label,
}: {
  rotors?: number;
  size?: number;
  className?: string;
  label?: string;
}) {
  const cx = 12, cy = 12, R = 8, r = 2.5;
  const offset = rotors === 4 ? Math.PI / 4 : 0; // quad = X, even rotors = flat
  const pts = Array.from({ length: rotors }, (_, i) => {
    const a = offset + (i * 2 * Math.PI) / rotors;
    return [cx + R * Math.cos(a), cy + R * Math.sin(a)] as const;
  });
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinecap="round"
    >
      {label && <title>{label}</title>}
      {pts.map(([x, y], i) => (
        <line key={`l${i}`} x1={cx} y1={cy} x2={x} y2={y} />
      ))}
      {pts.map(([x, y], i) => (
        <circle key={`c${i}`} cx={x} cy={y} r={r} />
      ))}
      <circle cx={cx} cy={cy} r="1.5" fill="currentColor" stroke="none" />
    </svg>
  );
}

function Read({
  label,
  value,
  unit,
  critical,
  urgent,
}: {
  label: string;
  value: string;
  unit?: string;
  critical?: boolean;
  urgent?: boolean;
}) {
  const cls = critical
    ? `hud-critical${urgent ? " hud-critical-urgent" : ""}`
    : "text-slate-100";
  return (
    <div className="flex flex-col">
      <span className="text-[10px] text-slate-400 tracking-wider">{label}</span>
      <div className="flex items-baseline gap-1">
        <span className={`tnum text-lg leading-none ${cls}`}>{value}</span>
        {unit && <span className="text-[10px] text-slate-500">{unit}</span>}
      </div>
    </div>
  );
}

const f = (n: number | null | undefined, d = 1) => (n == null ? "--" : n.toFixed(d));

/** Instrument HUD — shown for every CONNECTED drone (display-only; the "active"
 *  drone is still the one commands target, and it's highlighted here). This
 *  wrapper decides WHETHER to show this drone, then renders the instrument.
 *  Called per-vehicle (`vehicleId`) it renders whenever THAT vehicle is
 *  connected — so when both drones are connected, both HUDs render (App.tsx
 *  stacks them in a vertical column). Called bare it shows the active drone if
 *  it's live. If the drone isn't connected it renders nothing — never a
 *  stale/offline HUD.
 *
 *  The gating lives here (and the instrument is a child) so the per-field
 *  flash/critical hooks below never run conditionally — a `<Hud key={id}>`
 *  instance for a disconnected drone simply mounts nothing.
 *  The console lives in its own panel (below the conversation window). */
export default function Hud({ vehicleId }: { vehicleId?: string }) {
  const vehicles = useGcs((s) => s.vehicles);
  const activeVehicle = useGcs((s) => s.activeVehicle);
  const activeConnected = useGcs((s) => s.telem.connected);

  // The active (command-target) vehicle's id: the store's `activeVehicle` (the
  // drone commands target / drives the instruments — it auto-follows the
  // connected drone), falling back to the `/api/vehicles` `active` flag if the
  // store hasn't latched one yet. Used only to HIGHLIGHT the active HUD now.
  const activeId = activeVehicle ?? vehicles.find((v) => v.active)?.id ?? null;

  // DISPLAY GATING IS PER-CONNECTED DRONE (not per-active). Per-vehicle: render
  // this HUD whenever THAT vehicle is connected, so both connected drones show
  // their HUD at once. The `isActive` flag (passed down) keeps the active drone
  // visually distinguished (teal ring/accent) without hiding the other. The
  // gate is DEBOUNCED (see GatedHud) so a transient roster flip never blanks the
  // HUD — it stays mounted and the instrument itself shows the offline state.
  if (vehicleId) {
    return <GatedHud vehicleId={vehicleId} isActive={vehicleId === activeId} />;
  }
  // Bare (single-vehicle) call: render the active telemetry exactly as before —
  // no `vehicleId`, so no name header, driven by the store's `telem`.
  const bareOnline = vehicles.find((v) => v.id === activeId)?.connected ?? activeConnected;
  if (!bareOnline) return null;
  return <HudInstrument isActive />;
}

/** Per-vehicle HUD gate. C1: mounts the instrument once the drone first connects
 *  and keeps it mounted across transient `connected:false` blips (debounced) so
 *  a one-poll roster flap doesn't blank/remount the HUD. Before the first-ever
 *  connect it renders nothing. The instrument reads live telemetry and shows the
 *  offline state itself, so a sustained disconnect degrades gracefully in place. */
function GatedHud({ vehicleId, isActive }: { vehicleId: string; isActive: boolean }) {
  const { mounted } = useConnectedGate(vehicleId);
  if (!mounted) return null;
  return <HudInstrument vehicleId={vehicleId} isActive={isActive} />;
}

/** The actual instrument for one drone. Always rendered when mounted, so the
 *  flash/critical hooks below are unconditional. `isActive` highlights the
 *  command-target drone (display-only). */
function HudInstrument({ vehicleId, isActive }: { vehicleId?: string; isActive: boolean }) {
  const vehicles = useGcs((s) => s.vehicles);
  const fleetTelem = useGcs((s) => s.fleetTelem);
  const activeTelem = useGcs((s) => s.telem);

  // Per-vehicle telemetry comes from fleetTelem; fall back to the active telem
  // for the active drone (whose live stream also drives `telem`).
  const t = vehicleId ? fleetTelem[vehicleId] ?? (isActive ? activeTelem : undefined) : activeTelem;
  const veh = vehicleId ? vehicles.find((v) => v.id === vehicleId) : undefined;
  const name = veh?.name ?? (vehicleId ?? null);
  const rotors = veh?.kind ? ROTORS[veh.kind] ?? 4 : 4;
  const status = t?.connected ? t?.mode ?? "—" : "offline";

  // --- Rapid-change detection ----------------------------------------------
  // Track the last rendered numeric value per field; if a field jumps by more
  // than its threshold between updates, flash it red until the timestamp ttl
  // passes. Stored in a ref so it survives re-renders without store state.
  const prevRef = useRef<Record<string, number>>({});
  const flashUntilRef = useRef<Record<string, number>>({});
  const FLASH_MS = 900; // how long a rapid-change flash lingers
  // Per-field "this is a big jump" deltas (in the field's own units).
  const JUMP: Record<string, number> = {
    alt_rel: 15,        // m between updates
    alt_msl: 15,        // m
    heading: 45,        // deg (wrap-aware below)
    climb: 6,           // m/s
    groundspeed: 10,    // m/s
    airspeed: 10,       // m/s
    throttle: 40,       // %
    battery_voltage: 3, // V
    battery_current: 25, // A
  };
  const now = Date.now();
  const flashing = (key: string, v: number | null | undefined): boolean => {
    if (v == null || !Number.isFinite(v)) return (flashUntilRef.current[key] ?? 0) > now;
    const prev = prevRef.current[key];
    const limit = JUMP[key];
    if (prev != null && limit != null) {
      let delta = Math.abs(v - prev);
      if (key === "heading") delta = Math.min(delta, 360 - delta); // shortest arc
      if (delta >= limit) flashUntilRef.current[key] = now + FLASH_MS;
    }
    prevRef.current[key] = v;
    return (flashUntilRef.current[key] ?? 0) > now;
  };

  // --- Critical conditions --------------------------------------------------
  const armed = !!t?.armed;
  const pct = t?.battery_pct;
  const volt = t?.battery_voltage;
  // Battery: prefer pct; if pct is null fall back to a low-voltage heuristic
  // (assume a 4S-ish pack: <=14.0V getting low, <=13.2V urgent).
  const battCritical =
    pct != null ? pct <= 20 : volt != null ? volt <= 14.0 : false;
  const battUrgent =
    pct != null ? pct <= 10 : volt != null ? volt <= 13.2 : false;

  // GPS: no fix or no sats while armed is dangerous (flying without position).
  const gpsCritical = armed && (t?.gps_fix === 0 || t?.satellites === 0);

  // Energy/dynamics: aggressive vertical motion paired with high current draw
  // (e.g. fighting a hard descent or runaway climb) → critical.
  const fastVert = t?.climb != null && Math.abs(t.climb) >= 8; // m/s
  const highCurrent = t?.battery_current != null && t.battery_current >= 40; // A
  const dynamicsCritical = fastVert && highCurrent;

  // A genuine critical event (low batt / GPS lost while armed / hard dynamics)
  // drives the auto-expand. Keep it "sticky" ~5s so a brief spike stays readable.
  const critical = battCritical || gpsCritical || dynamicsCritical;
  const criticalUntil = useRef(0);
  if (critical) criticalUntil.current = now + 5000;
  const criticalActive = critical || now < criticalUntil.current;

  // Docked compact chip by default; expands (spring) on hover, on a critical
  // event, or when pinned (click). Collapses again on mouse-leave otherwise.
  const [hovered, setHovered] = useState(false);
  const [pinned, setPinned] = useState(false);
  const expanded = pinned || hovered || criticalActive;

  const dotCls = criticalActive
    ? "bg-danger"
    : isActive ? "bg-accent" : t?.connected ? "bg-amber-400/80" : "bg-slate-500";

  // ── collapsed chip ──
  const chip = (
    <div className="flex items-center gap-2">
      <span className={`h-2 w-2 shrink-0 rounded-full ${dotCls} ${criticalActive ? "pulse" : ""}`} />
      <RotorIcon rotors={rotors} label={veh?.kind} size={14} className={isActive ? "text-accent/90" : "text-slate-300"} />
      <span className={`text-xs font-semibold tracking-wide ${isActive ? "text-accent" : "text-slate-200"}`}>
        {(name ?? "DRONE").toUpperCase()}
      </span>
      <span className="text-[10px] tracking-wider text-slate-400">
        {status}{t?.armed ? " · ARMED" : ""}
      </span>
      <span className={`tnum text-xs ${battCritical ? "hud-critical" : "text-slate-200"}`}>
        {pct != null ? `${pct}%` : volt != null ? `${volt.toFixed(1)}V` : "--"}
      </span>
    </div>
  );

  // ── expanded panel ──
  const full = (
    <>
      {name && (
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-1.5">
            <span className={`h-2 w-2 rounded-full ${isActive ? "bg-accent" : "bg-amber-400/80"}`} />
            <span className={`text-xs font-semibold tracking-wide ${isActive ? "text-accent" : "text-slate-200"}`}>
              {name.toUpperCase()}
            </span>
            <RotorIcon rotors={rotors} label={veh?.kind} className={isActive ? "text-accent/90" : "text-slate-300"} />
          </div>
          <span className={`text-[10px] tracking-wider ${t?.armed ? "text-danger" : "text-slate-500"}`}>
            {status}{t?.armed ? " · ARMED" : ""}
          </span>
        </div>
      )}

      <div className="flex items-center gap-3">
        <AttitudeIndicator roll={t?.roll ?? null} pitch={t?.pitch ?? null} size={120} />
        <div className="grid grid-cols-1 gap-2">
          <Read label="REL ALT" value={f(t?.alt_rel, 1)} unit="m" critical={flashing("alt_rel", t?.alt_rel)} />
          <Read
            label="HEADING"
            value={t?.heading == null ? "--" : `${Math.round(t.heading)}°`}
            critical={gpsCritical || flashing("heading", t?.heading)}
            urgent={gpsCritical}
          />
          <Read
            label="V·SPD"
            value={f(t?.climb, 1)}
            unit="m/s"
            critical={dynamicsCritical || flashing("climb", t?.climb)}
            urgent={dynamicsCritical}
          />
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2 border-t border-edge/60 pt-2">
        <Read label="GND SPD" value={f(t?.groundspeed, 1)} unit="m/s" critical={flashing("groundspeed", t?.groundspeed)} />
        <Read label="AIR SPD" value={f(t?.airspeed, 1)} unit="m/s" critical={flashing("airspeed", t?.airspeed)} />
        <Read
          label="THR"
          value={t?.throttle == null ? "--" : `${t.throttle}%`}
          critical={flashing("throttle", t?.throttle)}
        />
        <Read
          label="BATT"
          value={f(t?.battery_voltage, 1)}
          unit="V"
          critical={battCritical || flashing("battery_voltage", t?.battery_voltage)}
          urgent={battUrgent}
        />
        <Read
          label="CUR"
          value={f(t?.battery_current, 1)}
          unit="A"
          critical={dynamicsCritical || flashing("battery_current", t?.battery_current)}
          urgent={dynamicsCritical}
        />
        <Read label="MSL" value={f(t?.alt_msl, 0)} unit="m" critical={flashing("alt_msl", t?.alt_msl)} />
      </div>
    </>
  );

  return (
    <motion.div
      layout
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onClick={() => setPinned((p) => !p)}
      title={pinned ? "pinned — click to collapse" : expanded ? "" : "hover or click to expand"}
      transition={{ type: "spring", stiffness: 400, damping: 34 }}
      className={`glass instrument relative flex cursor-pointer flex-col gap-3 overflow-hidden rounded-xl ${
        expanded ? "w-64 p-3" : "w-auto p-2"
      } ${criticalActive ? "ring-1 ring-danger/80" : name && isActive ? "ring-1 ring-accent/60" : ""}`}
    >
      {expanded ? full : chip}
    </motion.div>
  );
}
