import { useEffect, useRef, useState } from "react";
import { Car, Crosshair, EyeOff, Orbit, PersonStanding, Play, Radio, ScanSearch, Square, Video, VideoOff } from "lucide-react";
import { ALLY_MARKER_TTL_MS, useGcs } from "../store/useGcs";
import { api, MJPEG_URL } from "../lib/api";
import { useGo2RtcWebRTC } from "../lib/useGo2Rtc";
import { useConnectedGate } from "../lib/useConnectedGate";
import type { VehicleId } from "../lib/types";
import VideoFrame from "./VideoFrame";
import RecordButton from "./RecordButton";
import StaleTelemetryOverlay from "./StaleTelemetryOverlay";

const STREAM = import.meta.env.VITE_GO2RTC_STREAM ?? "drone";

// This panel shows OVERWATCH's feed — render it whenever Overwatch is CONNECTED
// (display-only; not gated on being the command-active drone). So when both
// drones are connected, both feeds show; an offline Overwatch shows no feed UI.
const VEHICLE_ID = "overwatch";

const ACCENT = "#22e3c4"; // cockpit teal
const ALLY = "#ffb020"; // amber — friendly ally marker (distinct from red/green targets)

// Draw the AR ally marker: an amber diamond at (u·w, v·h) with a label + range,
// clearly different from the red/green target reticles. When the ally is out of
// frame (in_view false) we instead pin an edge chevron pointing toward it. `pulse`
// is the shared 0..1 breathing phase from the rAF loop.
function drawAllyMarker(
  ctx: CanvasRenderingContext2D,
  m: { label: string; u: number; v: number; range_m: number; in_view: boolean; behind: boolean },
  w: number,
  h: number,
  pulse: number,
) {
  const rng = m.range_m >= 1000 ? `${(m.range_m / 1000).toFixed(1)} km` : `${Math.round(m.range_m)} m`;
  ctx.save();
  ctx.shadowColor = ALLY;

  if (m.in_view) {
    const x = m.u * w;
    const y = m.v * h;
    const r = 9 + pulse * 2;
    // Amber diamond outline + faint fill.
    ctx.shadowBlur = 8 + pulse * 6;
    ctx.strokeStyle = ALLY;
    ctx.lineWidth = 2;
    ctx.fillStyle = "rgba(255,176,32,0.14)";
    ctx.beginPath();
    ctx.moveTo(x, y - r);
    ctx.lineTo(x + r, y);
    ctx.lineTo(x, y + r);
    ctx.lineTo(x - r, y);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    // center dot
    ctx.beginPath();
    ctx.arc(x, y, 1.5, 0, Math.PI * 2);
    ctx.fillStyle = ALLY;
    ctx.fill();
    // Label + range above the diamond, flipping below if it'd clip the top.
    ctx.shadowBlur = 4;
    ctx.font = "bold 12px ui-sans-serif, system-ui, sans-serif";
    const text = `${m.label}  ·  ${rng}`;
    const tw = ctx.measureText(text).width;
    let tx = x - tw / 2;
    tx = Math.max(2, Math.min(tx, w - tw - 2));
    const ty = y - r - 7 < 12 ? y + r + 16 : y - r - 7;
    ctx.fillStyle = "rgba(10,12,16,0.7)";
    ctx.fillRect(tx - 4, ty - 12, tw + 8, 16);
    ctx.fillStyle = ALLY;
    ctx.fillText(text, tx, ty);
  } else {
    // Out of frame — pin an edge chevron pointing toward the ally. u,v are
    // clamped to the frame edge by the backend, so they already indicate which
    // edge it left from; nudge inward so the arrow + label stay visible.
    const ex = Math.max(18, Math.min(m.u * w, w - 18));
    const ey = Math.max(18, Math.min(m.v * h, h - 18));
    // Direction from center toward the (clamped) edge point.
    const ang = Math.atan2(ey - h / 2, ex - w / 2);
    const a = 9 + pulse * 2;
    ctx.translate(ex, ey);
    ctx.rotate(ang);
    ctx.shadowBlur = 8;
    ctx.strokeStyle = ALLY;
    ctx.fillStyle = "rgba(255,176,32,0.18)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(a, 0);
    ctx.lineTo(-a * 0.7, a * 0.7);
    ctx.lineTo(-a * 0.7, -a * 0.7);
    ctx.closePath();
    ctx.fill();
    ctx.stroke();
    ctx.rotate(-ang);
    ctx.translate(-ex, -ey);
    // Compact label by the chevron.
    ctx.shadowBlur = 4;
    ctx.font = "bold 11px ui-sans-serif, system-ui, sans-serif";
    const text = `${m.label} ${rng}`;
    const tw = ctx.measureText(text).width;
    let tx = ex - tw / 2;
    tx = Math.max(2, Math.min(tx, w - tw - 2));
    const ty = ey > h / 2 ? ey - 14 : ey + 22;
    ctx.fillStyle = "rgba(10,12,16,0.7)";
    ctx.fillRect(tx - 4, ty - 11, tw + 8, 15);
    ctx.fillStyle = ALLY;
    ctx.fillText(text, tx, ty);
  }
  ctx.restore();
}

