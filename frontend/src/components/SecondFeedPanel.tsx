import { useRef, useState } from "react";
import { Crosshair, EyeOff, Loader2, Navigation, Video, VideoOff, Wifi } from "lucide-react";
import { useGcs } from "../store/useGcs";
import { useGo2RtcWebRTC } from "../lib/useGo2Rtc";
import { useConnectedGate } from "../lib/useConnectedGate";
import { api } from "../lib/api";
import VideoFrame from "./VideoFrame";
import RecordButton from "./RecordButton";
import StaleTelemetryOverlay from "./StaleTelemetryOverlay";

// Outrider's onboard video. The Jetson's VIO pipeline (oakd_tracker
// rgb_udp_streamer) streams the OAK-D RGB as H.264/MPEG-TS over UDP to the GCS
// host :5600; go2rtc ingests it as the server-side "outrider" stream.
const OUTRIDER_STREAM = "outrider";
const clamp01 = (n: number) => Math.max(0, Math.min(1, n));

// This panel shows OUTRIDER's feed — render it whenever Outrider is CONNECTED
// (display-only; not gated on being the command-active drone). So when both
// drones are connected, both feeds show; an offline Outrider shows no feed UI.
const VEHICLE_ID = "outrider";

/**
 * OUTRIDER's video feed (second panel). Plays the VIO RGB stream, AND supports
 * CLICK-TO-TRACK against Outrider's ONBOARD tracker: toggle SELECT, drag a box
 * over the target, release → the box (normalized to the real video frame, not
 * the letterboxed panel) is sent to the Jetson's onboard tracker (UDP :8771),
 * which locks and burns the reticle into this very stream. CLEAR releases it.
 */
/**
 * Gate: render OUTRIDER's feed once Outrider has CONNECTED (display-only, not
 * gated on being the command-active drone) — so when both drones are live, both
 * feeds show. C1 FIX: we MOUNT the inner panel (and its WebRTC) on the first-ever
 * connect and KEEP it mounted across transient roster flips; a momentary
 * `connected:false` (poll race / link flap) must NOT unmount the panel and tear
 * down the `RTCPeerConnection`. `offline` is DEBOUNCED (true only after a
 * sustained disconnect) and drives an overlay, not an unmount. Before Outrider
 * has EVER connected we return null (no panel, no peer connection, no edge-tab).
 * Gating in a wrapper keeps the inner panel's hooks unconditional (no
 * Rules-of-Hooks violation from an early return).
 */
export default function SecondFeedPanel() {
  const { mounted, offline } = useConnectedGate(VEHICLE_ID);
  if (!mounted) return null;
  return <OutriderFeedPanel offline={offline} />;
}

