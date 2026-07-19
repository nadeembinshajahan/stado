import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { Loader2, Mic, MicOff } from "lucide-react";
import { useGcs } from "../../store/useGcs";
import { startVoice, type VoiceSession } from "../../lib/voice";

type State = "idle" | "connecting" | "live" | "error";
type MicMode = "ptt" | "open";

/**
 * Mobile voice button — large floating FAB (88x88), bottom-right, above the
 * home indicator. Re-implements the desktop VoiceButton's PTT / open-mic logic
 * (sharing `lib/voice` directly so the session pipe + tool wiring are
 * identical). Hold to talk in PTT mode; tap to toggle in open mode. A small
 * pill above the FAB switches modes without dropping the session.
 *
 * Why duplicate vs reuse the desktop component: the desktop button is a tight
 * inline control with a mode toggle that ENLARGES the click target (bad on
 * mobile). The mobile FAB is a different shape, owns the safe-area anchoring,
 * and skips the spacebar listener (no keyboard on a phone). The realtime voice
 * session lib is identical.
 */
export default function MobilePttFab() {
  const [state, setState] = useState<State>("idle");
  const [micMode, setMicMode] = useState<MicMode>("ptt");
  const [active, setActive] = useState(false);
  const session = useRef<VoiceSession | null>(null);
  const activeRef = useRef(false);
  const modeRef = useRef<MicMode>("ptt");
  const pendingTools = useRef<Map<string, number>>(new Map());

  const pushLog = useGcs((s) => s.pushLog);
  const convHeard = useGcs((s) => s.convHeard);
  const convSaid = useGcs((s) => s.convSaid);
  const convTool = useGcs((s) => s.convTool);
  const convToolResult = useGcs((s) => s.convToolResult);

  modeRef.current = micMode;

  const ensureSession = async () => {
    if (session.current) return;
    setState("connecting");
    session.current = await startVoice(
      {
        onState: (s) => {
          setState(s === "closed" ? "idle" : s);
          if (s === "closed") {
            session.current = null;
            activeRef.current = false;
            setActive(false);
          }
          if (s === "live" && activeRef.current) session.current?.setTalking(true);
        },
        onHeard: (t) => convHeard(t),
        onSaid: (t) => convSaid(t),
        onTool: (name, args) => {
          const a = Object.entries(args)
            .map(([k, v]) => `${k}=${v}`)
            .join(",");
          pushLog("voice", `▶ ${name}(${a})`);
          pendingTools.current.set(name, convTool(name, args));
        },
        onToolResult: (name, result) => {
          const id = pendingTools.current.get(name);
          if (id != null) {
            convToolResult(id, result.ok !== false);
            pendingTools.current.delete(name);
          }
        },
        onError: (m) => pushLog("voice", `voice: ${m}`, 3),
      },
      { manualVad: modeRef.current === "ptt" },
    );
  };

  const beginTalk = async () => {
    if (activeRef.current) return;
    activeRef.current = true;
    setActive(true);
    try {
      await ensureSession();
    } catch (err) {
      pushLog("voice", `voice: ${(err as Error).message}`, 3);
      setState("error");
      activeRef.current = false;
      setActive(false);
      return;
    }
    if (activeRef.current) session.current?.setTalking(true);
  };

  const endTalk = () => {
    if (!activeRef.current) return;
    activeRef.current = false;
    setActive(false);
    session.current?.setTalking(false);
  };

  const toggleOpen = async () => {
    if (activeRef.current) {
      endTalk();
      return;
    }
    activeRef.current = true;
    setActive(true);
    try {
      await ensureSession();
    } catch (err) {
      pushLog("voice", `voice: ${(err as Error).message}`, 3);
      setState("error");
      activeRef.current = false;
      setActive(false);
      return;
    }
    if (activeRef.current) session.current?.setTalking(true);
  };

  // Switching modes drops the live session so the next press reconnects with
  // the right VAD config (manual vs auto). Same behavior as desktop.
  const switchMode = (m: MicMode) => {
    if (m === micMode) return;
    endTalk();
    setMicMode(m);
    session.current?.stop();
    session.current = null;
    setState("idle");
  };

  // Tear the session down on unmount (e.g. operator rotates to desktop layout).
  useEffect(() => () => session.current?.stop(), []);

  const live = state === "live";
  const ptt = micMode === "ptt";

  const handlers = ptt
    ? {
        onPointerDown: (e: React.PointerEvent) => {
          e.preventDefault();
          void beginTalk();
        },
        onPointerUp: endTalk,
        onPointerLeave: endTalk,
        onPointerCancel: endTalk,
      }
    : { onClick: toggleOpen };

  const label = active
    ? ptt
      ? "TALKING"
      : "LISTENING"
    : state === "connecting"
      ? "…"
      : ptt
        ? "HOLD"
        : "TAP";

  return (
    <div
      className="pointer-events-none fixed right-3 z-[55] flex flex-col items-end gap-1.5 select-none"
      style={{ bottom: "calc(env(safe-area-inset-bottom, 0px) + 168px)" }}
    >
      {/* PTT / Open-mic toggle pill — small but reachable */}
      <div className="pointer-events-auto flex rounded-md bg-ink/80 p-0.5 text-[9px] font-bold shadow-md backdrop-blur">
        {(["ptt", "open"] as MicMode[]).map((m) => (
          <button
            key={m}
            onClick={() => switchMode(m)}
            className={`tap rounded px-2 py-0.5 transition-colors ${
              micMode === m ? "bg-accent/30 text-accent" : "text-slate-400"
            }`}
          >
            {m === "ptt" ? "PTT" : "OPEN"}
          </button>
        ))}
      </div>

      <motion.button
        whileTap={{ scale: 0.92 }}
        {...handlers}
        className={`tap pointer-events-auto relative flex h-[88px] w-[88px] flex-col items-center justify-center gap-0.5 rounded-full text-center touch-none transition-colors ${
          active
            ? "bg-accent/30 text-accent glow-accent pulse"
            : live
              ? "bg-accent/15 text-accent"
              : "bg-ink/85 text-slate-100 ring-1 ring-edge/60"
        }`}
        style={{
          boxShadow: active
            ? "0 0 26px rgba(34,227,196,0.55), 0 8px 24px rgba(0,0,0,0.45)"
            : "0 8px 24px rgba(0,0,0,0.45)",
        }}
        aria-label="Push to talk"
      >
        {state === "connecting" ? (
          <Loader2 size={26} className="animate-spin" />
        ) : active ? (
          <Mic size={26} />
        ) : (
          <MicOff size={26} />
        )}
        <span className="text-[10px] font-bold tracking-wider">{label}</span>
        {live && !active && (
          <span
            className="absolute right-1.5 top-1.5 h-2 w-2 rounded-full bg-accent"
            title="connected"
          />
        )}
      </motion.button>
    </div>
  );
}