// Render the anonymized vehicle-ID card next to a track box (top-right of the
// box). Flips to the left of the box if it would run off the canvas. Mirrors the
// backend's _draw_vehicle_card layout: cyan bold plate, then location, model,
// class/fuel/year, masked owner, and a dim mock tag. Coords are in canvas px.
function drawVehicleCard(
  ctx: CanvasRenderingContext2D,
  info: VehicleId["info"],
  boxRight: number,
  boxTop: number,
  cw: number,
  ch: number,
) {
  const plate = info.plate || "—";
  const loc = info.rto_code ? `${info.state} — ${info.rto_code}` : info.state || "?";
  // [text, color, fontPx, bold]
  const lines: [string, string, number, boolean][] = [
    [plate, ACCENT, 16, true],
    [loc, "rgb(220,220,220)", 12, false],
    [info.maker_model || "unknown model", "rgb(255,255,255)", 12, false],
  ];
  const meta = [info.vehicle_class, info.fuel, info.reg_year].filter(Boolean).join(" / ");
  if (meta) lines.push([meta, "rgb(200,200,200)", 12, false]);
  if (info.owner) lines.push([`Owner: ${info.owner}`, "rgb(160,220,255)", 12, false]);
  if (info.source === "mock") lines.push(["[mock — anonymized]", "rgb(140,140,140)", 11, false]);

  const pad = 8;
  const lineH = 20;
  // Measure widest line.
  let wmax = 0;
  for (const [text, , fontPx, bold] of lines) {
    ctx.font = `${bold ? "bold " : ""}${fontPx}px ui-sans-serif, system-ui, sans-serif`;
    wmax = Math.max(wmax, ctx.measureText(text).width);
  }
  const cardW = wmax + pad * 2;
  const cardH = lineH * lines.length + pad;

  // Anchor top-right of the box; flip to the left if it overflows the canvas.
  let x0 = boxRight + 10;
  const y0 = Math.max(0, Math.min(boxTop, ch - cardH));
  if (x0 + cardW > cw) x0 = Math.max(0, boxRight - cardW - 10);

  // Semi-transparent dark panel + cyan border.
  ctx.save();
  ctx.fillStyle = "rgba(20,20,20,0.78)";
  ctx.fillRect(x0, y0, cardW, cardH);
  ctx.strokeStyle = ACCENT;
  ctx.lineWidth = 1;
  ctx.strokeRect(x0 + 0.5, y0 + 0.5, cardW - 1, cardH - 1);

  ctx.textBaseline = "alphabetic";
  let ty = y0 + pad + 12;
  for (const [text, color, fontPx, bold] of lines) {
    ctx.font = `${bold ? "bold " : ""}${fontPx}px ui-sans-serif, system-ui, sans-serif`;
    ctx.fillStyle = color;
    ctx.fillText(text, x0 + pad, ty);
    ty += lineH;
  }
  ctx.restore();
}

// Vision always ingests the OVERWATCH feed via go2rtc's H.264 RTSP restream.
// go2rtc transcodes the SIYI feed to H.264 (which OpenCV decodes reliably,
// unlike the raw SIYI HEVC or an unloaded Feed 2), so the pipeline can always
// "see images".
const OVERWATCH_RTSP = "rtsp://127.0.0.1:8554/drone";

