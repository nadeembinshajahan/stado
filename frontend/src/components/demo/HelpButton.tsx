import { HelpCircle } from "lucide-react";

// Persistent help button — re-opens the OnboardingBanner. Uses a window
// singleton (set by OnboardingBanner) so we don't have to plumb the state
// through MobileShell → MobileTopBar.
export default function HelpButton() {
  return (
    <button
      onClick={() => window.__stadoShowOnboarding?.()}
      aria-label="Show command hints"
      className="tap rounded-lg p-2 text-slate-400 transition-colors hover:bg-edge/60 hover:text-slate-100"
    >
      <HelpCircle size={18} />
    </button>
  );
}
