import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowUpFromLine, Battery, Clock, Download, Gauge, MapPin, PlayCircle, Radio, Route,
  Satellite, Sparkles, TrendingUp, X,
} from "lucide-react";
import jsPDF from "jspdf";
import autoTable from "jspdf-autotable";
import { useGcs } from "../store/useGcs";
import { api } from "../lib/api";
import type { FlightDetail, MissionDetail, MissionSummary } from "../lib/types";
import {
  buildTrajectoryUrl, fetchImageDataUrl, trajectoryColor,
} from "../lib/reportMap";

const LOGO_SRC = "/strato-logo.png";

// ── formatting helpers ───────────────────────────────────────────────────────
function fmtDate(ts: number | null): string {
  if (ts == null) return "—";
  return new Date(ts * 1000).toLocaleString([], {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}
function fmtTime(ts: number | null): string {
  if (ts == null) return "—";
  return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}
function fmtDuration(s: number | null): string {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}m ${sec.toString().padStart(2, "0")}s`;
}
function fmtCoord(c: { lat: number | null; lon: number | null } | null): string {
  if (!c || c.lat == null || c.lon == null) return "—";
  return `${c.lat.toFixed(6)}, ${c.lon.toFixed(6)}`;
}
function fmtDist(m: number): string {
  return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`;
}

// Avg ground speed derived from total distance / duration, when both exist.
function avgSpeed(flight: FlightDetail): number | null {
  if (flight.duration_s && flight.duration_s > 0) {
    return flight.distance_m / flight.duration_s;
  }
  return null;
}

// A mission's display label, e.g. "Overwatch + Outrider". Falls back to the
// flight count if no vehicle names were recorded.
function missionLabel(names: string[], flightCount: number): string {
  if (names.length) return names.join(" + ");
  return `${flightCount} flight${flightCount === 1 ? "" : "s"}`;
}

// Build the mode timeline with the duration each mode was held (until the next
// mode change, or until flight end for the last one).
function modeTimelineWithDurations(
  flight: FlightDetail,
): { mode: string; ts: number; held_s: number | null }[] {
  const tl = flight.mode_timeline;
  return tl.map((m, i) => {
    const next = i + 1 < tl.length ? tl[i + 1].ts : flight.end_ts;
    const held = next != null ? next - m.ts : null;
    return { mode: m.mode, ts: m.ts, held_s: held };
  });
}

const SEVERITY_LABEL: Record<number, string> = {
  0: "EMERG", 1: "ALERT", 2: "CRIT", 3: "ERROR",
  4: "WARN", 5: "NOTICE", 6: "INFO", 7: "DEBUG",
};

// Load the logo as a data URL once, so the PDF can embed it.
async function loadLogoDataUrl(): Promise<{ url: string; w: number; h: number } | null> {
  try {
    const res = await fetch(LOGO_SRC);
    const blob = await res.blob();
    const url = await new Promise<string>((resolve, reject) => {
      const r = new FileReader();
      r.onloadend = () => resolve(r.result as string);
      r.onerror = reject;
      r.readAsDataURL(blob);
    });
    const dims = await new Promise<{ w: number; h: number }>((resolve) => {
      const img = new Image();
      img.onload = () => resolve({ w: img.naturalWidth, h: img.naturalHeight });
      img.onerror = () => resolve({ w: 0, h: 0 });
      img.src = url;
    });
    return { url, w: dims.w, h: dims.h };
  } catch {
    return null;
  }
}

// ── PDF generation ───────────────────────────────────────────────────────────
const lastY = (doc: jsPDF) =>
  (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY;

// Render the dark-band header with the light-logo-chip. `subtitle` lines (e.g.
// the mission's drones + time) print right-aligned under the title.
function renderHeader(
  doc: jsPDF,
  pageW: number,
  margin: number,
  logo: { url: string; w: number; h: number } | null,
  title: string,
  subtitle: string[],
) {
  doc.setFillColor(11, 15, 23);
  doc.rect(0, 0, pageW, 86, "F");
  if (logo && logo.w > 0) {
    const h = 24;
    const w = (logo.w / logo.h) * h;
    const padX = 12;
    const padY = 9;
    const chipX = margin;
    const chipY = 18;
    // Light chip behind the (dark) logo for contrast.
    doc.setFillColor(244, 247, 250);
    doc.roundedRect(chipX, chipY, w + padX * 2, h + padY * 2, 8, 8, "F");
    try {
      doc.addImage(logo.url, "PNG", chipX + padX, chipY + padY, w, h);
    } catch {
      /* logo optional */
    }
  }
  doc.setTextColor(34, 227, 196);
  doc.setFont("helvetica", "bold");
  doc.setFontSize(18);
  doc.text(title, pageW - margin, 40, { align: "right" });
  doc.setTextColor(200, 210, 220);
  doc.setFont("helvetica", "normal");
  doc.setFontSize(10);
  subtitle.slice(0, 2).forEach((line, i) => {
    doc.text(line, pageW - margin, 58 + i * 14, { align: "right" });
  });
  doc.setTextColor(30, 30, 30);
}

// Render one flight's full body (summary, trajectory, stats, mode/action/event
// tables) into the doc starting at `y`. Returns the y after the last table.
// Preserves the graceful-fallback summary + trajectory behavior.
async function renderFlightBody(
  doc: jsPDF,
  pageW: number,
  margin: number,
  flight: FlightDetail,
  summary: string | null,
  startY: number,
): Promise<number> {
  let y = startY;

  // Per-drone band so each section is clearly attributed in a multi-drone PDF.
  doc.setFillColor(27, 36, 51);
  doc.rect(margin, y - 12, pageW - margin * 2, 22, "F");
  doc.setTextColor(34, 227, 196);
  doc.setFont("helvetica", "bold");
  doc.setFontSize(12);
  doc.text(
    `${flight.vehicle_name}  ·  ${fmtDate(flight.start_ts)}  ·  ${fmtDuration(flight.duration_s)}`,
    margin + 8,
    y + 3,
  );
  doc.setTextColor(30, 30, 30);
  y += 24;

  // Mission summary — operator-grade headline.
  if (summary && summary.trim()) {
    doc.setFontSize(11);
    doc.setFont("helvetica", "bold");
    doc.setTextColor(14, 142, 126);
    doc.text("SUMMARY", margin, y);
    y += 14;
    doc.setFont("helvetica", "normal");
    doc.setFontSize(9.5);
    doc.setTextColor(40, 40, 40);
    const wrapped = doc.splitTextToSize(summary.trim(), pageW - margin * 2);
    doc.text(wrapped, margin, y);
    y += wrapped.length * 12 + 14;
    doc.setTextColor(30, 30, 30);
  }

  // Satellite trajectory image. On failure (offline / no key) drop in a note.
  const mapUrl = buildTrajectoryUrl(flight.path, trajectoryColor(flight.vehicle_name));
  if (mapUrl) {
    try {
      const img = await fetchImageDataUrl(mapUrl);
      if (img.w > 0) {
        const imgW = pageW - margin * 2;
        const imgH = imgW * (img.h / img.w);
        doc.setDrawColor(14, 142, 126);
        doc.setLineWidth(1);
        doc.addImage(img.dataUrl, "PNG", margin, y, imgW, imgH);
        doc.rect(margin, y, imgW, imgH, "S");
        doc.setFontSize(8);
        doc.setTextColor(120, 120, 120);
        doc.text("Flight trajectory (satellite)", margin, y + imgH + 11);
        y += imgH + 24;
      }
    } catch {
      doc.setFontSize(9);
      doc.setTextColor(150, 90, 90);
      doc.text(
        "Trajectory map unavailable (offline or map service unreachable).",
        margin,
        y + 6,
      );
      y += 22;
      doc.setTextColor(30, 30, 30);
    }
  }

  // Summary stats table.
  const battUsed =
    flight.battery_used_pct != null ? `${flight.battery_used_pct}%` : "—";
  const battRange =
    flight.battery_start_pct != null
      ? `${flight.battery_start_pct}% → ${flight.battery_min_pct ?? "—"}%`
      : "—";
  const avg = avgSpeed(flight);
  autoTable(doc, {
    startY: y,
    head: [["Summary", ""]],
    body: [
      ["Vehicle", `${flight.vehicle_name} (${flight.vehicle_id})`],
      ["Flight ID", flight.id],
      ["Start", fmtDate(flight.start_ts)],
      ["End", fmtDate(flight.end_ts)],
      ["Duration", fmtDuration(flight.duration_s)],
      ["Max altitude (rel)", `${flight.max_alt_m.toFixed(1)} m`],
      ["Total distance", fmtDist(flight.distance_m)],
      ["Max ground speed", `${flight.max_speed_ms.toFixed(1)} m/s`],
      ["Avg ground speed", avg != null ? `${avg.toFixed(1)} m/s` : "—"],
      ["Battery start → min", battRange],
      ["Battery used", battUsed],
      ["Takeoff", fmtCoord(flight.takeoff)],
      ["Landing", fmtCoord(flight.landing)],
      ["Path samples", String(flight.path?.length ?? 0)],
    ],
    theme: "grid",
    styles: { fontSize: 9, cellPadding: 4 },
    headStyles: { fillColor: [14, 142, 126], textColor: 255, fontStyle: "bold" },
    columnStyles: { 0: { fontStyle: "bold", cellWidth: 160 } },
    margin: { left: margin, right: margin },
  });
  y = lastY(doc) + 18;

  // Mode timeline (with durations between mode changes).
  if (flight.mode_timeline.length) {
    autoTable(doc, {
      startY: y,
      head: [["Time", "Mode", "Held"]],
      body: modeTimelineWithDurations(flight).map((m) => [
        fmtTime(m.ts),
        m.mode,
        m.held_s != null ? fmtDuration(m.held_s) : "—",
      ]),
      theme: "striped",
      styles: { fontSize: 9, cellPadding: 4 },
      headStyles: { fillColor: [27, 36, 51], textColor: 255, fontStyle: "bold" },
      columnStyles: { 0: { cellWidth: 70 }, 2: { cellWidth: 80 } },
      margin: { left: margin, right: margin },
    });
    y = lastY(doc) + 18;
  }

  // Agent actions timeline (STADO's commanded actions, timestamped).
  const actions = flight.actions ?? [];
  if (actions.length) {
    autoTable(doc, {
      startY: y,
      head: [["Time", "Action", "Result"]],
      body: actions.map((a) => [fmtTime(a.ts), a.label, a.ok ? "OK" : "FAILED"]),
      theme: "striped",
      styles: { fontSize: 9, cellPadding: 4, overflow: "linebreak" },
      headStyles: { fillColor: [14, 142, 126], textColor: 255, fontStyle: "bold" },
      columnStyles: { 0: { cellWidth: 70 }, 2: { cellWidth: 70 } },
      margin: { left: margin, right: margin },
    });
    y = lastY(doc) + 18;
  }

  // Events.
  if (flight.events.length) {
    autoTable(doc, {
      startY: y,
      head: [["Time", "Severity", "Event"]],
      body: flight.events.map((e) => [
        fmtTime(e.ts),
        e.severity != null ? SEVERITY_LABEL[e.severity] ?? String(e.severity) : "—",
        e.text,
      ]),
      theme: "striped",
      styles: { fontSize: 9, cellPadding: 4, overflow: "linebreak" },
      headStyles: { fillColor: [27, 36, 51], textColor: 255, fontStyle: "bold" },
      columnStyles: { 0: { cellWidth: 60 }, 1: { cellWidth: 60 } },
      margin: { left: margin, right: margin },
    });
    y = lastY(doc) + 18;
  }
  return y;
}

function renderFooter(doc: jsPDF, pageW: number, margin: number) {
  const pages = doc.getNumberOfPages();
  for (let i = 1; i <= pages; i++) {
    doc.setPage(i);
    doc.setFontSize(8);
    doc.setTextColor(150, 150, 150);
    doc.text(
      `STRATO·GCS mission report — generated ${new Date().toLocaleString([], { hour12: false })}`,
      margin,
      doc.internal.pageSize.getHeight() - 20,
    );
    doc.text(`${i} / ${pages}`, pageW - margin, doc.internal.pageSize.getHeight() - 20, {
      align: "right",
    });
  }
}

// Whole-mission PDF: one dark-band header + light-logo-chip, then every member
// drone's full report stacked (each drone starts on a fresh page after the
// first), then a footer. `summaries` is keyed by flight id.
async function downloadMissionPdf(
  mission: MissionDetail,
  summaries: Record<string, string | null>,
) {
  const doc = new jsPDF({ unit: "pt", format: "a4" });
  const pageW = doc.internal.pageSize.getWidth();
  const margin = 40;
  const logo = await loadLogoDataUrl();

  const label = missionLabel(mission.names, mission.flight_ids.length);
  renderHeader(doc, pageW, margin, logo, "MISSION REPORT", [
    label,
    `${fmtDate(mission.t0)} · ${fmtDuration(mission.duration_s)}`,
  ]);

  let y = 104;
  for (let i = 0; i < mission.flights.length; i++) {
    const flight = mission.flights[i];
    if (i > 0) {
      doc.addPage();
      y = 56;
    }
    y = await renderFlightBody(doc, pageW, margin, flight, summaries[flight.id] ?? null, y);
  }

  renderFooter(doc, pageW, margin);
  doc.save(`mission-report-${mission.mission_id}.pdf`);
}

// ── small UI atoms ───────────────────────────────────────────────────────────
function StatCard({
  icon, label, value, sub,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
  sub?: string;
}) {
  return (
    <div className="glass instrument relative rounded-lg p-3">
      <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-slate-400">
        {icon}
        {label}
      </div>
      <div className="tnum mt-1 text-lg text-slate-100">{value}</div>
      {sub && <div className="text-[11px] text-slate-500">{sub}</div>}
    </div>
  );
}

function Logo({ className }: { className?: string }) {
  return (
    <img
      src={LOGO_SRC}
      alt="StratoFirma Autonomy Labs"
      className={className ?? "h-7 w-auto select-none"}
      draggable={false}
      style={{
        filter:
          "brightness(0) invert(1) drop-shadow(0 0 5px rgba(34,227,196,0.65)) drop-shadow(0 0 16px rgba(34,227,196,0.35))",
      }}
    />
  );
}

// ── one drone's report column ─────────────────────────────────────────────────
// Renders a single member flight's report (summary, trajectory, stats, mode +
// action + event timelines) as one responsive column. Fetches its own satellite
// trajectory image + model-written summary, and reports its summary text up to the
// mission via `onSummary` so the PDF export can include it.
function FlightColumn({
  flight,
  onSummary,
}: {
  flight: FlightDetail;
  onSummary: (id: string, summary: string | null) => void;
}) {
  const [mapUrl, setMapUrl] = useState<string | null>(null);
  const [mapState, setMapState] = useState<"idle" | "loading" | "ready" | "error">("idle");
  const [summary, setSummary] = useState<string | null>(null);
  const [summaryState, setSummaryState] = useState<"idle" | "loading" | "ready" | "error">("idle");

  // Trajectory image (graceful fallback note on offline / no-key).
  useEffect(() => {
    const url = buildTrajectoryUrl(flight.path, trajectoryColor(flight.vehicle_name));
    if (!url) {
      setMapUrl(null);
      setMapState("error");
      return;
    }
    let cancelled = false;
    setMapState("loading");
    fetchImageDataUrl(url)
      .then((img) => {
        if (cancelled) return;
        if (img.w > 0) {
          setMapUrl(img.dataUrl);
          setMapState("ready");
        } else {
          setMapState("error");
        }
      })
      .catch(() => !cancelled && setMapState("error"));
    return () => {
      cancelled = true;
    };
  }, [flight.id, flight.path, flight.vehicle_name]);

  // Model-written summary (reuse the cached one on the detail when present).
  useEffect(() => {
    if (flight.summary && flight.summary.trim()) {
      setSummary(flight.summary);
      setSummaryState("ready");
      onSummary(flight.id, flight.summary);
      return;
    }
    let cancelled = false;
    setSummary(null);
    setSummaryState("loading");
    api
      .flightSummary(flight.id)
      .then((res) => {
        if (cancelled) return;
        setSummary(res.summary);
        setSummaryState("ready");
        onSummary(flight.id, res.summary);
      })
      .catch(() => {
        if (!cancelled) {
          setSummaryState("error");
          onSummary(flight.id, null);
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flight.id]);

  const color = trajectoryColor(flight.vehicle_name);

  return (
    <div className="min-w-0">
      {/* column header — vehicle name with its fleet color accent */}
      <div className="glass instrument relative mb-4 flex items-center gap-3 rounded-xl px-4 py-3">
        <span className="h-3 w-3 shrink-0 rounded-full" style={{ background: color }} />
        <div className="min-w-0 flex-1">
          <div className="truncate text-base font-bold text-slate-100">
            {flight.vehicle_name}
            <span className="ml-2 text-xs font-normal text-slate-500">{flight.vehicle_id}</span>
          </div>
          <div className="text-xs text-slate-400">
            {fmtDate(flight.start_ts)} · {fmtDuration(flight.duration_s)}
          </div>
        </div>
      </div>

      {/* summary */}
      <div className="mb-4">
        <div className="mb-2 flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-slate-500">
          <Sparkles size={12} /> Summary
        </div>
        <div className="glass instrument relative rounded-xl p-4">
          {summaryState === "loading" && (
            <div className="text-xs text-slate-500">Generating mission summary…</div>
          )}
          {summaryState === "ready" && summary && (
            <p className="text-sm leading-relaxed text-slate-200">{summary}</p>
          )}
          {summaryState === "error" && (
            <div className="text-xs text-slate-500">Mission summary unavailable.</div>
          )}
          {summaryState === "idle" && <div className="text-xs text-slate-600">No summary.</div>}
        </div>
      </div>

      {/* satellite trajectory */}
      <div className="mb-4">
        <div className="mb-2 flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-slate-500">
          <Satellite size={12} /> Trajectory
        </div>
        <div className="glass instrument relative overflow-hidden rounded-xl p-2">
          {mapState === "loading" && (
            <div className="flex h-56 items-center justify-center text-xs text-slate-500">
              Loading satellite imagery…
            </div>
          )}
          {mapState === "ready" && mapUrl && (
            <img
              src={mapUrl}
              alt="Flight trajectory on satellite imagery"
              className="w-full rounded-lg"
              draggable={false}
            />
          )}
          {mapState === "error" && (
            <div className="flex h-56 flex-col items-center justify-center gap-1 text-center text-xs text-slate-500">
              <Satellite size={20} className="text-slate-600" />
              <div>Trajectory map unavailable.</div>
              <div className="text-slate-600">Offline, no map key, or insufficient path data.</div>
            </div>
          )}
          {mapState === "idle" && (
            <div className="flex h-56 items-center justify-center text-xs text-slate-600">
              No trajectory.
            </div>
          )}
        </div>
      </div>

      {/* stats grid */}
      <div className="mb-4 grid grid-cols-2 gap-3">
        <StatCard
          icon={<ArrowUpFromLine size={12} />}
          label="Max Altitude"
          value={`${flight.max_alt_m.toFixed(1)} m`}
        />
        <StatCard icon={<Route size={12} />} label="Distance" value={fmtDist(flight.distance_m)} />
        <StatCard
          icon={<Gauge size={12} />}
          label="Max Speed"
          value={`${flight.max_speed_ms.toFixed(1)} m/s`}
        />
        <StatCard
          icon={<TrendingUp size={12} />}
          label="Avg Speed"
          value={avgSpeed(flight) != null ? `${avgSpeed(flight)!.toFixed(1)} m/s` : "—"}
        />
        <StatCard
          icon={<Clock size={12} />}
          label="Duration"
          value={fmtDuration(flight.duration_s)}
        />
        <StatCard
          icon={<Battery size={12} />}
          label="Battery Used"
          value={flight.battery_used_pct != null ? `${flight.battery_used_pct}%` : "—"}
          sub={
            flight.battery_start_pct != null
              ? `${flight.battery_start_pct}% → ${flight.battery_min_pct ?? "—"}%`
              : undefined
          }
        />
        <StatCard
          icon={<MapPin size={12} />}
          label="Takeoff / Landing"
          value={fmtCoord(flight.takeoff)}
          sub={fmtCoord(flight.landing)}
        />
      </div>

      {/* mode timeline */}
      <div className="mb-4">
        <div className="mb-2 text-[10px] uppercase tracking-wider text-slate-500">
          Mode Timeline ({flight.mode_timeline.length})
        </div>
        <div className="glass instrument relative rounded-xl p-4">
          {flight.mode_timeline.length === 0 ? (
            <div className="text-xs text-slate-600">No mode changes recorded.</div>
          ) : (
            <div className="space-y-2">
              {modeTimelineWithDurations(flight).map((m, i) => (
                <div key={i} className="flex items-center gap-3">
                  <span className="tnum w-20 text-[11px] text-slate-500">{fmtTime(m.ts)}</span>
                  <span className="h-1.5 w-1.5 rounded-full bg-accent" />
                  <span className="rounded bg-accent/10 px-2 py-0.5 text-xs font-semibold text-accent">
                    {m.mode}
                  </span>
                  {m.held_s != null && (
                    <span className="tnum text-[11px] text-slate-500">
                      held {fmtDuration(m.held_s)}
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* agent actions */}
      <div className="mb-4">
        <div className="mb-2 flex items-center gap-1.5 text-[10px] uppercase tracking-wider text-slate-500">
          <Radio size={12} /> Agent Actions ({flight.actions?.length ?? 0})
        </div>
        <div className="glass instrument relative rounded-xl p-4">
          {(flight.actions?.length ?? 0) === 0 ? (
            <div className="text-xs text-slate-600">No agent actions recorded.</div>
          ) : (
            <div className="space-y-2">
              {flight.actions!.map((a, i) => (
                <div key={i} className="flex items-center gap-3">
                  <span className="tnum w-20 text-[11px] text-slate-500">{fmtTime(a.ts)}</span>
                  <span className={`h-1.5 w-1.5 rounded-full ${a.ok ? "bg-accent" : "bg-danger"}`} />
                  <span className="text-xs font-semibold text-slate-200">{a.label}</span>
                  {!a.ok && (
                    <span className="rounded bg-danger/15 px-1.5 py-0.5 text-[10px] font-semibold text-danger">
                      FAILED
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* events */}
      <div className="mb-4">
        <div className="mb-2 text-[10px] uppercase tracking-wider text-slate-500">
          Events ({flight.events.length})
        </div>
        <div className="glass instrument relative rounded-xl p-4">
          {flight.events.length === 0 ? (
            <div className="text-xs text-slate-600">No notable events recorded.</div>
          ) : (
            <div className="space-y-1 font-mono text-[11px]">
              {flight.events.map((e, i) => (
                <div key={i} className="flex gap-2">
                  <span className="text-slate-600">{fmtTime(e.ts)}</span>
                  <span
                    className={
                      e.severity != null && e.severity <= 3
                        ? "text-danger"
                        : e.severity === 4
                          ? "text-warn"
                          : "text-slate-300"
                    }
                  >
                    {e.severity != null && e.severity <= 4
                      ? `[${SEVERITY_LABEL[e.severity]}] `
                      : ""}
                    {e.text}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── main overlay ─────────────────────────────────────────────────────────────
export default function ReportPage() {
  const reportOpen = useGcs((s) => s.reportOpen);
  const setReportOpen = useGcs((s) => s.setReportOpen);
  const startMissionReplay = useGcs((s) => s.startMissionReplay);
  const pushLog = useGcs((s) => s.pushLog);

  const [missions, setMissions] = useState<MissionSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<MissionDetail | null>(null);
  const [loadingList, setLoadingList] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Per-flight summaries collected from the columns, for the PDF export.
  const [summaries, setSummaries] = useState<Record<string, string | null>>({});

  // Replay the WHOLE selected mission (all drones together) and close the report.
  const replayMission = (m: MissionDetail) => {
    startMissionReplay(m);
    setReportOpen(false);
  };

  // Refresh the mission list whenever the page opens.
  useEffect(() => {
    if (!reportOpen) return;
    let cancelled = false;
    setLoadingList(true);
    setError(null);
    api
      .missions()
      .then((ms) => {
        if (cancelled) return;
        setMissions(ms);
        setSelectedId((cur) => cur ?? (ms[0]?.mission_id ?? null));
      })
      .catch(() => !cancelled && setError("Failed to load missions"))
      .finally(() => !cancelled && setLoadingList(false));
    return () => {
      cancelled = true;
    };
  }, [reportOpen]);

  // Load the selected mission's full detail (all member flights).
  useEffect(() => {
    if (!reportOpen || !selectedId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setLoadingDetail(true);
    setSummaries({});
    api
      .mission(selectedId)
      .then((d) => !cancelled && setDetail(d))
      .catch(() => !cancelled && setDetail(null))
      .finally(() => !cancelled && setLoadingDetail(false));
    return () => {
      cancelled = true;
    };
  }, [reportOpen, selectedId]);

  // Close on Escape.
  useEffect(() => {
    if (!reportOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setReportOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [reportOpen, setReportOpen]);

  const onColumnSummary = (id: string, summary: string | null) =>
    setSummaries((prev) => ({ ...prev, [id]: summary }));

  // Responsive column grid: 1 col on small screens, else one column per drone
  // (capped so very wide layouts don't get unreadably thin).
  const colCount = Math.min(detail?.flights.length ?? 1, 3);
  const gridCols =
    colCount >= 3 ? "lg:grid-cols-3" : colCount === 2 ? "lg:grid-cols-2" : "lg:grid-cols-1";

  return (
    <AnimatePresence>
      {reportOpen && (
        <motion.div
          key="report"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.18 }}
          className="absolute inset-0 z-50 flex flex-col bg-ink/92 backdrop-blur-sm"
          style={{ background: "rgba(5,7,11,0.92)" }}
        >
          {/* header */}
          <div className="glass instrument relative flex items-center gap-4 border-b border-edge/60 px-5 py-3">
            <Logo />
            <div className="h-6 w-px bg-edge/60" />
            <div>
              <div className="text-sm font-bold tracking-wide text-accent">MISSION REPORTS</div>
              <div className="text-[11px] text-slate-400">
                {missions.length} mission{missions.length === 1 ? "" : "s"}
              </div>
            </div>
            <div className="flex-1" />
            <button
              onClick={() => setReportOpen(false)}
              className="flex items-center gap-1.5 rounded-md bg-edge/40 px-3 py-1.5 text-xs font-semibold text-slate-300 transition-colors hover:bg-danger/20 hover:text-danger"
              title="Close (Esc)"
            >
              <X size={16} />
              Close
            </button>
          </div>

          {/* body: mission list (left) + side-by-side reports (right) */}
          <div className="flex min-h-0 flex-1">
            {/* mission list */}
            <div className="flex w-72 shrink-0 flex-col border-r border-edge/60 bg-panel/40">
              <div className="px-4 py-2 text-[10px] uppercase tracking-wider text-slate-500">
                Missions
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
                {loadingList && <div className="px-2 py-4 text-xs text-slate-500">Loading…</div>}
                {error && <div className="px-2 py-4 text-xs text-danger">{error}</div>}
                {!loadingList && !error && missions.length === 0 && (
                  <div className="px-2 py-4 text-xs text-slate-600">
                    No missions recorded yet. A mission appears here after one or
                    more drones arm and disarm.
                  </div>
                )}
                {missions.map((m) => {
                  const active = m.mission_id === selectedId;
                  return (
                    <button
                      key={m.mission_id}
                      onClick={() => setSelectedId(m.mission_id)}
                      className={`mb-1.5 block w-full rounded-lg border px-3 py-2 text-left transition-colors ${
                        active
                          ? "border-accent/50 bg-accent/10 glow-accent"
                          : "border-edge/40 bg-panel/60 hover:border-edge hover:bg-edge/40"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-sm font-semibold text-slate-100">
                          {missionLabel(m.names, m.flight_count)}
                        </span>
                        <span className="tnum shrink-0 text-[11px] text-slate-400">
                          {fmtDuration(m.duration_s)}
                        </span>
                      </div>
                      <div className="mt-0.5 text-[11px] text-slate-500">{fmtDate(m.t0)}</div>
                      <div className="tnum mt-1 flex gap-3 text-[10px] text-slate-400">
                        <span>
                          {m.names.length} drone{m.names.length === 1 ? "" : "s"} · {m.flight_count} flight
                          {m.flight_count === 1 ? "" : "s"}
                        </span>
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>

            {/* mission detail — side-by-side drone columns */}
            <div className="min-h-0 flex-1 overflow-y-auto p-6">
              {!selectedId && (
                <div className="flex h-full items-center justify-center text-sm text-slate-600">
                  Select a mission to view its report.
                </div>
              )}
              {selectedId && loadingDetail && !detail && (
                <div className="text-sm text-slate-500">Loading report…</div>
              )}
              {detail && (
                <div className="mx-auto max-w-[1400px]">
                  {/* mission-level header */}
                  <div className="glass instrument relative mb-6 flex items-center gap-4 rounded-xl px-5 py-4">
                    <Logo className="h-8 w-auto select-none" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-lg font-bold text-slate-100">
                        {missionLabel(detail.names, detail.flights.length)}
                      </div>
                      <div className="text-xs text-slate-400">
                        {fmtDate(detail.t0)} · {fmtDuration(detail.duration_s)} ·{" "}
                        {detail.names.length} drone{detail.names.length === 1 ? "" : "s"} ·{" "}
                        {detail.flights.length} flight{detail.flights.length === 1 ? "" : "s"}
                      </div>
                    </div>
                    <button
                      onClick={() => replayMission(detail)}
                      disabled={detail.flights.every((f) => (f.path?.length ?? 0) < 2)}
                      className="flex items-center gap-2 rounded-lg bg-edge/50 px-4 py-2 text-sm font-semibold text-slate-100 transition-colors hover:bg-edge/70 disabled:opacity-40"
                      title="Replay the whole mission (all drones) on the map"
                    >
                      <PlayCircle size={16} />
                      Replay mission
                    </button>
                    <button
                      onClick={() =>
                        downloadMissionPdf(detail, summaries).catch((e) =>
                          pushLog("error", `PDF export failed: ${(e as Error).message}`, 3),
                        )
                      }
                      className="flex items-center gap-2 rounded-lg bg-accent/15 px-4 py-2 text-sm font-semibold text-accent glow-accent transition-colors hover:bg-accent/25"
                      title="Download a PDF of every drone in the mission"
                    >
                      <Download size={16} />
                      Download PDF
                    </button>
                  </div>

                  {/* per-drone columns, side by side */}
                  <div className={`grid grid-cols-1 gap-6 ${gridCols}`}>
                    {detail.flights.map((f) => (
                      <FlightColumn key={f.id} flight={f} onSummary={onColumnSummary} />
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