/** FPV — raw low-latency WebRTC from go2rtc, with a click-select track overlay. */
function FpvView({ onStateChange }: { onStateChange?: (live: boolean) => void }) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const state = useGo2RtcWebRTC(videoRef, STREAM, true);
  // Report the go2rtc media state UP so VideoPanel can decide between a
  // non-blocking "telemetry stale" badge (frame still playing) and the dimming
  // "reconnecting…" curtain (no frame) when telemetry drops. The video streams
  // independently of telemetry, so a stale link must NOT black out a live frame.
  useEffect(() => {
    onStateChange?.(state === "live");
  }, [state, onStateChange]);
  const { tracks, selectedTrack, lockActive, vehicleId } = useGcs();
  // AR ally markers (Outrider projected into Overwatch's feed). Read into a ref
  // so the rAF loop sees the latest without re-subscribing every frame, and a
  // count drives the effect re-run so the loop (re)starts when a marker appears.
  const allyMarkers = useGcs((s) => s.allyMarkers);
  const pruneAllyMarkers = useGcs((s) => s.pruneAllyMarkers);
  const visionRunning = useGcs((s) => s.visionRunning);
  const setVisionRunning = useGcs((s) => s.setVisionRunning);
  const pushLog = useGcs((s) => s.pushLog);
  const canvas = useRef<HTMLCanvasElement>(null);
  // Latest ally markers, mirrored into a ref so the reticle's rAF loop reads them
  // live (like dragRef) without the loop re-subscribing each frame.
  const allyRef = useRef(allyMarkers);
  allyRef.current = allyMarkers;

  // ── Click-to-track (drag a box → seed the CSRT tracker) ──────────────────
  // Gated behind a SELECT toggle so it never clashes with the panel's drag /
  // double-click-to-focus. While SELECT is on, pointerdown→move→up on the
  // canvas draws a rubber-band rectangle; on release we normalize it (0..1 of
  // the canvas, matching how the reticle un-normalizes `tracks`) and POST it to
  // /api/vision/seed_box. The backend locks CSRT on that ROI and the normal
  // reticle takes over from the reported track.
  const [selectMode, setSelectMode] = useState(false);
  // Mirror select mode to the store so VideoPanel can disable the panel's drag
  // while the operator is drag-drawing a click-to-track box.
  const setBoxSelecting = useGcs((s) => s.setBoxSelecting);
  useEffect(() => {
    setBoxSelecting(selectMode);
    return () => setBoxSelecting(false);
  }, [selectMode, setBoxSelecting]);
  const [seeding, setSeeding] = useState(false);
  // Live rubber-band rect in CANVAS pixels (null when not dragging). Stored in a
  // ref so the reticle's rAF loop can read it without re-subscribing each frame.
  const dragRef = useRef<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const [dragging, setDragging] = useState(false); // drives a redraw while idle (no lock)

  const seedFromDrag = async (r: { x0: number; y0: number; x1: number; y1: number }) => {
    const cv = canvas.current;
    if (!cv) return;
    const { width, height } = cv.getBoundingClientRect();
    if (width < 2 || height < 2) return;
    const nx = Math.min(r.x0, r.x1) / width;
    const ny = Math.min(r.y0, r.y1) / height;
    const nw = Math.abs(r.x1 - r.x0) / width;
    const nh = Math.abs(r.y1 - r.y0) / height;
    if (nw < 0.01 || nh < 0.01) return; // ignore an accidental tap / sliver
    const box = { x: nx, y: ny, w: nw, h: nh };
    setSeeding(true);
    try {
      // Auto-start the vision pipeline if it isn't running yet — mirrors what
      // TrackView's START button does — so the operator can lock a target
      // straight off the live feed without first switching to TRACK mode and
      // pressing START. seed_box needs a live pipeline to lock CSRT onto the ROI.
      const justStarted = !useGcs.getState().visionRunning;
      if (justStarted) {
        await api.vision.start(OVERWATCH_RTSP);
        setVisionRunning(true);
      }

      // SEED RACE FIX: right after start() the pipeline has no frame yet, so an
      // immediate seed_box returns "no frame" (or grabs a stray YOLO box) and the
      // lock fails. RETRY the seed every ~300ms (up to ~3s) until the pipeline has
      // a frame and the ROI locks. We seed-then-check, treating a "no frame"
      // result (returned `{ok:false}` OR a thrown 4xx whose body says so) as
      // retryable; any other failure is reported immediately.
      const RETRY_MS = 300;
      const MAX_TRIES = 10; // 10 × 300ms ≈ 3s
      const noFrame = (s: string | undefined) => !!s && /no\s*frame|frame/i.test(s);
      let locked = false;
      let lastReason: string | undefined;
      for (let attempt = 0; attempt < MAX_TRIES && !locked; attempt++) {
        if (attempt > 0) await new Promise((res) => setTimeout(res, RETRY_MS));
        try {
          const res = (await api.vision.seedBox(box)) as { ok?: boolean; reason?: string };
          if (res && res.ok === false) {
            lastReason = res.reason ?? "seed failed";
            // Retry only while the pipeline simply has no frame yet; any other
            // rejection (e.g. bad ROI) is terminal — stop and report it.
            if (!noFrame(lastReason)) break;
          } else {
            locked = true;
          }
        } catch (e) {
          lastReason = (e as Error).message;
          // A "no frame" 4xx surfaces here as a thrown error — keep retrying.
          if (!noFrame(lastReason)) throw e;
        }
      }

      if (locked) {
        pushLog("track", "Target locked (manual select)");
        setSelectMode(false); // one-shot: drop back to normal interaction
      } else {
        // Still couldn't seed after the retries — log a single failure.
        pushLog("track", lastReason ?? "seed failed (no frame)");
      }
    } catch (e) {
      pushLog("error", `seed_box: ${(e as Error).message}`, 3);
    } finally {
      setSeeding(false);
    }
  };

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!selectMode) return;
    e.preventDefault();
    e.stopPropagation();
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    dragRef.current = { x0: x, y0: y, x1: x, y1: y };
    setDragging(true);
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!selectMode || !dragRef.current) return;
    e.preventDefault();
    e.stopPropagation();
    const rect = e.currentTarget.getBoundingClientRect();
    dragRef.current = {
      ...dragRef.current,
      x1: e.clientX - rect.left,
      y1: e.clientY - rect.top,
    };
    // No setState needed: the rAF loop started on pointer-down self-sustains
    // (it re-requests frames while dragRef is non-null) and reads dragRef live.
  };
  const onPointerUp = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (!selectMode || !dragRef.current) return;
    e.preventDefault();
    e.stopPropagation();
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* capture may already be gone — ignore */
    }
    const r = dragRef.current;
    dragRef.current = null;
    setDragging(false);
    void seedFromDrag(r);
  };

  useEffect(() => {
    const cv = canvas.current;
    if (!cv) return;
    const ctx = cv.getContext("2d")!;

    let raf = 0;

    // Defense-grade targeting reticle: bright corner brackets + center
    // crosshair + colored glow. Locked target = red, others = green. The
    // locked target gets a gentle pulse + a slowly rotating outer tick ring.
    const LOCK = "#ff3b3b"; // bright red — locked / selected
    const TRACK = "#22ff88"; // bright green — other tracked objects

    const draw = (now: number) => {
      const { width, height } = cv.getBoundingClientRect();
      if (cv.width !== width) cv.width = width;
      if (cv.height !== height) cv.height = height;
      ctx.clearRect(0, 0, width, height);

      // 0..1 phase that completes a cycle every ~1.6s, for the pulse.
      const pulse = 0.5 + 0.5 * Math.sin((now / 1600) * Math.PI * 2);
      // Slow rotation of the outer tick ring, full turn every ~6s.
      const spin = (now / 6000) * Math.PI * 2;

      for (const t of tracks) {
        const sel = t.id === selectedTrack;
        const color = sel ? LOCK : TRACK;
        const bx = t.x * width;
        const by = t.y * height;
        const bw = t.w * width;
        const bh = t.h * height;
        const cx = bx + bw / 2;
        const cy = by + bh / 2;

        // Corner bracket arm length scales with box size but stays sane.
        const arm = Math.max(8, Math.min(bw, bh) * 0.22);
        // Locked target gets a subtle breathing inset so brackets "pulse".
        const inset = sel ? pulse * 3 : 0;
        const lx = bx + inset;
        const ty = by + inset;
        const rx = bx + bw - inset;
        const dy = by + bh - inset;

        ctx.save();
        ctx.shadowColor = color;
        ctx.shadowBlur = sel ? 12 + pulse * 8 : 6;
        ctx.strokeStyle = color;
        ctx.lineWidth = sel ? 2.5 : 2;
        ctx.lineCap = "round";

        // L-shaped corner brackets.
        ctx.beginPath();
        // top-left
        ctx.moveTo(lx, ty + arm);
        ctx.lineTo(lx, ty);
        ctx.lineTo(lx + arm, ty);
        // top-right
        ctx.moveTo(rx - arm, ty);
        ctx.lineTo(rx, ty);
        ctx.lineTo(rx, ty + arm);
        // bottom-right
        ctx.moveTo(rx, dy - arm);
        ctx.lineTo(rx, dy);
        ctx.lineTo(rx - arm, dy);
        // bottom-left
        ctx.moveTo(lx + arm, dy);
        ctx.lineTo(lx, dy);
        ctx.lineTo(lx, dy - arm);
        ctx.stroke();

        // Center crosshair — small +/cross with a gap in the middle.
        const ch = Math.max(6, Math.min(bw, bh) * 0.12);
        const gap = ch * 0.45;
        ctx.beginPath();
        ctx.moveTo(cx - ch, cy);
        ctx.lineTo(cx - gap, cy);
        ctx.moveTo(cx + gap, cy);
        ctx.lineTo(cx + ch, cy);
        ctx.moveTo(cx, cy - ch);
        ctx.lineTo(cx, cy - gap);
        ctx.moveTo(cx, cy + gap);
        ctx.lineTo(cx, cy + ch);
        ctx.stroke();
        // tiny center dot
        ctx.beginPath();
        ctx.arc(cx, cy, 1.3, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();

        // Locked target — slowly rotating outer tick ring for that HUD feel.
        if (sel) {
          const ring = Math.max(bw, bh) * 0.5 + 10 + pulse * 3;
          ctx.lineWidth = 1.6;
          for (let i = 0; i < 4; i++) {
            const a = spin + (i * Math.PI) / 2;
            const x1 = cx + Math.cos(a) * ring;
            const y1 = cy + Math.sin(a) * ring;
            const x2 = cx + Math.cos(a) * (ring + 7);
            const y2 = cy + Math.sin(a) * (ring + 7);
            ctx.beginPath();
            ctx.moveTo(x1, y1);
            ctx.lineTo(x2, y2);
            ctx.stroke();
          }
        }
        ctx.restore();
      }

      // Anonymized vehicle-ID card — mirrors the backend's TRACK overlay. Drawn
      // next to the locked/selected box (top-right, flips left if it overflows).
      // drawVehicleCard manages its own shadow state via save/restore.
      if (vehicleId) {
        const box =
          tracks.find((t) => t.id === selectedTrack) ??
          (lockActive ? tracks[0] : undefined);
        if (box) {
          drawVehicleCard(
            ctx,
            vehicleId.info,
            (box.x + box.w) * width, // box right edge (px)
            box.y * height, // box top edge (px)
            width,
            height,
          );
        }
      }

      // AR ally marker(s): Outrider projected into THIS feed. Only drawn when
      // the main panel is the Overwatch camera (STREAM === "drone") — the
      // projection is from Overwatch's pose. Drop stale markers (TTL) so a marker
      // fades when the backend stops publishing (lost fix / feed offline).
      let drewAlly = false;
      if (STREAM === "drone") {
        const ms = Object.values(allyRef.current);
        if (ms.length) {
          let stale = false;
          for (const m of ms) {
            // `seen` is performance.now()-based; compare against the same clock.
            if (now - m.seen < ALLY_MARKER_TTL_MS) {
              drawAllyMarker(ctx, m, width, height, pulse);
              drewAlly = true;
            } else {
              stale = true;
            }
          }
          // No fresh batch within the TTL → evict so the marker fades for good.
          if (stale) pruneAllyMarkers();
        }
      }

      // Rubber-band selection rectangle (click-to-track), drawn over everything
      // while the operator is dragging a box. Dashed cyan with corner ticks so
      // it reads as a "selection", distinct from the solid lock reticle.
      const d = dragRef.current;
      if (d) {
        const rx = Math.min(d.x0, d.x1);
        const ry = Math.min(d.y0, d.y1);
        const rw = Math.abs(d.x1 - d.x0);
        const rh = Math.abs(d.y1 - d.y0);
        ctx.save();
        ctx.strokeStyle = ACCENT;
        ctx.lineWidth = 1.5;
        ctx.shadowColor = ACCENT;
        ctx.shadowBlur = 8;
        ctx.setLineDash([6, 4]);
        ctx.strokeRect(rx, ry, rw, rh);
        ctx.setLineDash([]);
        ctx.fillStyle = "rgba(34,227,196,0.10)";
        ctx.fillRect(rx, ry, rw, rh);
        ctx.restore();
      }

      // Keep animating while there's a locked target needing motion, while the
      // operator is actively drawing a selection box, OR while an ally marker is
      // on screen (its diamond/chevron breathes with the pulse).
      const animate =
        dragRef.current !== null || drewAlly || tracks.some((t) => t.id === selectedTrack);
      if (animate) raf = requestAnimationFrame(draw);
    };

    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
    // `dragging` restarts the loop on a rubber-band drag; `allyMarkers` restarts
    // it when an ally marker appears/updates (otherwise a single static frame
    // would leave it unanimated). The loop self-sustains off the refs.
  }, [tracks, selectedTrack, lockActive, vehicleId, dragging, allyMarkers, pruneAllyMarkers]);

  return (
    <>
      <video ref={videoRef} autoPlay playsInline muted className="h-full w-full object-contain" />
      <canvas
        ref={canvas}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        className={`absolute inset-0 h-full w-full ${
          selectMode ? "cursor-crosshair touch-none" : "pointer-events-none"
        }`}
      />
      {/* TRACK toggle — exposed directly on the LIVE feed (Outrider-style): no
          need to switch to TRACK mode + START first. Tapping TRACK arms the
          drag-to-draw-box interaction; releasing the box auto-starts the vision
          pipeline (if needed) and seeds the CSRT tracker on the drawn ROI. The
          gate is just `state === "live"` so it's available the moment the feed
          is up. */}
      {state === "live" && (
        <button
          onClick={() => setSelectMode((v) => !v)}
          disabled={seeding}
          className={`absolute top-2 left-2 z-10 flex items-center gap-1 rounded-md px-2 py-1 text-[11px] font-semibold shadow ${
            selectMode ? "bg-accent text-ink glow-accent" : "bg-ink/70 text-accent border border-accent/40"
          }`}
          title={
            selectMode
              ? "Drag a box on the video to lock a target"
              : "Click-to-track: draw a box on the live feed to lock (auto-starts vision)"
          }
        >
          <Crosshair size={13} /> {seeding ? "LOCKING…" : selectMode ? "DRAW BOX" : "TRACK"}
        </button>
      )}
      {/* STOP TRACK — explicit, clearly-labeled control to stop tracking. Visible
          whenever the vision pipeline is running (including while SELECT/DRAW BOX
          is armed), so the operator can always bail out + drop the lock without
          leaving the live feed. Stops the pipeline (`api.vision.stop()`), clears
          the running flag, and exits select mode. */}
      {state === "live" && visionRunning && (
        <button
          onClick={async (e) => {
            e.stopPropagation();
            setSelectMode(false); // also exit draw-box mode if armed
            try {
              await api.vision.stop();
            } catch (err) {
              pushLog("error", `vision stop: ${(err as Error).message}`, 3);
            } finally {
              setVisionRunning(false); // drops the lock (clears tracks/objects)
            }
          }}
          disabled={seeding}
          className="absolute top-2 left-[7rem] z-10 flex items-center gap-1 rounded-md border border-danger/50 bg-danger/20 px-2 py-1 text-[11px] font-semibold text-danger shadow"
          title="Stop tracking — stop the vision pipeline and drop the lock"
        >
          <Square size={12} /> STOP TRACK
        </button>
      )}
      {selectMode && (
        <div className="pointer-events-none absolute bottom-2 left-1/2 -translate-x-1/2 z-10 rounded bg-ink/70 px-2 py-0.5 text-[10px] text-accent">
          drag a box around the target to lock
        </div>
      )}
      {state !== "live" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 text-slate-500">
          <VideoOff size={28} />
          <span className="text-xs">
            {state === "error" ? "no stream — start go2rtc + set RTSP_URL" : "connecting…"}
          </span>
        </div>
      )}
    </>
  );
}

