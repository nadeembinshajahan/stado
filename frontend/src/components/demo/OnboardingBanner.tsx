import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { Hand, HelpCircle, Mic, X } from "lucide-react";

// One-tab session key. Reviewer arrives fresh → banner shows. They dismiss →
// stays dismissed until tab close. (localStorage would be too sticky for a
// shared demo URL.)
const STORAGE_KEY = "stado-onboarded-v1";

// Singleton ref so `<HelpButton />` (mounted elsewhere) can re-open this
// without prop-drilling or a store. Set when the banner first mounts.
declare global {
  interface Window {
    __stadoShowOnboarding?: () => void;
  }
}

export default function OnboardingBanner() {
  const [open, setOpen] = useState(false);

  // Show on first paint after a short delay so the SPA has a moment to render.
  useEffect(() => {
    if (sessionStorage.getItem(STORAGE_KEY)) return;
    const t = window.setTimeout(() => setOpen(true), 600);
    return () => window.clearTimeout(t);
  }, []);

  // Expose to the ? button.
  useEffect(() => {
    window.__stadoShowOnboarding = () => setOpen(true);
    return () => {
      delete window.__stadoShowOnboarding;
    };
  }, []);

  const close = () => {
    setOpen(false);
    try {
      sessionStorage.setItem(STORAGE_KEY, "1");
    } catch {
      /* private mode etc — best-effort */
    }
  };

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          onClick={close}
          className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm safe-x"
          style={{
            paddingTop: "env(safe-area-inset-top)",
            paddingBottom: "env(safe-area-inset-bottom)",
          }}
          role="dialog"
          aria-modal="true"
          aria-labelledby="stado-onboarding-title"
        >
          <motion.div
            initial={{ scale: 0.96, opacity: 0, y: 16 }}
            animate={{ scale: 1, opacity: 1, y: 0 }}
            exit={{ scale: 0.96, opacity: 0, y: 16 }}
            transition={{ type: "spring", damping: 24, stiffness: 280 }}
            onClick={(e) => e.stopPropagation()}
            className="relative m-4 w-[min(92vw,420px)] rounded-2xl border border-edge bg-panel/95 p-5 shadow-2xl backdrop-blur"
          >
            <button
              onClick={close}
              aria-label="Close"
              className="tap absolute right-3 top-3 rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-edge/60 hover:text-slate-100"
            >
              <X size={18} />
            </button>

            <div className="mb-1 flex items-center gap-2 text-[11px] font-bold uppercase tracking-wider text-accent">
              <HelpCircle size={14} /> Try these commands
            </div>
            <h2
              id="stado-onboarding-title"
              className="mb-4 pr-6 text-lg font-semibold leading-tight text-slate-100"
            >
              Welcome to <span className="text-accent">STADO</span>.
              Two simulated drones, one ground station.
            </h2>

            <div className="space-y-4 text-sm text-slate-200">
              <Section
                icon={<Mic size={14} className="text-accent" />}
                label="Hold the mic and say"
              >
                <Cmd>&quot;Take off both drones to 20 meters&quot;</Cmd>
                <Cmd>&quot;Survey the area I&apos;m pointing at&quot; <span className="text-slate-400">(then tap the map)</span></Cmd>
                <Cmd>&quot;Return to home&quot;</Cmd>
                <Cmd>&quot;Set max altitude to 50 meters&quot;</Cmd>
              </Section>

              <Section
                icon={<Hand size={14} className="text-accent" />}
                label="Or just tap"
              >
                <Cmd>The command bar → ARM, TAKEOFF, HOLD, RTL, LAND</Cmd>
                <Cmd>The vehicle pill to swap drones</Cmd>
                <Cmd>The map to send a GOTO</Cmd>
                <Cmd>🔄 Reset Sim if things get weird</Cmd>
              </Section>
            </div>

            <button
              onClick={close}
              className="tap mt-5 w-full rounded-xl bg-accent py-3 text-sm font-bold text-[#021712] transition-transform active:scale-[0.98]"
            >
              Got it — let&apos;s fly
            </button>

            <div className="mt-3 text-center text-[10px] uppercase tracking-wider text-slate-500">
              Tap outside this card to close
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function Section({
  icon,
  label,
  children,
}: {
  icon: React.ReactNode;
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wider text-slate-400">
        {icon} {label}
      </div>
      <ul className="space-y-1.5">{children}</ul>
    </div>
  );
}

function Cmd({ children }: { children: React.ReactNode }) {
  return (
    <li className="rounded-lg bg-edge/40 px-2.5 py-1.5 text-[13px] leading-snug text-slate-100">
      {children}
    </li>
  );
}
