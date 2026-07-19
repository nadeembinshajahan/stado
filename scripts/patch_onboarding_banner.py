"""Demo-only patch: 'Try these commands' onboarding banner + persistent ? button.

Behavior:
  - On every fresh session (sessionStorage key empty) the banner pops up ~600ms
    after the SPA mounts, dimming the rest of the UI behind a blurred backdrop.
  - Click anywhere outside the card, the [×] icon, or the "Got it" button to
    dismiss. Dismissal persists for the session.
  - A small (?) button in the mobile top bar re-opens it at any time.

Why a sessionStorage key (not localStorage): every reviewer arriving at the
demo URL is a first-timer in this conversation — they want the hints. But a
returning visitor in the SAME tab doesn't want to keep seeing it.

Files this patch touches (CWD = staged frontend root, /src in Stage A):
  - CREATES  src/components/demo/OnboardingBanner.tsx
  - CREATES  src/components/demo/HelpButton.tsx
  - MODIFIES src/App.tsx                              (mounts the banner)
  - MODIFIES src/components/mobile/MobileTopBar.tsx   (adds the ? button)
"""
from __future__ import annotations

import pathlib
import sys
import textwrap


ONBOARDING_BANNER_TSX = textwrap.dedent('''
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
''').lstrip()


HELP_BUTTON_TSX = textwrap.dedent('''
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
''').lstrip()


def _inject_first_child(file_path: str, root_div_signature: str) -> bool:
    """Add an import + insert <OnboardingBanner /> as the first child of the
    div that matches `root_div_signature`. Returns True if the file changed."""
    p = pathlib.Path(file_path)
    if not p.exists():
        return False
    src = p.read_text()
    if "OnboardingBanner" in src:
        print(f"patch_onboarding_banner: {file_path} already patched — skip")
        return False

    # Compute the import path relative to this file's location. App.tsx is at
    # src/App.tsx → "./components/demo/OnboardingBanner". MobileShell is at
    # src/components/mobile/MobileShell.tsx → "../demo/OnboardingBanner".
    rel = pathlib.Path(file_path).parent
    target = pathlib.Path("src/components/demo/OnboardingBanner")
    import_path = pathlib.Path("/" + str(target)).relative_to("/" + str(rel)) \
        if False else None  # noqa — manual case-by-case below to stay readable
    if file_path == "src/App.tsx":
        import_line = 'import OnboardingBanner from "./components/demo/OnboardingBanner";'
    elif file_path.endswith("MobileShell.tsx"):
        import_line = 'import OnboardingBanner from "../demo/OnboardingBanner";'
    else:
        raise ValueError(f"unknown file for import-path computation: {file_path}")

    # 1. Insert import after the last existing import statement.
    lines = src.split("\n")
    insert_at = 0
    for i, ln in enumerate(lines):
        if ln.startswith("import "):
            insert_at = i + 1
    lines.insert(insert_at, import_line)
    src = "\n".join(lines)

    # 2. Inject `<OnboardingBanner />` as the FIRST child of the matching div.
    #    Strategy: find the signature, then insert the banner right after the
    #    closing `>` of that opening tag (preserving the indent of the next line).
    idx = src.find(root_div_signature)
    if idx == -1:
        sys.exit(
            f"patch_onboarding_banner: couldn't find anchor in {file_path}:\n  {root_div_signature!r}"
        )
    # Find the end of the opening tag (the `>` that closes it).
    tag_end = src.find(">", idx)
    if tag_end == -1:
        sys.exit(f"patch_onboarding_banner: unclosed opening tag in {file_path}")
    # Find the indentation of the NEXT non-empty line — that tells us how to
    # indent our inserted child.
    after = src[tag_end + 1:]
    # Skip a leading newline if present.
    if after.startswith("\n"):
        after = after[1:]
        offset = tag_end + 2
    else:
        offset = tag_end + 1
    # Capture leading whitespace as the indent.
    leading = ""
    for ch in after:
        if ch in " \t":
            leading += ch
        else:
            break
    insertion = f"\n{leading}<OnboardingBanner />"
    src = src[:tag_end + 1] + insertion + src[tag_end + 1:]
    p.write_text(src)
    print(f"patch_onboarding_banner: patched {file_path} (mounted OnboardingBanner)")
    return True


def patch_app_tsx() -> None:
    # Desktop root div in App.tsx.
    _inject_first_child(
        "src/App.tsx",
        '<div className="relative h-full w-full overflow-hidden">',
    )


def patch_mobile_shell() -> None:
    # Mobile root div in MobileShell.tsx — only present in the mobile codebase.
    _inject_first_child(
        "src/components/mobile/MobileShell.tsx",
        '<div className="relative h-full w-full overflow-hidden bg-ink">',
    )