/** TRACK — backend annotated MJPEG (YOLO + VLM-lock burned in). */
function TrackView() {
  const visionRunning = useGcs((s) => s.visionRunning);
  const setVisionRunning = useGcs((s) => s.setVisionRunning);
  const pushLog = useGcs((s) => s.pushLog);
  const [starting, setStarting] = useState(false);

  if (!visionRunning) {
    return (
      <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-slate-400">
        <ScanSearch size={30} className="text-accent" />
        <button
          disabled={starting}
          onClick={async () => {
            setStarting(true);
            try {
              // Always run vision on the Overwatch feed's H.264 RTSP restream.
              await api.vision.start(OVERWATCH_RTSP);
              setVisionRunning(true);
            } catch (e) {
              pushLog("error", `vision start: ${(e as Error).message}`, 3);
            } finally {
              setStarting(false);
            }
          }}
          className="flex items-center gap-2 rounded-lg bg-accent/20 text-accent px-4 py-2 text-sm font-semibold glow-accent"
        >
          <Play size={16} /> {starting ? "starting…" : "START TRACKING"}
        </button>
        <span className="text-[11px]">runs YOLO+ByteTrack on the Overwatch feed</span>
      </div>
    );
  }
  return (
    <img
      src={MJPEG_URL}
      alt="tracking"
      className="h-full w-full object-contain"
      onError={() => setVisionRunning(false)}
    />
  );
}