function OutriderFeedPanel({ offline }: { offline: boolean }) {
  const showSecondFeed = useGcs((s) => s.showSecondFeed);
  const setShowSecondFeed = useGcs((s) => s.setShowSecondFeed);
  const pushLog = useGcs((s) => s.pushLog);

  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dragRef = useRef<{ x0: number; y0: number; x1: number; y1: number } | null>(null);

  const [selecting, setSelecting] = useState(false);
  const [tracking, setTracking] = useState(false);
  const [seeding, setSeeding] = useState(false);
  const [following, setFollowing] = useState(false);

  const state = useGo2RtcWebRTC(videoRef, OUTRIDER_STREAM, true);
  const live = state === "live";

  // The object-contain content rect of the video within the canvas box (the
  // video is 4:3, the panel area 16:9 → horizontal letterbox). A drawn box must
  // be normalized to THIS rect so it maps to the tracker's actual frame coords.
  const contentRect = () => {
    const cv = canvasRef.current!;
    const v = videoRef.current!;
    const cw = cv.clientWidth, ch = cv.clientHeight;
    const vw = v.videoWidth || 4, vh = v.videoHeight || 3;
    const ar = vw / vh, car = cw / ch;
    let w = cw, h = ch, x = 0, y = 0;
    if (ar > car) { h = cw / ar; y = (ch - h) / 2; } // wider than panel → vertical bars
    else { w = ch * ar; x = (cw - w) / 2; }           // taller/narrower → horizontal bars
    return { x, y, w, h };
  };

  const localPt = (e: React.PointerEvent) => {
    const r = canvasRef.current!.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  const redraw = () => {
    const cv = canvasRef.current;
    if (!cv) return;
    if (cv.width !== cv.clientWidth || cv.height !== cv.clientHeight) {
      cv.width = cv.clientWidth;
      cv.height = cv.clientHeight;
    }
    const ctx = cv.getContext("2d")!;
    ctx.clearRect(0, 0, cv.width, cv.height);
    const d = dragRef.current;
    if (d) {
      ctx.save();
      ctx.strokeStyle = "#22e3c4";
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 4]);
      ctx.shadowColor = "#22e3c4";
      ctx.shadowBlur = 8;
      ctx.strokeRect(Math.min(d.x0, d.x1), Math.min(d.y0, d.y1), Math.abs(d.x1 - d.x0), Math.abs(d.y1 - d.y0));
      ctx.restore();
    }
  };

  const onDown = (e: React.PointerEvent) => {
    if (!selecting) return;
    const p = localPt(e);
    dragRef.current = { x0: p.x, y0: p.y, x1: p.x, y1: p.y };
    canvasRef.current?.setPointerCapture(e.pointerId);
  };
  const onMove = (e: React.PointerEvent) => {
    if (!selecting || !dragRef.current) return;
    const p = localPt(e);
    dragRef.current.x1 = p.x;
    dragRef.current.y1 = p.y;
    redraw();
  };
  const onUp = async (e: React.PointerEvent) => {
    if (!selecting || !dragRef.current) return;
    const d = dragRef.current;
    dragRef.current = null;
    redraw();
    canvasRef.current?.releasePointerCapture(e.pointerId);
    const rect = contentRect();
    const x0 = Math.min(d.x0, d.x1), y0 = Math.min(d.y0, d.y1);
    const bw = Math.abs(d.x1 - d.x0), bh = Math.abs(d.y1 - d.y0);
    // normalize to the video content rect (0..1 of the real frame)
    const box = {
      x: clamp01((x0 - rect.x) / rect.w),
      y: clamp01((y0 - rect.y) / rect.h),
      w: clamp01(bw / rect.w),
      h: clamp01(bh / rect.h),
    };
    setSelecting(false);
    if (box.w < 0.02 || box.h < 0.02) return; // ignore tiny/accidental boxes
    setSeeding(true);
    try {
      const r = await api.outrider.track(box);
      if (r?.ok === false) pushLog("track", r.reason ?? "onboard seed failed");
      else setTracking(true);
    } catch (err) {
      pushLog("error", `outrider track: ${(err as Error).message}`, 3);
    } finally {
      setSeeding(false);
    }
  };

  const clearTrack = async () => {
    setTracking(false);
    setFollowing(false);
    try {
      await api.outrider.clearTrack();
    } catch {
      /* best-effort */
    }
  };

  // Toggle the ONBOARD follow controller (UDP :8771 FOLLOW 1/0) — flies Outrider
  // toward the locked target. Only meaningful while tracking.
  const toggleFollow = async () => {
    const next = !following;
    setFollowing(next);
    try {
      const r = await api.outrider.follow(next);
      if (r?.ok === false) {
        setFollowing(!next);
        pushLog("follow", r.reason ?? "onboard follow failed");
      }
    } catch (err) {
      setFollowing(!next);
      pushLog("error", `outrider follow: ${(err as Error).message}`, 3);
    }
  };

  const header = (
    <div className="flex items-center gap-2 text-xs">
      <Wifi size={14} className={live ? "text-ok" : "text-slate-500"} />
      <span className="font-semibold tracking-wide">OUTRIDER</span>
      <span
        className={`text-[10px] uppercase tracking-wider ${
          state === "live" ? "text-ok" : state === "error" ? "text-danger" : "text-slate-400"
        }`}
      >
        {state === "live" ? "live" : state === "error" ? "offline" : "connecting…"}
      </span>
      {/* SELECT → drag a box to lock the ONBOARD tracker */}
      {live && (
        <button
          onClick={() => setSelecting((s) => !s)}
          className={`flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold ${
            selecting ? "bg-accent/30 text-accent" : "text-slate-300 hover:text-accent"
          }`}
          title="Drag a box on the target to lock the onboard tracker"
        >
          {seeding ? <Loader2 size={11} className="animate-spin" /> : <Crosshair size={11} />}
          {selecting ? "DRAW BOX" : "TRACK"}
        </button>
      )}
      {tracking && (
        <button
          onClick={toggleFollow}
          className={`flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold ${
            following ? "bg-accent/30 text-accent" : "text-slate-300 hover:text-accent"
          }`}
          title="Toggle onboard follow (fly Outrider toward the locked target)"
        >
          <Navigation size={11} />
          FOLLOW
        </button>
      )}
      {tracking && (
        <button
          onClick={clearTrack}
          className="rounded px-1.5 py-0.5 text-[10px] font-semibold text-danger hover:bg-danger/20"
          title="Clear onboard track"
        >
          CLEAR
        </button>
      )}
      {/* REC → record this feed to a local MP4 on the Mac */}
      <RecordButton stream={OUTRIDER_STREAM} />
      <button
        onClick={() => setShowSecondFeed(false)}
        className="ml-auto rounded p-0.5 text-slate-400 hover:text-slate-100"
        title="Hide Outrider feed"
      >
        <EyeOff size={13} />
      </button>
    </div>
  );

  const video = () => (
    <>
      <video ref={videoRef} autoPlay playsInline muted className="h-full w-full object-contain" />
      {/* draw-box overlay — only captures pointers while selecting, so the
          panel's drag + double-click-focus still work otherwise */}
      <canvas
        ref={canvasRef}
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        className={`absolute inset-0 h-full w-full ${selecting ? "cursor-crosshair" : "pointer-events-none"}`}
      />
      {selecting && (
        <div className="absolute left-1/2 top-2 -translate-x-1/2 rounded bg-ink/80 px-2 py-0.5 text-[10px] text-accent">
          drag a box around the target to lock onboard
        </div>
      )}
      {state !== "live" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 px-4 text-center text-slate-500">
          {state === "error" ? <VideoOff size={26} /> : <Loader2 size={26} className="animate-spin" />}
          <span className="text-xs">
            {state === "error"
              ? "Outrider feed offline — start the Jetson VIO stream"
              : "waiting for Outrider video…"}
          </span>
        </div>
      )}
      {/* C1: sustained telemetry-disconnect overlay. The feed + WebRTC stay
          MOUNTED underneath (instant recovery; a transient flip shows nothing).
          The VIDEO streams independently of telemetry, so while frames are still
          arriving (`live`) we keep the picture fully visible and overlay only a
          NON-blocking "telemetry stale" badge — we DON'T black out a live feed
          mid-flight. Only when there's no live frame do we dim + label it
          "reconnecting…". Sits above the stream-state overlay. */}
      {offline && <StaleTelemetryOverlay label="Outrider" mediaLive={live} />}
    </>
  );

  // STAYS MOUNTED when hidden (CSS only) so the WebRTC keeps playing — restore is
  // instant and never kills the feed. Collapsed → a neat vertical tab flush
  // against the right screen edge; click it to bring the feed back.
  return (
    <>
      {!showSecondFeed && (
        <button
          onClick={() => setShowSecondFeed(true)}
          title="Show Outrider feed"
          className="fixed right-0 top-1/2 z-30 -translate-y-1/2 flex items-center gap-2 rounded-l-lg border border-r-0 border-edge/60 bg-ink/85 px-1.5 py-3 text-[11px] font-semibold tracking-widest text-slate-200 backdrop-blur hover:text-accent hover:border-accent/60 [writing-mode:vertical-rl]"
        >
          <Video size={13} className={live ? "text-ok" : "text-slate-500"} />
          OUTRIDER
        </button>
      )}
      <div className={showSecondFeed ? undefined : "hidden"}>
        <VideoFrame
          feed="feed2"
          defaultPos="bottom-3 right-[26rem]"
          defaultWidth={28}
          thumbCorner="bottom-left"
          header={header}
          video={video}
          dragDisabled={selecting}
          videoAspect="aspect-[4/3]"
        />
      </div>
    </>
  );
}
