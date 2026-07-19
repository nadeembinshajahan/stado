/**
 * Browser side of the realtime voice bridge (Qwen Realtime via /ws/voice).
 * Captures mic PCM16 @16 kHz → /ws/voice; plays the PCM16 @24 kHz reply.
 */

// Realtime input transcription often mishears drone-domain words (famously
// "drones" → "cones"). The MODEL still interprets the command correctly from the
// audio; this only cleans up the DISPLAYED transcript so it reads right. Whole-word,
// case-insensitive; high-confidence domain terms only so we never corrupt real text.
const TRANSCRIPT_FIXES: [RegExp, string][] = [
  [/\b(?:cones|clones|crones|drome|drones'|drone's)\b/gi, "drones"],
  [/\bout[\s-]?rider(s)?\b/gi, "Outrider"],
  [/\bover[\s-]?watch(ed|es)?\b/gi, "Overwatch"],
  [/\bway[\s-]?point(s)?\b/gi, "waypoint$1"],
  [/\bloyter\b/gi, "loiter"],
  [/\b(?:r\.?\s?t\.?\s?l|are tee elle|or tell)\b/gi, "RTL"],
];
export function fixTranscript(text: string): string {
  let out = text;
  for (const [re, sub] of TRANSCRIPT_FIXES) out = out.replace(re, sub);
  return out;
}
export interface VoiceHandlers {
  onState: (s: "connecting" | "live" | "closed" | "error") => void;
  onHeard: (text: string) => void;
  onSaid: (text: string) => void;
  onTool: (name: string, args: Record<string, unknown>) => void;
  onToolResult?: (name: string, result: Record<string, unknown>) => void;
  onError: (msg: string) => void;
}

export interface VoiceSession {
  stop: () => void;
  setTalking: (on: boolean) => void; // push-to-talk gate
}

export async function startVoice(
  h: VoiceHandlers,
  opts: { manualVad?: boolean } = {},
): Promise<VoiceSession> {
  h.onState("connecting");

  // manualVad (push-to-talk): the client marks utterance start/end so the server
  // responds the instant PTT is released. auto: server VAD segments turns (open mic).
  const manualVad = opts.manualVad ?? true;
  const vad = manualVad ? "manual" : "auto";
  const wsUrl = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/voice?vad=${vad}`;
  const ws = new WebSocket(wsUrl);
  ws.binaryType = "arraybuffer";

  // Capture @16 kHz (AudioContext resamples the mic for us).
  const inCtx = new AudioContext({ sampleRate: 16000 });
  const outCtx = new AudioContext({ sampleRate: 24000 });
  let playHead = 0;
  let stream: MediaStream | null = null;
  let proc: ScriptProcessorNode | null = null;
  let talking = false; // push-to-talk: only stream mic while held

  const playPcm = (ab: ArrayBuffer) => {
    const i16 = new Int16Array(ab);
    const f32 = new Float32Array(i16.length);
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 0x8000;
    const buf = outCtx.createBuffer(1, f32.length, 24000);
    buf.getChannelData(0).set(f32);
    const node = outCtx.createBufferSource();
    node.buffer = buf;
    node.connect(outCtx.destination);
    const t = Math.max(outCtx.currentTime, playHead);
    node.start(t);
    playHead = t + buf.duration;
  };

  ws.onmessage = (ev) => {
    if (ev.data instanceof ArrayBuffer) {
      playPcm(ev.data);
      return;
    }
    const m = JSON.parse(ev.data);
    switch (m.type) {
      case "ready": h.onState("live"); break;
      case "heard": h.onHeard(fixTranscript(m.text)); break;
      case "said": h.onSaid(m.text); break;
      case "tool": h.onTool(m.name, m.args ?? {}); break;
      case "tool_result": h.onToolResult?.(m.name, m.result ?? {}); break;
      case "error": h.onError(m.message); h.onState("error"); break;
    }
  };
  ws.onclose = () => h.onState("closed");
  ws.onerror = () => h.onState("error");

  // If the WS never opens (8 s timeout) — or the mic grab below fails — we must
  // release the two AudioContexts + the socket, or repeated failed PTT presses
  // leak AudioContexts (browsers cap them ~6) until voice stops working entirely.
  try {
    await new Promise<void>((res, rej) => {
      ws.onopen = () => res();
      setTimeout(() => rej(new Error("voice ws timeout")), 8000);
    });

    stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
  } catch (err) {
    stream?.getTracks().forEach((t) => t.stop());
    inCtx.close().catch(() => {});
    outCtx.close().catch(() => {});
    try { ws.close(); } catch { /* */ }
    throw err;
  }
  const src = inCtx.createMediaStreamSource(stream);
  proc = inCtx.createScriptProcessor(4096, 1, 1);
  src.connect(proc);
  const mute = inCtx.createGain();
  mute.gain.value = 0;
  proc.connect(mute);
  mute.connect(inCtx.destination); // keep the processor pumping, silently

  proc.onaudioprocess = (e) => {
    if (ws.readyState !== WebSocket.OPEN || !talking) return; // PTT: silent unless held
    const f32 = e.inputBuffer.getChannelData(0);
    const i16 = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) {
      const s = Math.max(-1, Math.min(1, f32[i]));
      i16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    ws.send(i16.buffer);
  };

  return {
    setTalking: (on: boolean) => {
      talking = on;
      // Manual VAD: mark the utterance boundary so the server finalises the turn
      // the instant PTT is released (start before audio flows, end right after it stops).
      if (manualVad && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: on ? "activity_start" : "activity_end" }));
      }
    },
    stop: () => {
      try { proc?.disconnect(); } catch { /* */ }
      stream?.getTracks().forEach((t) => t.stop());
      inCtx.close();
      outCtx.close();
      ws.close();
    },
  };
}
