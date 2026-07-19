import { VideoOff, WifiOff } from "lucide-react";

/**
 * Telemetry-disconnect overlay for a video feed (C1 follow-up).
 *
 * THE BUG THIS FIXES: the feeds used to render a heavy, full-screen
 * `bg-ink/70` "offline — reconnecting…" curtain the moment `offline`
 * (a DEBOUNCED, sustained telemetry-disconnect) went true — even though the
 * video itself streams INDEPENDENTLY over go2rtc/WebRTC and is STILL delivering
 * frames. So a uXRCE-DDS wedge or a link blip blacked out the operator's live
 * picture mid-flight, which is dangerous.
 *
 * THE FIX: VISIBILITY of the picture is decoupled from telemetry. While the
 * media stream is LIVE (`mediaLive`), we keep the frame fully visible and only
 * overlay a compact, NON-blocking amber badge ("TELEMETRY STALE") at the top.
 * We only fall back to the dimming "reconnecting…" curtain when there's truly
 * nothing to show (media NOT live AND telemetry offline). Either way the panel
 * and its `RTCPeerConnection` stay MOUNTED (the caller never unmounts on a
 * transient flip), so the feed never blips and reconnect is instant.
 *
 *  mediaLive=true  → amber "TELEMETRY STALE" pill, picture stays clear.
 *  mediaLive=false → dim "<label> offline — reconnecting…" curtain (no frame).
 */
export default function StaleTelemetryOverlay({
  label,
  mediaLive,
}: {
  /** Vehicle display name, e.g. "Overwatch" / "Outrider". */
  label: string;
  /** True when the WebRTC/go2rtc media is actually delivering frames. */
  mediaLive: boolean;
}) {
  if (mediaLive) {
    // Picture is alive — DON'T cover it. Just a compact warning pill.
    return (
      <div className="pointer-events-none absolute inset-x-0 top-0 z-20 flex justify-center p-2">
        <div className="stale-badge flex items-center gap-1.5 rounded-md border border-warn/60 bg-ink/85 px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wider text-warn shadow">
          <WifiOff size={13} />
          Telemetry stale
        </div>
      </div>
    );
  }
  // No live frame to protect — show the truthful dimming curtain.
  return (
    <div className="pointer-events-none absolute inset-0 z-20 flex flex-col items-center justify-center gap-1 bg-ink/70 text-slate-400">
      <VideoOff size={26} />
      <span className="text-xs">{label} offline — reconnecting…</span>
    </div>
  );
}