function TrackControls() {
  const { lockActive, visionRunning, setVisionRunning } = useGcs();
  const pushLog = useGcs((s) => s.pushLog);
  const [desc, setDesc] = useState("the car");
  const [following, setFollowing] = useState(false);

  const wrap = (label: string, fn: () => Promise<unknown>) => async () => {
    try {
      const r = (await fn()) as { ok?: boolean; reason?: string };
      if (r && r.ok === false) pushLog("track", r.reason ?? `${label} failed`);
    } catch (e) {
      pushLog("error", `${label}: ${(e as Error).message}`, 3);
    }
  };

  // Quick-pick: set the description and lock onto that target in one tap.
  const quickPick = (label: string, target: string) =>
    wrap(label, () => {
      setDesc(target);
      return api.vision.acquire(target);
    });

  if (!visionRunning) return null;
  return (
    <div className="flex items-center gap-1.5 px-2 py-1.5 border-t border-edge/60">
      <span
        className="flex items-center gap-1 rounded-md bg-edge/50 px-1.5 py-1 text-[10px] font-semibold uppercase tracking-wider text-slate-300"
        title="Feed being tracked"
      >
        <Video size={11} className="text-accent" /> Overwatch
      </span>
      <button
        onClick={quickPick("person", "the person")}
        className="flex items-center gap-1 rounded-md bg-accent/20 text-accent px-2 py-1 text-[11px] font-semibold"
        title="Lock onto a person"
      >
        <PersonStanding size={13} /> Person
      </button>
      <button
        onClick={quickPick("vehicle", "the car")}
        className="flex items-center gap-1 rounded-md bg-accent/20 text-accent px-2 py-1 text-[11px] font-semibold"
        title="Lock onto a vehicle"
      >
        <Car size={13} /> Vehicle
      </button>
      <input
        value={desc}
        onChange={(e) => setDesc(e.target.value)}
        placeholder="describe target…"
        className="flex-1 min-w-0 bg-ink/70 border border-edge/60 rounded px-2 py-1 text-xs text-slate-200"
      />
      <button
        onClick={wrap("acquire", () => api.vision.acquire(desc))}
        className="flex items-center gap-1 rounded-md bg-accent/20 text-accent px-2 py-1 text-[11px] font-semibold"
        title="Acquire with Qwen vision, then track"
      >
        <Crosshair size={13} /> LOCK
      </button>
      <button
        onClick={wrap("follow", async () => {
          const next = !following;
          setFollowing(next);
          return api.vision.follow(next);
        })}
        className={`rounded-md px-2 py-1 text-[11px] font-semibold ${
          following ? "bg-ok/25 text-ok" : "bg-edge/50 text-slate-200"
        }`}
      >
        {following ? "FOLLOWING" : "FOLLOW"}
      </button>
      <button
        onClick={wrap("orbit", api.vision.orbitTarget)}
        className="flex items-center gap-1 rounded-md bg-edge/50 text-slate-100 px-2 py-1 text-[11px] font-semibold"
      >
        <Orbit size={13} /> ORBIT
      </button>
      <button
        onClick={async () => {
          await api.vision.stop();
          setVisionRunning(false);
          setFollowing(false);
        }}
        className="rounded-md bg-danger/20 text-danger px-2 py-1"
        title="Stop vision"
      >
        <Square size={13} />
      </button>
      <span className={`ml-1 h-2 w-2 rounded-full ${lockActive ? "bg-accent pulse" : "bg-edge"}`} title="lock" />
    </div>
  );
}

