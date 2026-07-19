import { useEffect, useMemo, useRef } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Pause, Play, Radio, RotateCcw, SkipBack, X } from "lucide-react";
import { useGcs } from "../store/useGcs";

const SPEEDS = [0.5, 1, 2, 4, 8];

function fmtClock(s: number): string {
  const sec = Math.max(0, Math.floor(s));
  const m = Math.floor(sec / 60);
  const r = sec % 60;
  return `${m}:${r.toString().padStart(2, "0")}`;
}

/**
 * Mission-replay transport. Owns the playback clock: a rAF loop advances
 * `replay.t` while playing (scaled by speed) on a SHARED clock keyed on absolute
 * timestamps spanning the mission window, and the map views read each drone's
 * pose off it. Play/pause, a scrub timeline (with per-drone mode-change + agent-
 * action ticks, color-coded by drone), and a speed selector. Surfaces each
 * drone's current mode + most-recent action as the playhead crosses them.
 */
export default function ReplayPlayer() {
  const replay = useGcs((s) => s.replay);
  const setPlaying = useGcs((s) => s.setReplayPlaying);
  const setSpeed = useGcs((s) => s.setReplaySpeed);
  const setTime = useGcs((s) => s.setReplayTime);
  const stop = useGcs((s) => s.stopReplay);

  const raf = useRef<number | null>(null);
  const last = useRef<number>(0);

  // rAF clock — advances t by wall-time * speed; pauses at the end.
  useEffect(() => {
    if (!replay || !replay.playing) {
      if (raf.current != null) cancelAnimationFrame(raf.current);
      raf.current = null;
      return;
    }
    last.current = performance.now();
    const tick = (now: number) => {
      const dt = (now - last.current) / 1000;
      last.current = now;
      const st = useGcs.getState();
      const r = st.replay;
      if (!r || !r.playing) return;
      const next = r.t + dt * r.speed;
      if (next >= r.duration) {
        st.setReplayTime(r.duration);
        st.setReplayPlaying(false);
        return;
      }
      st.setReplayTime(next);
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => {
      if (raf.current != null) cancelAnimationFrame(raf.current);
      raf.current = null;
    };
  }, [replay?.playing, replay?.flightId]);

  // Per-drone current mode + most-recent action at the current playhead (on the
  // shared absolute clock). Each drone reports independently so a mission shows
  // every drone's state at once.
  const perDrone = useMemo(() => {
    if (!replay) return [] as { name: string; color: string; mode: string | null; action: string | null }[];
    const absT = replay.startTs + replay.t;
    return replay.drones.map((d) => {
      let mode: string | null = null;
      for (const m of d.modeTimeline) {
        if (m.ts <= absT) mode = m.mode;
        else break;
      }
      let action: string | null = null;
      for (const a of d.actions) {
        if (a.ts <= absT) action = a.label;
        else break;
      }
      return { name: d.vehicleName, color: d.color, mode, action };
    });
  }, [replay?.t, replay?.flightId, replay?.drones, replay?.startTs]);

  const isMission = !!replay?.missionId && (replay?.drones.length ?? 0) > 1;

  return (
    <AnimatePresence>
      {replay && (
        <motion.div
          key="replay"
          initial={{ y: 24, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={{ y: 24, opacity: 0 }}
          className="glass instrument absolute bottom-20 left-1/2 z-40 w-[min(720px,92vw)] -translate-x-1/2 rounded-xl px-4 py-3"
        >
          <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <span className="flex items-center gap-1.5 rounded-md bg-accent/15 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-accent">
              <Radio size={11} /> {isMission ? "Mission Replay" : "Replay"}
            </span>
            {/* Per-drone status chips: name (in its color), current mode, last action. */}
            {perDrone.map((d, i) => (
              <span key={i} className="flex items-center gap-1.5">
                <span
                  className="h-2 w-2 rounded-full"
                  style={{ background: d.color }}
                />
                <span className="text-sm font-semibold" style={{ color: d.color }}>
                  {d.name}
                </span>
                {d.mode && (
                  <span className="rounded bg-edge/50 px-1.5 py-0.5 text-[10px] font-semibold text-slate-200">
                    {d.mode}
                  </span>
                )}
                {d.action && (
                  <span className="truncate text-[10px] text-slate-400" title={d.action}>
                    ▸ {d.action}
                  </span>
                )}
              </span>
            ))}
            <div className="flex-1" />
            <button
              onClick={stop}
              className="flex items-center gap-1 rounded-md bg-edge/40 px-2 py-1 text-[11px] font-semibold text-slate-300 transition-colors hover:bg-danger/20 hover:text-danger"
              title="Exit replay (back to live)"
            >
              <X size={13} /> Exit
            </button>
          </div>

          <div className="flex items-center gap-3">
            <button
              onClick={() => setTime(0)}
              className="text-slate-400 transition-colors hover:text-slate-100"
              title="Restart"
            >
              <SkipBack size={16} />
            </button>
            <button
              onClick={() => {
                // At the end, play restarts from 0.
                if (replay.t >= replay.duration - 0.01) setTime(0);
                setPlaying(!replay.playing);
              }}
              className="flex h-9 w-9 items-center justify-center rounded-full bg-accent/20 text-accent transition-colors hover:bg-accent/30"
              title={replay.playing ? "Pause" : "Play"}
            >
              {replay.t >= replay.duration - 0.01 ? (
                <RotateCcw size={16} />
              ) : replay.playing ? (
                <Pause size={16} />
              ) : (
                <Play size={16} />
              )}
            </button>
            <span className="tnum w-12 text-right text-[11px] text-slate-400">
              {fmtClock(replay.t)}
            </span>

            {/* scrub track with event ticks */}
            <div className="relative flex-1">
              <input
                type="range"
                min={0}
                max={replay.duration}
                step={0.1}
                value={replay.t}
                onChange={(e) => {
                  setPlaying(false);
                  setTime(Number(e.target.value));
                }}
                className="w-full accent-[#22e3c4]"
              />
              {/* Merged ticks across ALL drones — mode changes (thin, in the
                  drone's color) + agent actions (taller; danger red if failed,
                  else the drone's color) — positioned on the shared clock. */}
              <div className="pointer-events-none absolute inset-x-0 top-1/2 -translate-y-1/2">
                {replay.drones.flatMap((d, di) =>
                  d.modeTimeline.map((m, i) => {
                    const frac = (m.ts - replay.startTs) / replay.duration;
                    if (frac < 0 || frac > 1) return null;
                    return (
                      <span
                        key={`m${di}-${i}`}
                        className="absolute h-2 w-[2px] -translate-x-1/2"
                        style={{ left: `${frac * 100}%`, background: d.color, opacity: 0.7 }}
                        title={`${d.vehicleName}: ${m.mode}`}
                      />
                    );
                  }),
                )}
                {replay.drones.flatMap((d, di) =>
                  d.actions.map((a, i) => {
                    const frac = (a.ts - replay.startTs) / replay.duration;
                    if (frac < 0 || frac > 1) return null;
                    return (
                      <span
                        key={`a${di}-${i}`}
                        className="absolute h-3 w-[2px] -translate-x-1/2"
                        style={{
                          left: `${frac * 100}%`,
                          background: a.ok ? d.color : "#ff4d5e",
                          opacity: 0.9,
                        }}
                        title={`${d.vehicleName}: ${a.label}`}
                      />
                    );
                  }),
                )}
              </div>
            </div>

            <span className="tnum w-12 text-[11px] text-slate-500">
              {fmtClock(replay.duration)}
            </span>

            <select
              value={replay.speed}
              onChange={(e) => setSpeed(Number(e.target.value))}
              className="rounded-md border border-edge/60 bg-ink/70 px-1.5 py-1 text-[11px] text-slate-200"
              title="Playback speed"
            >
              {SPEEDS.map((s) => (
                <option key={s} value={s}>
                  {s}×
                </option>
              ))}
            </select>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
