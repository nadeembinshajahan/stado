import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Boxes, Crosshair, Loader2, Plus, Send, Trash2, X } from "lucide-react";
import { useGcs } from "../store/useGcs";
import type { FleetZone } from "../store/useGcs";
import { api } from "../lib/api";
import { lawnmowerPath, splitRectZones, zoneColor } from "../lib/geo";

/**
 * FLEET search-area planning UX (works over both the 2D and 3D map). The operator
 * builds a set of NAMED, persisted search regions: pick a center on the map, set
 * width × breadth (m) + rotation + name, and an oriented rectangle updates LIVE
 * in both views (the maps draw the selected `fleetRegion` from the store; every
 * other saved region is drawn dimmed). Regions persist to localStorage so they
 * survive a refresh.
 *
 * Selecting a region (here OR by clicking its polygon on the map) loads it for
 * editing; every field edit and every drag of the on-map center handle updates
 * the region LIVE — including the divided fleet-zone preview if shown — BEFORE
 * any backend command. "Survey with fleet" is the only action that POSTs to the
 * backend (`surveyCoordinated`), which splits the region into one zone per drone.
 */
export default function FleetSurveyPanel() {
  const pushLog = useGcs((s) => s.pushLog);
  const fleetZones = useGcs((s) => s.fleetZones);
  const setFleetZones = useGcs((s) => s.setFleetZones);
  const pickCenter = useGcs((s) => s.fleetPickCenter);
  const setPickCenter = useGcs((s) => s.setFleetPickCenter);
  const savedRegions = useGcs((s) => s.savedRegions);
  const selectedRegionId = useGcs((s) => s.selectedRegionId);
  const updateRegion = useGcs((s) => s.updateRegion);
  const removeRegion = useGcs((s) => s.removeRegion);
  const selectRegion = useGcs((s) => s.selectRegion);
  const vehicles = useGcs((s) => s.vehicles);

  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  // The region currently being edited = the selected one (its live geometry is
  // mirrored into fleetRegion by the store). Editing fields patches it directly,
  // so the map (2D + 3D) reflects changes in real time, before any commit.
  const selected = savedRegions.find((r) => r.id === selectedRegionId) ?? null;

  // Backend coordinated-survey defaults — mirrored here so the LIVE preview
  // splits + grids the region exactly as the backend will on commit.
  const LINE_SPACING_M = 25;
  const GAP_M = 5;

  // The fleet that will fly: connected, MISSION-CAPABLE vehicles in registry
  // order; fall back to the full vehicle list, then to a 2-drone default
  // (Overwatch + Outrider) so a preview always shows even before any vehicle
  // connects. A vehicle whose transport can't carry the MISSION_* upload
  // (supports_missions=false, e.g. a DDS-bridge Outrider with the old bridge) is
  // EXCLUDED here — matching the backend's per-vehicle survey filter — so the
  // preview split shows only the drones that will actually fly the survey.
  // (Missing flag → treated as mission-capable so nothing is wrongly dropped.)
  const missionCapable = (v: { supports_missions?: boolean }) => v.supports_missions !== false;
  const excludedNames = vehicles
    .filter((v) => v.connected && !missionCapable(v))
    .map((v) => v.name);
  const fleet = (() => {
    const connected = vehicles.filter((v) => v.connected && missionCapable(v));
    const pool = connected.length ? connected : vehicles.filter(missionCapable);
    if (pool.length) return pool.map((v) => ({ vehicle: v.id, name: v.name }));
    return [
      { vehicle: "overwatch", name: "Overwatch" },
      { vehicle: "outrider", name: "Outrider" },
    ];
  })();

  // LIVE preview: while a region is selected and we're NOT showing a committed
  // (flying) survey, divide it into one zone per drone and plan each zone's
  // lawnmower path — so the operator sees every drone's path in its own color,
  // updating in real time as the region is moved / resized / rotated, BEFORE any
  // command. The committed result (set by surveyWithFleet) is left untouched.
  const fleetCount = fleet.length;
  const fleetKey = fleet.map((f) => `${f.vehicle}:${f.name}`).join(",");
  useEffect(() => {
    const flying = fleetZones.some((z) => z.flying);
    if (!selected || flying || fleetCount < 1) return;
    const polys = splitRectZones(
      selected.center[0],
      selected.center[1],
      Math.max(1, selected.width_m),
      Math.max(1, selected.height_m),
      ((selected.heading_deg % 360) + 360) % 360,
      fleetCount,
      GAP_M,
    );
    const preview: FleetZone[] = polys.map((polygon, i) => ({
      vehicle: fleet[i]?.vehicle ?? `drone${i}`,
      name: fleet[i]?.name ?? `Drone ${i + 1}`,
      polygon,
      path: lawnmowerPath(polygon, LINE_SPACING_M),
      flying: false,
    }));
    setFleetZones(preview);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    selectedRegionId,
    selected?.center[0],
    selected?.center[1],
    selected?.width_m,
    selected?.height_m,
    selected?.heading_deg,
    fleetCount,
    fleetKey,
  ]);

  // Leaving the panel tears down the picking mode (but keeps saved regions/zones).
  const close = () => {
    setOpen(false);
    setPickCenter(false);
  };

  // Begin defining a NEW region: arm "pick center"; the next map click drops a
  // fresh region with sensible defaults (handled in the map click handlers, which
  // call addRegion when no region is selected). Deselect first so the click adds.
  const startNew = () => {
    selectRegion(null);
    setFleetZones([]);
    setPickCenter(true);
  };

  const surveyWithFleet = async () => {
    if (!selected || busy) return;
    setBusy(true);
    try {
      const resp = await api.surveyCoordinated({
        name: selected.name,
        center: selected.center,
        width_m: Math.max(1, selected.width_m),
        height_m: Math.max(1, selected.height_m),
        heading_deg: ((selected.heading_deg % 360) + 360) % 360,
      });
      const zones: FleetZone[] = (resp.assignments || []).map((a) => {
        // Prefer the backend's actual planned grid; fall back to a frontend
        // lawnmower over the zone polygon if the backend didn't return one.
        const path =
          (a as { path?: [number, number][] }).path ??
          lawnmowerPath(a.polygon, LINE_SPACING_M);
        return {
          vehicle: a.vehicle,
          name: a.name,
          polygon: a.polygon,
          path,
          flying: true,
        };
      });
      setFleetZones(zones);
      setPickCenter(false);
      if (zones.length) {
        const names = zones.map((z) => z.name).join(" + ");
        pushLog("cmd", `Fleet survey "${selected.name}" — ${names}`);
      } else {
        pushLog("vision", "Fleet survey: backend returned no assignments", 2);
      }
    } catch (e) {
      pushLog("error", `Fleet survey: ${(e as Error).message}`, 3);
    } finally {
      setBusy(false);
    }
  };

  const num = (v: string) => (v === "" ? 0 : Number(v));

  return (
    <>
      {/* FLEET affordance — left rail, below the map controls. */}
      <button
        onClick={() => setOpen((v) => !v)}
        title="Fleet search-area planning"
        className={`glass absolute left-3 top-[9.25rem] z-20 rounded-lg p-2 ${
          open || savedRegions.length ? "text-accent glow-accent" : "text-slate-300"
        }`}
      >
        <div className="relative">
          <Boxes size={18} />
          {savedRegions.length > 0 && (
            <span className="absolute -right-2 -top-2 flex h-4 min-w-4 items-center justify-center rounded-full bg-accent px-1 text-[9px] font-bold text-ink">
              {savedRegions.length}
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
            className="glass absolute left-[17.5rem] top-[9.25rem] z-20 flex max-h-[calc(100vh-14rem)] w-64 flex-col gap-2.5 overflow-y-auto rounded-xl p-3"
          >
            <div className="flex items-center justify-between">
              <span className="text-[11px] font-semibold uppercase tracking-wide text-accent">
                Search Areas
              </span>
              <button className="text-slate-500 hover:text-slate-200" onClick={close}>
                <X size={14} />
              </button>
            </div>

            {/* Saved regions list — select to edit, × to delete. */}
            <div className="flex flex-col gap-1">
              {savedRegions.length === 0 ? (
                <p className="text-[11px] leading-tight text-slate-500">
                  No search areas yet. Add one, then click the map to set its
                  center.
                </p>
              ) : (
                savedRegions.map((r, i) => {
                  const isSel = r.id === selectedRegionId;
                  return (
                    <div
                      key={r.id}
                      className={`group flex items-center gap-2 rounded-md px-2 py-1.5 transition-colors ${
                        isSel ? "bg-edge/70 ring-1 ring-accent/50" : "bg-edge/30 hover:bg-edge/50"
                      }`}
                    >
                      <span
                        className="h-3 w-3 shrink-0 rounded-sm"
                        style={{ background: zoneColor(r.name, i) }}
                      />
                      <button
                        className="min-w-0 flex-1 text-left"
                        onClick={() => selectRegion(isSel ? null : (r.id as string))}
                        title="Select to edit"
                      >
                        <span className="block truncate text-xs font-semibold text-slate-100">
                          {r.name}
                        </span>
                        <span className="tnum block text-[10px] text-slate-500">
                          {Math.round(r.width_m)} × {Math.round(r.height_m)} m ·{" "}
                          {Math.round(((r.heading_deg % 360) + 360) % 360)}°
                        </span>
                      </button>
                      <button
                        onClick={() => removeRegion(r.id as string)}
                        className="shrink-0 text-slate-500 transition-colors hover:text-danger"
                        title="Delete this search area"
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  );
                })
              )}
            </div>

            <button
              onClick={pickCenter && !selected ? () => setPickCenter(false) : startNew}
              className={`flex items-center justify-center gap-2 rounded-md px-3 py-2 text-xs font-semibold transition-colors ${
                pickCenter && !selected
                  ? "bg-accent/25 text-accent glow-accent"
                  : "bg-edge/40 text-slate-100 hover:bg-edge/60"
              }`}
            >
              {pickCenter && !selected ? (
                <>
                  <Crosshair size={14} /> Click map to place center…
                </>
              ) : (
                <>
                  <Plus size={14} /> New search area
                </>
              )}
            </button>

            {/* Editor — only when a region is selected. Edits patch the saved
                region live (the map redraws from the store immediately). */}
            {selected && (
              <div className="flex flex-col gap-2.5 border-t border-edge/50 pt-2.5">
                <span className="text-[10px] uppercase tracking-wide text-slate-400">
                  Editing
                </span>

                <label className="flex flex-col gap-1">
                  <span className="text-[10px] uppercase tracking-wide text-slate-400">Name</span>
                  <input
                    value={selected.name}
                    onChange={(e) => updateRegion(selected.id as string, { name: e.target.value })}
                    className="w-full rounded border border-edge/60 bg-ink/70 px-2 py-1 text-xs text-slate-200"
                  />
                </label>

                <span className="tnum text-[10px] text-slate-400">
                  center {selected.center[0].toFixed(5)}, {selected.center[1].toFixed(5)}{" "}
                  · drag the ◆ handle to move
                </span>

                <div className="flex gap-2">
                  <label className="flex flex-1 flex-col gap-1">
                    <span className="text-[10px] uppercase tracking-wide text-slate-400">
                      Width (m)
                    </span>
                    <input
                      type="number"
                      min={1}
                      value={Math.round(selected.width_m)}
                      onChange={(e) =>
                        updateRegion(selected.id as string, { width_m: Math.max(1, num(e.target.value)) })
                      }
                      className="tnum w-full rounded border border-edge/60 bg-ink/70 py-1 text-center text-xs text-slate-200"
                    />
                  </label>
                  <label className="flex flex-1 flex-col gap-1">
                    <span className="text-[10px] uppercase tracking-wide text-slate-400">
                      Breadth (m)
                    </span>
                    <input
                      type="number"
                      min={1}
                      value={Math.round(selected.height_m)}
                      onChange={(e) =>
                        updateRegion(selected.id as string, { height_m: Math.max(1, num(e.target.value)) })
                      }
                      className="tnum w-full rounded border border-edge/60 bg-ink/70 py-1 text-center text-xs text-slate-200"
                    />
                  </label>
                </div>

                <label className="flex flex-col gap-1">
                  <div className="flex items-center justify-between">
                    <span className="text-[10px] uppercase tracking-wide text-slate-400">
                      Rotation
                    </span>
                    <input
                      type="number"
                      min={0}
                      max={360}
                      value={Math.round(((selected.heading_deg % 360) + 360) % 360)}
                      onChange={(e) =>
                        updateRegion(selected.id as string, {
                          heading_deg: ((num(e.target.value) % 360) + 360) % 360,
                        })
                      }
                      className="tnum w-14 rounded border border-edge/60 bg-ink/70 py-0.5 text-center text-[11px] text-slate-200"
                    />
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={360}
                    step={1}
                    value={((selected.heading_deg % 360) + 360) % 360}
                    onChange={(e) =>
                      updateRegion(selected.id as string, {
                        heading_deg: ((num(e.target.value) % 360) + 360) % 360,
                      })
                    }
                    className="w-full accent-[#22e3c4]"
                    title="Rotate the search area (degrees)"
                  />
                </label>

                {excludedNames.length > 0 && (
                  <p
                    className="text-[10px] leading-tight text-warn"
                    title="A DDS-bridge vehicle can't take a MISSION upload — it's left out of the survey"
                  >
                    {excludedNames.join(", ")} can't run surveys (no mission upload) —
                    excluded.
                  </p>
                )}
                <button
                  disabled={busy}
                  onClick={surveyWithFleet}
                  className="flex items-center justify-center gap-1.5 rounded-md bg-ok/20 px-3 py-2 text-xs font-semibold text-ok disabled:opacity-40"
                >
                  {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />}
                  Survey with fleet
                </button>

                <button
                  onClick={() => removeRegion(selected.id as string)}
                  className="flex items-center justify-center gap-1.5 rounded-md text-[11px] text-slate-400 hover:text-danger"
                >
                  <Trash2 size={13} /> Delete search area
                </button>
              </div>
            )}

            {fleetZones.length > 0 && (
              <div className="flex flex-col gap-1 border-t border-edge/50 pt-2">
                <span className="text-[10px] uppercase tracking-wide text-slate-400">
                  {fleetZones.some((z) => z.flying) ? "Assigned zones" : "Zone preview"}
                </span>
                {fleetZones.map((z, i) => (
                  <div key={z.vehicle} className="flex items-center gap-2 text-xs text-slate-200">
                    <span
                      className="h-3 w-3 shrink-0 rounded-sm"
                      style={{ background: zoneColor(z.name || z.vehicle, i) }}
                    />
                    <span className="truncate">{z.name}</span>
                    <span className={`ml-auto text-[10px] ${z.flying ? "text-ok" : "text-slate-500"}`}>
                      {z.flying ? "flying" : "preview"}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