export default function VideoPanel() {
  const uiMode = useGcs((s) => s.uiMode);
  const trackMode = uiMode === "track";
  const showMainFeed = useGcs((s) => s.showMainFeed);
  const setShowMainFeed = useGcs((s) => s.setShowMainFeed);
  const boxSelecting = useGcs((s) => s.boxSelecting);
  const visionRunning = useGcs((s) => s.visionRunning);

  // Whether the actual go2rtc/WebRTC media is delivering frames. Reported up
  // from FpvView. In TRACK mode the picture is the backend MJPEG <img>, which is
  // "live" whenever the vision pipeline is running. Used ONLY to choose the
  // overlay style when telemetry drops — a stale link must never black out a
  // still-playing frame.
  const [fpvLive, setFpvLive] = useState(false);
  const mediaLive = trackMode ? visionRunning : fpvLive;

  // DISPLAY GATING IS PER-CONNECTED DRONE (not per-active). C1 FIX: we mount this
  // panel (and its WebRTC) the moment Overwatch first connects and KEEP it
  // mounted across transient roster flips — a momentary `connected:false` (poll
  // race / backend blip / link flap) must NOT unmount the panel and tear down the
  // `RTCPeerConnection`. `offline` is DEBOUNCED (true only after a sustained
  // disconnect) and gates an overlay + a `hidden` visibility class, never an
  // unmount. Before Overwatch has EVER connected we render nothing (no panel, no
  // peer connection, no edge-tab).
  const { mounted, offline } = useConnectedGate(VEHICLE_ID);
  if (!mounted) return null;

  const header = (
    <div className="flex w-full items-center gap-2 text-xs">
      {trackMode ? <Crosshair size={14} className="text-accent" /> : <Radio size={14} className={offline ? (mediaLive ? "text-warn" : "text-slate-500") : "text-ok"} />}
      <span className="font-semibold tracking-wide">
        {trackMode ? "TRACK · vision" : `FPV · ${STREAM.toUpperCase()}`}
      </span>
      {/* Telemetry status pill. While the PICTURE is still live we say "telemetry"
          in amber (the feed is fine, only the data link wedged); only when there's
          no live frame is it a red "offline". Never a red "offline" over a good feed. */}
      {offline && (
        mediaLive ? (
          <span className="rounded bg-warn/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-warn">
            telemetry
          </span>
        ) : (
          <span className="rounded bg-danger/20 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-danger">
            offline
          </span>
        )
      )}
      {/* REC → record the Overwatch ("drone") go2rtc stream to a local MP4 */}
      <RecordButton stream={STREAM} />
      <button
        onClick={(e) => {
          e.stopPropagation();
          setShowMainFeed(false);
        }}
        className="ml-auto rounded p-0.5 text-slate-400 hover:text-slate-100"
        title="Hide feed"
      >
        <EyeOff size={13} />
      </button>
    </div>
  );

  // STAYS MOUNTED when hidden (CSS only) so the WebRTC keeps playing — instant
  // restore, never kills the feed. Collapsed → a vertical tab on the right edge
  // (upper third, clear of the Outrider tab); click to bring it back.
  return (
    <>
      {!showMainFeed && (
        <button
          onClick={() => setShowMainFeed(true)}
          title="Show Overwatch feed"
          className="fixed right-0 top-1/3 z-30 -translate-y-1/2 flex items-center gap-2 rounded-l-lg border border-r-0 border-edge/60 bg-ink/85 px-1.5 py-3 text-[11px] font-semibold tracking-widest text-slate-200 backdrop-blur hover:text-accent hover:border-accent/60 [writing-mode:vertical-rl]"
        >
          <Radio size={13} className="text-ok" />
          {trackMode ? "TRACK" : "OVERWATCH"}
        </button>
      )}
      <div className={showMainFeed ? undefined : "hidden"}>
        <VideoFrame
          feed="main"
          defaultPos="top-16 right-3"
          defaultWidth={34}
          thumbCorner="bottom-right"
          header={header}
          video={() => (
            <>
              {trackMode ? <TrackView /> : <FpvView onStateChange={setFpvLive} />}
              {/* C1: sustained telemetry-disconnect overlay. The feed + WebRTC
                  stay MOUNTED underneath (instant recovery; never torn down on a
                  transient flip). The VIDEO streams independently of telemetry,
                  so while frames are still arriving (`mediaLive`) we keep the
                  picture fully visible and show only a NON-blocking "telemetry
                  stale" badge — we DON'T black out a live frame. Only when
                  there's no live frame do we dim + label it "reconnecting…". */}
              {offline && <StaleTelemetryOverlay label="Overwatch" mediaLive={mediaLive} />}
            </>
          )}
          controls={trackMode ? <TrackControls /> : undefined}
          dragDisabled={boxSelecting}
        />
      </div>
    </>
  );
}
