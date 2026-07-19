import { useRef } from "react";
import { AnimatePresence, motion, useDragControls, type PanInfo } from "framer-motion";
import { X } from "lucide-react";
import { useGo2RtcWebRTC } from "../../lib/useGo2Rtc";
import { useGcs } from "../../store/useGcs";

const MAIN_STREAM = import.meta.env.VITE_GO2RTC_STREAM ?? "drone";
const OUTRIDER_STREAM = "outrider";

/**
 * Bottom sheet that previews the two live video feeds (Overwatch + Outrider).
 * Mounted only when the operator opens it from the top bar menu — the desktop
 * `VideoPanel`/`SecondFeedPanel` floating panels are NOT rendered on mobile
 * (they'd cover the map). This is a strictly view-only mobile affordance;
 * click-to-track / acquire-by-text remains desktop-only for now (drawing a
 * pixel-precise box on a phone is awful).
 *
 * Drag down to dismiss. Both feeds get their own `useGo2RtcWebRTC` so closing
 * the sheet tears the peer connections down (they re-establish on next open).
 */
export default function MobileVideoSheet({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  return <Inner open={open} onClose={onClose} />;
}

function Inner({ open, onClose }: { open: boolean; onClose: () => void }) {
  const dragControls = useDragControls();
  return (
    <AnimatePresence>
      {open && (
        <>
          {/* dimmed backdrop */}
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 0.55 }}
            exit={{ opacity: 0 }}
            onClick={onClose}
            className="fixed inset-0 z-[65] bg-black"
          />
          <motion.div
            key="sheet"
            initial={{ y: "100%" }}
            animate={{ y: 0 }}
            exit={{ y: "100%" }}
            transition={{ type: "spring", stiffness: 380, damping: 38 }}
            drag="y"
            dragControls={dragControls}
            dragListener={false}
            dragConstraints={{ top: 0, bottom: 600 }}
            dragElastic={0.05}
            onDragEnd={(_, info: PanInfo) => {
              if (info.offset.y > 120 || info.velocity.y > 600) onClose();
            }}
            className="glass safe-bottom safe-x fixed inset-x-0 bottom-0 z-[70] flex max-h-[85vh] flex-col rounded-t-2xl"
          >
            <div
              className="tap flex items-center justify-between px-3 py-2"
              style={{ touchAction: "none" }}
              onPointerDown={(e) => dragControls.start(e)}
            >
              <div className="flex flex-1 items-center justify-center">
                <div className="h-1.5 w-12 rounded-full bg-slate-500/70" />
              </div>
              <button
                onClick={onClose}
                className="tap absolute right-2 top-2 rounded-full p-2 text-slate-400"
              >
                <X size={16} />
              </button>
            </div>

            <div className="flex-1 space-y-3 overflow-y-auto px-3 pb-3">
              <FeedTile stream={MAIN_STREAM} label="OVERWATCH" />
              <FeedTile stream={OUTRIDER_STREAM} label="OUTRIDER" aspect="aspect-[4/3]" />
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}

function FeedTile({
  stream,
  label,
  aspect = "aspect-video",
}: {
  stream: string;
  label: string;
  aspect?: string;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const state = useGo2RtcWebRTC(videoRef, stream, true);
  const live = state === "live";
  const pushLog = useGcs((s) => s.pushLog);

  return (
    <div className="overflow-hidden rounded-xl border border-edge/60 bg-black">
      <div className="flex items-center justify-between border-b border-edge/60 px-2.5 py-1.5">
        <span className="text-xs font-bold tracking-wider text-slate-100">
          {label}
        </span>
        <span
          className={`rounded px-1.5 py-0.5 text-[10px] font-bold ${
            live ? "bg-accent/20 text-accent" : "bg-edge/50 text-slate-400"
          }`}
        >
          {live ? "LIVE" : state === "connecting" ? "…" : "OFFLINE"}
        </span>
      </div>
      <div className={`relative ${aspect} bg-black`}>
        <video
          ref={videoRef}
          autoPlay
          playsInline
          muted
          className="h-full w-full object-contain"
          onError={() => pushLog("voice", `${label.toLowerCase()} feed error`, 4)}
        />
        {!live && (
          <div className="absolute inset-0 flex items-center justify-center text-xs text-slate-500">
            {state === "error" ? `${label} offline` : "connecting…"}
          </div>
        )}
      </div>
    </div>
  );
}
