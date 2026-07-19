import { useEffect, useState } from "react";

/**
 * Single source of truth for the "are we on a phone-class viewport" decision
 * that drives `App.tsx`'s shell swap. Mobile = the matchMedia `(max-width:
 * 767px)` bucket (Tailwind's default md breakpoint). Tablets (≥768) and
 * desktop (≥1024) both render the existing desktop shell — tablets just get a
 * `pointer:coarse` tap-target shim via index.css.
 *
 * Synchronous initial read so the very first paint already picks the right
 * tree (no flash from a `false` → `true` toggle on mount). Listens to the
 * media-query change event so a rotate or window-drag re-resolves cleanly.
 */
export const MOBILE_QUERY = "(max-width: 767px)";

function initial(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(MOBILE_QUERY).matches;
}

export function useIsMobile(): boolean {
  const [v, setV] = useState<boolean>(initial);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mql = window.matchMedia(MOBILE_QUERY);
    const on = () => setV(mql.matches);
    on();
    // addEventListener("change") is supported on every iOS/Android browser we
    // care about; the older `addListener` is just an alias on legacy WebKit.
    mql.addEventListener?.("change", on);
    return () => mql.removeEventListener?.("change", on);
  }, []);
  return v;
}
