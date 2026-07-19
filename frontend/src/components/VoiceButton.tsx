import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { Loader2, Mic, MicOff } from "lucide-react";
import { useGcs } from "../store/useGcs";
import { startVoice, type VoiceSession } from "../lib/voice";

type State = "idle" | "connecting" | "live" | "error";
type MicMode = "ptt" | "open";

/** Voice control with two mic modes:
 *  - PTT (default): hold the button OR the spacebar to speak; mic only streams
 *    while held, so no ambient pickup. Release to stop.
 *  - Open mic: tap to toggle continuous listening (the server-side VAD segments
 *    turns). Good for hands-free, noisier-tolerant operation.
 *  The realtime voice session stays open across presses so context is preserved. */
export default function VoiceButton() {
  const [state, setState] = useState<State>("idle");
  const [micMode, setMicMode] = useState<MicMode>("ptt");
  const [active, setActive] = useState(false); // held (PTT) or listening (open)
  const session = useRef<VoiceSession | null>(null);
  const activeRef = useRef(false);
  const modeRef = useRef<MicMode>("ptt");
  // most-recent conversation entry id per tool name, so tool_result can resolve it
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
    session.current = await startVoice({
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
        const a = Object.entries(args).map(([k, v]) => `${k}=${v}`).join(",");
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
    }, { manualVad: modeRef.current === "ptt" });
  };

  // PTT: start streaming the mic.
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

  // Open mic: tap to toggle continuous listening.
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

  // Spacebar = PTT (only in PTT mode, and not while typing in a field).
  useEffect(() => {
    const typing = (t: EventTarget | null) => {
      const el = t as HTMLElement | null;
      if (!el) return false;
      const tag = el.tagName;
      return tag === "INPUT" || tag === "TEXTAREA" || el.isContentEditable;
    };
    const down = (e: KeyboardEvent) => {
      if (e.code !== "Space" || e.repeat || modeRef.current !== "ptt") return;
      if (typing(e.target)) return;
      e.preventDefault();
      void beginTalk();
    };
    const up = (e: KeyboardEvent) => {
      if (e.code !== "Space" || modeRef.current !== "ptt") return;
      if (typing(e.target)) return;
      e.preventDefault();
      endTalk();
    };
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
    };
  }, []); // refs keep handlers current

  const switchMode = (m: MicMode) => {
    if (m === micMode) return;
    endTalk();
    setMicMode(m);
    // VAD mode is fixed at connect time — drop the session so the next press
    // reconnects with the matching manual/auto config.
    session.current?.stop();
    session.current = null;
    setState("idle");
  };

  const live = state === "live";
  const ptt = micMode === "ptt";
  const label = active
    ? ptt ? "TALKING" : "LISTENING"
    : state === "connecting" ? "…"
    : ptt ? (live ? "HOLD/SPACE" : "PTT")
    : (live ? "TAP" : "OPEN MIC");

  // PTT button uses pointer hold; open-mic uses click toggle.
  const pttHandlers = ptt
    ? {
        onPointerDown: (e: React.PointerEvent) => { e.preventDefault(); void beginTalk(); },
        onPointerUp: endTalk,
        onPointerLeave: endTalk,
        onPointerCancel: endTalk,
      }
    : { onClick: toggleOpen };

  return (
    <div className="relative flex flex-col items-center gap-1 select-none">
      {/* PTT / Open-mic mode toggle */}
      <div className="flex rounded-md bg-edge/40 p-0.5 text-[9px] font-semibold">
        {(["ptt", "open"] as MicMode[]).map((m) => (
          <button
            key={m}
            onClick={() => switchMode(m)}
            className={`rounded px-1.5 py-0.5 transition-colors ${
              micMode === m ? "bg-accent/30 text-accent" : "text-slate-400 hover:text-slate-200"
            }`}
            title={m === "ptt" ? "Push-to-talk (hold button or spacebar)" : "Open mic (tap to listen continuously)"}
          >
            {m === "ptt" ? "PTT" : "OPEN"}
          </button>
        ))}
      </div>

      <motion.button
        whileTap={{ scale: 0.92 }}
        {...pttHandlers}
        className={`relative flex flex-col items-center justify-center gap-1 rounded-xl px-4 py-2 min-w-[84px] transition-colors touch-none ${
          active ? "bg-accent/30 text-accent glow-accent pulse" : live ? "bg-accent/15 text-accent" : "bg-edge/40 text-slate-200 hover:bg-edge/70"
        }`}
        title={ptt ? "Push to talk — hold button or spacebar" : "Open mic — tap to toggle listening"}
      >
        {state === "connecting" ? (
          <Loader2 size={20} className="animate-spin" />
        ) : active ? (
          <Mic size={20} />
        ) : (
          <MicOff size={20} />
        )}
        <span className="text-[10px] font-semibold tracking-wide">{label}</span>
        {live && !active && (
          <span className="absolute -top-1 -right-1 h-2 w-2 rounded-full bg-accent" title="connected" />
        )}
      </motion.button>
    </div>
  );
}
