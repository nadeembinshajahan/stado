import { useEffect, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useGcs } from "../store/useGcs";

/** Branded boot splash. Shows the StratoFirma logo over a dark backdrop until
 *  the backend WebSocket is up (or a short floor elapses), then fades out. */
export default function Preloader() {
  const socketOpen = useGcs((s) => s.socketOpen);
  const [done, setDone] = useState(false);
  const [minElapsed, setMinElapsed] = useState(false);

  useEffect(() => {
    const t = setTimeout(() => setMinElapsed(true), 1400); // min on-screen time
    const hard = setTimeout(() => setDone(true), 6000); // never block forever
    return () => { clearTimeout(t); clearTimeout(hard); };
  }, []);
  useEffect(() => {
    if (socketOpen && minElapsed) setDone(true);
  }, [socketOpen, minElapsed]);

  return (
    <AnimatePresence>
      {!done && (
        <motion.div
          initial={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.6 }}
          className="fixed inset-0 z-[100] flex flex-col items-center justify-center bg-[radial-gradient(circle_at_50%_40%,#0b1322,#04070c)]"
        >
          <motion.img
            src="/strato-logo.png"
            alt="StratoFirma"
            initial={{ opacity: 0, scale: 0.92 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ duration: 0.8, ease: "easeOut" }}
            className="w-72 max-w-[60vw] object-contain"
            // The asset is a DARK logo; render it WHITE (brightness(0) invert(1))
            // with a teal glow so it's visible on the dark splash — same treatment
            // as the StatusBar logo (it was invisible dark-on-dark before).
            style={{
              filter:
                "brightness(0) invert(1) drop-shadow(0 0 10px rgba(34,227,196,0.6)) drop-shadow(0 0 32px rgba(34,227,196,0.35))",
            }}
          />
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.5 }}
            className="mt-8 flex items-center gap-2 text-xs tracking-[0.3em] text-accent/80"
          >
            <span className="h-1.5 w-1.5 animate-ping rounded-full bg-accent" />
            INITIALIZING GROUND CONTROL
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
