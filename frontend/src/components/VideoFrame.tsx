import { useRef, useState } from "react";
import { motion } from "framer-motion";
import { Maximize2, Minimize2 } from "lucide-react";
import { useGcs } from "../store/useGcs";

export type FeedId = "main" | "feed2";

/**
 * Shared chrome for the two floating video panels (main FPV/TRACK + second feed).
 * Provides three behaviors so both panels behave identically:
 *
 *  - DRAGGABLE in normal mode (framer-motion `drag`), as before.
 *  - RESIZABLE via a bottom-right corner handle (pointer-events drag → size
 *    state, kept for the session). The video area stays 16:9; width drives it.
 *  - DOUBLE-CLICK the video area → toggle FOCUS mode. The focused feed becomes
 *    large + centered + high z-index; the other shrinks to a corner thumbnail.
 *
 * Callers pass `header`, the 16:9 `video` area, and optional `controls`.
 */
export default function VideoFrame({
  feed,
  defaultPos,
  defaultWidth,
  thumbCorner = "bottom-right",
  header,
  video,
  controls,
  dragDisabled = false,
  videoAspect = "aspect-video",
}: {
  feed: FeedId;
  /** Tailwind position classes for the normal floating spot, e.g. "top-16 right-3". */
  defaultPos: string;
  defaultWidth: number; // rem
  /** Where this panel sits as a thumbnail when the OTHER feed is focused. */
  thumbCorner?: "bottom-right" | "bottom-left";
  header: React.ReactNode;
  video: (focused: boolean) => React.ReactNode;
  controls?: React.ReactNode;
  /** Disable panel dragging (e.g. while drag-drawing a click-to-track box). */
  dragDisabled?: boolean;
  /** Tailwind aspect class for the video area (default 16:9; e.g. "aspect-[4/3]" for a 640x480 feed → no letterbox). */
  videoAspect?: string;
}) {
  const focusedFeed = useGcs((s) => s.focusedFeed);
  const setFocusedFeed = useGcs((s) => s.setFocusedFeed);

  // Session-only size (rem). Resizing keeps the 16:9 video area; height tracks
  // width so we only need to track one dimension here.
  const [width, setWidth] = useState(defaultWidth);
  const resizing = useRef<{ startX: number; startW: number } | null>(null);

  const focused = focusedFeed === feed;
  const otherFocused = focusedFeed !== null && !focused;

  const toggleFocus = () => setFocusedFeed(focused ? null : feed);

  const onResizePointerDown = (e: React.PointerEvent) => {
    e.stopPropagation();
    e.preventDefault();
    resizing.current = { startX: e.clientX, startW: width };
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onResizePointerMove = (e: React.PointerEvent) => {
    if (!resizing.current) return;
    const remPx = parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
    const delta = (e.clientX - resizing.current.startX) / remPx;
    // Clamp to sane bounds (rem) so the panel can't vanish or swallow the screen.
    setWidth(Math.max(18, Math.min(72, resizing.current.startW + delta)));
  };
  const onResizePointerUp = (e: React.PointerEvent) => {
    resizing.current = null;
    try {
      (e.target as HTMLElement).releasePointerCapture(e.pointerId);
    } catch {
      /* capture may have been lost — ignore */
    }
  };

  // FOCUS: large panel CENTERED on screen. A full-screen flex layer does the
  // centering — NOT CSS-transform centering, because framer-motion's leftover
  // drag transform was overriding `-translate-*` and shoving the panel into a
  // corner. `pointer-events-none` on the layer keeps the map interactive around
  // the focused feed (only the panel itself captures clicks).
  if (focused) {
    return (
      <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 pointer-events-none">
        <div className="glass instrument pointer-events-auto w-[80vw] max-w-[80vw] rounded-xl overflow-hidden">
          <div className="flex items-center justify-between px-3 py-1.5 border-b border-edge/60">
            {header}
            <button
              onClick={toggleFocus}
              className="rounded p-0.5 text-slate-400 hover:text-accent"
              title="Exit focus"
            >
              <Minimize2 size={14} />
            </button>
          </div>
          <div
            className={`relative ${videoAspect} bg-black cursor-pointer`}
            onDoubleClick={toggleFocus}
            title="Double-click to exit focus"
          >
            {video(true)}
          </div>
          {controls}
        </div>
      </div>
    );
  }

  // THUMBNAIL: the non-focused feed shrinks to a small corner tile.
  if (otherFocused) {
    const corner = thumbCorner === "bottom-left" ? "bottom-3 left-3" : "bottom-3 right-3";
    return (
      <motion.div
        layout
        className={`glass instrument fixed ${corner} z-[55] w-56 rounded-lg overflow-hidden`}
      >
        <div className="flex items-center justify-between px-2 py-1 border-b border-edge/60 text-[11px]">
          {header}
          <button
            onClick={toggleFocus}
            className="rounded p-0.5 text-slate-400 hover:text-accent"
            title="Focus this feed"
          >
            <Maximize2 size={12} />
          </button>
        </div>
        <div
          className={`relative ${videoAspect} bg-black cursor-pointer`}
          onDoubleClick={toggleFocus}
          title="Double-click to focus"
        >
          {video(false)}
        </div>
      </motion.div>
    );
  }

  // NORMAL: draggable floating panel, resizable via the corner handle.
  return (
    <motion.div
      layout
      drag={!dragDisabled}
      dragMomentum={false}
      dragElastic={0}
      dragListener={!resizing.current && !dragDisabled}
      style={{ width: `${width}rem`, maxWidth: "60vw" }}
      className={`glass instrument absolute ${defaultPos} z-20 rounded-xl overflow-hidden`}
    >
      <div className="flex items-center justify-between px-3 py-1.5 cursor-grab active:cursor-grabbing border-b border-edge/60">
        {header}
      </div>

      <div
        className={`relative ${videoAspect} bg-black cursor-pointer`}
        onDoubleClick={toggleFocus}
        title="Double-click for fullscreen focus"
      >
        {video(false)}
      </div>

      {controls}

      {/* bottom-right resize handle */}
      <div
        onPointerDown={onResizePointerDown}
        onPointerMove={onResizePointerMove}
        onPointerUp={onResizePointerUp}
        className="absolute bottom-0 right-0 z-30 h-4 w-4 cursor-nwse-resize touch-none"
        title="Drag to resize"
      >
        <svg viewBox="0 0 16 16" className="h-full w-full text-accent/70">
          <path d="M15 5 L5 15 M15 10 L10 15 M15 15 L14 15" stroke="currentColor" strokeWidth="1.4" fill="none" />
        </svg>
      </div>
    </motion.div>
  );
}