def patch_status_bar() -> None:
    """Add a ? help button to the DESKTOP status bar (StatusBar.tsx), next to
    the existing Reports button. Without this, desktop users can dismiss the
    onboarding banner once and have no way to bring it back."""
    p = pathlib.Path("src/components/StatusBar.tsx")
    if not p.exists():
        print("patch_onboarding_banner: StatusBar.tsx not found — skip desktop ? button")
        return
    src = p.read_text()
    if "HelpButton" in src:
        print("patch_onboarding_banner: StatusBar.tsx already patched — skip")
        return

    # 1. Add the import after the last existing import.
    lines = src.split("\n")
    insert_at = 0
    for i, ln in enumerate(lines):
        if ln.startswith("import "):
            insert_at = i + 1
    lines.insert(insert_at, 'import HelpButton from "./demo/HelpButton";')
    src = "\n".join(lines)

    # 2. Insert <HelpButton /> right after the Reports button. We anchor on the
    #    unique `title="Open mission reports"` attribute and walk forward to the
    #    closing </button>.
    anchor = '          title="Open mission reports"\n        >'
    if anchor not in src:
        # Tolerate small whitespace drift.
        anchor = 'title="Open mission reports"'
    idx = src.find(anchor)
    if idx == -1:
        sys.exit("patch_onboarding_banner: couldn't find Reports button in StatusBar.tsx")
    # Find the </button> that closes this button.
    end = src.find("</button>", idx)
    if end == -1:
        sys.exit("patch_onboarding_banner: couldn't find closing tag of Reports button")
    insertion_point = end + len("</button>")
    insertion = "\n        <HelpButton />"
    src = src[:insertion_point] + insertion + src[insertion_point:]
    p.write_text(src)
    print("patch_onboarding_banner: patched StatusBar.tsx (added desktop HelpButton)")


def patch_mobile_topbar() -> None:
    p = pathlib.Path("src/components/mobile/MobileTopBar.tsx")
    if not p.exists():
        # MobileTopBar may not exist in older trees; not fatal — the banner still
        # works, just no persistent ? button on mobile.
        print("patch_onboarding_banner: MobileTopBar.tsx not found — banner only, no ? button")
        return

    src = p.read_text()
    if "HelpButton" in src:
        print("patch_onboarding_banner: MobileTopBar.tsx already patched — skip")
        return

    # Add the import after the last existing import.
    lines = src.split("\n")
    insert_at = 0
    for i, ln in enumerate(lines):
        if ln.startswith("import "):
            insert_at = i + 1
    lines.insert(
        insert_at,
        'import HelpButton from "../demo/HelpButton";'
    )
    src = "\n".join(lines)

    # Insert <HelpButton /> just before the OVERFLOW MENU BUTTON. We anchor on
    # the JSX comment that precedes it (NOT on `<MoreHorizontal>` — that's the
    # icon INSIDE the overflow button, so anchoring there embeds HelpButton in
    # the wrong place). Try several comment phrasings; bail loudly if none match.
    candidates = [
        "{/* overflow menu */}",
        "{/* overflow */}",
        "{/* ⋯ overflow */}",
        "{/* more */}",
    ]
    anchor = next((c for c in candidates if c in src), None)
    if anchor is None:
        sys.exit(
            "patch_onboarding_banner: couldn't find a 'overflow menu' anchor "
            "comment in MobileTopBar.tsx — upstream changed?"
        )
    # Preserve the exact 8-space indent on the comment line.
    src = src.replace(anchor, "<HelpButton />\n\n        " + anchor, 1)

    p.write_text(src)
    print("patch_onboarding_banner: patched MobileTopBar.tsx (added HelpButton)")


def main() -> None:
    # Create the demo/ subdir + component files.
    demo_dir = pathlib.Path("src/components/demo")
    demo_dir.mkdir(parents=True, exist_ok=True)

    banner_path = demo_dir / "OnboardingBanner.tsx"
    if not banner_path.exists():
        banner_path.write_text(ONBOARDING_BANNER_TSX)
        print(f"patch_onboarding_banner: wrote {banner_path}")
    else:
        print(f"patch_onboarding_banner: {banner_path} already present — skip write")

    help_path = demo_dir / "HelpButton.tsx"
    if not help_path.exists():
        help_path.write_text(HELP_BUTTON_TSX)
        print(f"patch_onboarding_banner: wrote {help_path}")
    else:
        print(f"patch_onboarding_banner: {help_path} already present — skip write")

    patch_app_tsx()
    patch_mobile_shell()
    patch_mobile_topbar()
    patch_status_bar()


if __name__ == "__main__":
    main()
