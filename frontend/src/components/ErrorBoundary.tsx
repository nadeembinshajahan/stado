import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  label?: string;
  /** When true the fallback fills its positioned container (`absolute inset-0`),
   *  for a full-region boundary like the Map. DEFAULT false → a small CONTAINED
   *  card that occupies only its own slot, so a throw in one panel (a HUD, the
   *  command bar, the status bar, a video feed) degrades JUST that panel and
   *  NEVER blanks the whole cockpit or covers the emergency controls. */
  fullscreen?: boolean;
}
interface State {
  error: Error | null;
}

/** Contains rendering failures so the rest of the GCS survives. By default the
 *  fallback is a small inline card (contained to the failing region); set
 *  `fullscreen` only for a boundary that genuinely owns the whole viewport (Map). */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error) {
    console.error(`[${this.props.label ?? "ErrorBoundary"}]`, error);
  }

  render() {
    if (this.state.error) {
      const card = (
        <div className="glass max-w-md rounded-xl p-3 text-center">
          <p className="text-danger font-semibold mb-1 text-sm">
            {this.props.label ?? "Component"} failed
          </p>
          <p className="text-xs text-slate-400 font-mono break-words">{this.state.error.message}</p>
        </div>
      );
      // Full-region fallback (Map): cover the positioned container. Otherwise a
      // CONTAINED card so a single-panel failure never blanks the cockpit.
      if (this.props.fullscreen) {
        return (
          <div className="absolute inset-0 flex items-center justify-center bg-[radial-gradient(circle_at_30%_20%,#0b1322,#05070b)]">
            {card}
          </div>
        );
      }
      return card;
    }
    return this.props.children;
  }
}
