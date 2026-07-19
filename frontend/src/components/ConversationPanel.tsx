import { useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { MessagesSquare, Trash2, User, Sparkles } from "lucide-react";
import { useGcs, type ConvEntry } from "../store/useGcs";

const fmtTime = (ts: number) =>
  new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

const fmtArgs = (args: Record<string, unknown>) =>
  Object.entries(args)
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`)
    .join(", ");

function ToolChip({ entry }: { entry: ConvEntry }) {
  const t = entry.tool!;
  const resolved = t.ok != null;
  return (
    <div className="flex justify-start">
      <div
        className={`flex max-w-[88%] items-center gap-1.5 rounded-lg border px-2 py-1 font-mono text-[11px] ${
          resolved
            ? t.ok
              ? "border-ok/30 bg-ok/10 text-ok"
              : "border-danger/30 bg-danger/10 text-danger"
            : "border-edge/60 bg-edge/30 text-slate-300"
        }`}
        title={`${t.name}(${fmtArgs(t.args)})`}
      >
        <span className="opacity-70">▸</span>
        <span className="truncate">
          {t.name}
          <span className="opacity-60">({fmtArgs(t.args)})</span>
        </span>
        {resolved && <span className="shrink-0">{t.ok ? "✓" : "✗"}</span>}
      </div>
    </div>
  );
}

function Bubble({ entry }: { entry: ConvEntry }) {
  const isUser = entry.role === "user";
  return (
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div className={`flex max-w-[88%] flex-col gap-0.5 ${isUser ? "items-end" : "items-start"}`}>
        <div className="flex items-center gap-1 px-1 text-[9px] uppercase tracking-wide text-slate-500">
          {isUser ? (
            <>
              <User size={9} /> You
            </>
          ) : (
            <>
              <Sparkles size={9} className="text-accent" /> STADO
            </>
          )}
          <span className="opacity-60">· {fmtTime(entry.ts)}</span>
        </div>
        <div
          className={`whitespace-pre-wrap break-words rounded-xl px-2.5 py-1.5 text-xs leading-snug ${
            isUser
              ? "rounded-tr-sm bg-edge/55 text-slate-100"
              : "rounded-tl-sm bg-accent/15 text-accent"
          }`}
        >
          {entry.text}
        </div>
      </div>
    </div>
  );
}

export default function ConversationPanel() {
  const conversation = useGcs((s) => s.conversation);
  const convClear = useGcs((s) => s.convClear);
  const scrollRef = useRef<HTMLDivElement>(null);

  // auto-scroll to newest as the transcript streams/grows
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [conversation]);

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25 }}
      className="glass instrument flex max-h-[45vh] w-96 flex-col overflow-hidden rounded-xl"
    >
      <div className="flex items-center justify-between border-b border-edge/60 px-3 py-1.5">
        <div className="flex items-center gap-2 text-xs">
          <MessagesSquare size={14} className="text-accent" />
          <span className="font-semibold tracking-wide">STADO · Conversation</span>
        </div>
        <button
          onClick={convClear}
          disabled={conversation.length === 0}
          className="rounded p-1 text-slate-500 transition-colors hover:text-slate-200 disabled:opacity-30"
          title="Clear conversation"
        >
          <Trash2 size={13} />
        </button>
      </div>

      <div ref={scrollRef} className="flex-1 space-y-2 overflow-y-auto px-3 py-2.5">
        {conversation.length === 0 ? (
          <div className="flex h-24 flex-col items-center justify-center gap-1.5 text-slate-500">
            <MessagesSquare size={18} className="opacity-50" />
            <span className="text-[11px]">Hold the mic and speak…</span>
          </div>
        ) : (
          <AnimatePresence initial={false}>
            {conversation.map((entry) => (
              <motion.div
                key={entry.id}
                layout
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.18 }}
              >
                {entry.role === "tool" ? <ToolChip entry={entry} /> : <Bubble entry={entry} />}
              </motion.div>
            ))}
          </AnimatePresence>
        )}
      </div>
    </motion.div>
  );
}
