import { useEffect, useState } from "react";
import { Circle, Loader2 } from "lucide-react";
import { useGcs } from "../store/useGcs";
import { api } from "../lib/api";

/**
 * REC toggle for a go2rtc stream → records it to a local MP4 on the Mac (the
 * backend runs `ffmpeg -c copy` of go2rtc's RTSP restream — no re-encode).
 * Shared by both feed headers: Outrider ("outrider") and Overwatch ("drone").
 * Self-contained — it mount-syncs from /record/status (so a recording started
 * before a page reload still shows as live) and ticks a 1 Hz elapsed timer.
 */
export default function RecordButton({ stream }: { stream: string }) {
  const pushLog = useGcs((s) => s.pushLog);
  // recordingSince = unix start (s) while recording, else null. recBusy debounces
  // the start/stop request; `now` ticks 1 Hz so the elapsed mm:ss re-renders.
  const [recordingSince, setRecordingSince] = useState<number | null>(null);
  const [recBusy, setRecBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());

  // On mount, resync in case this stream is already recording (survived a reload).
  useEffect(() => {
    let cancelled = false;
    api.record
      .status()
      .then((s) => {
        const r = s?.recording?.[stream];
        if (!cancelled && r) setRecordingSince(r.since_unix);
      })
      .catch(() => {/* best-effort */});
    return () => {
      cancelled = true;
    };
  }, [stream]);

  // 1 Hz tick to keep the elapsed timer live (only while recording).
  useEffect(() => {
    if (recordingSince == null) return;
    setNow(Date.now());
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [recordingSince]);

  const elapsed = (() => {
    if (recordingSince == null) return "00:00";
    const secs = Math.max(0, Math.floor(now / 1000 - recordingSince));
    const mm = String(Math.floor(secs / 60)).padStart(2, "0");
    const ss = String(secs % 60).padStart(2, "0");
    return `${mm}:${ss}`;
  })();

  const toggleRecord = async () => {
    if (recBusy) return;
    setRecBusy(true);
    try {
      if (recordingSince == null) {
        const r = await api.record.start(stream);
        if (r?.ok === false && r.already) {
          // Already recording (started elsewhere / before reload) — that's NOT a
          // failure. Reflect reality: pull the real start time from /record/status
          // so the REC indicator + elapsed timer are correct.
          const s = await api.record.status().catch(() => null);
          const existing = s?.recording?.[stream];
          setRecordingSince(existing?.since_unix ?? Date.now() / 1000);
        } else if (r?.ok === false) {
          pushLog("error", "could not start recording", 3);
        } else {
          setRecordingSince(Date.now() / 1000);
        }
      } else {
        const r = await api.record.stop(stream);
        setRecordingSince(null);
        if (r?.file) pushLog("rec", `saved ${r.file.split("/").pop()}`);
      }
    } catch (err) {
      pushLog("error", `record: ${(err as Error).message}`, 3);
    } finally {
      setRecBusy(false);
    }
  };

  return (
    <button
      onClick={(e) => {
        e.stopPropagation(); // header is a drag handle — don't start a drag
        void toggleRecord();
      }}
      disabled={recBusy}
      className={`flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold disabled:opacity-50 ${
        recordingSince != null ? "bg-danger/20 text-danger" : "text-slate-300 hover:text-danger"
      }`}
      title={recordingSince != null ? "Stop recording" : "Record this feed to MP4"}
    >
      {recBusy ? (
        <Loader2 size={11} className="animate-spin" />
      ) : (
        <Circle size={9} className={recordingSince != null ? "animate-pulse fill-current" : ""} />
      )}
      {recordingSince != null ? `REC ${elapsed}` : "REC"}
    </button>
  );
}
