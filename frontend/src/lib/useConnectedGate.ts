import { useEffect, useRef, useState } from "react";
import { useGcs } from "../store/useGcs";

/**
 * Connected-gate for the video panels (C1 fix).
 *
 * The roster `connected` flag flips false on any momentary blip (the 5 s
 * `/api/vehicles` poll racing a telemetry gap, a backend restart, a link flap).
 * Unmounting the panel on that flip tears down the WebRTC `RTCPeerConnection`,
 * forcing a multi-second black feed while ICE renegotiates — exactly the "video
 * blips for a split second" regression.
 *
 * This hook decouples MOUNTING from VISIBILITY:
 *  - `mounted`     — true once the drone has EVER been connected, and stays true
 *                    thereafter so the `<video>`/WebRTC peer connection survives
 *                    transient `connected:false` flips (NEVER torn down on a
 *                    poll blip). Before the first-ever connect it's false, so we
 *                    don't open a peer connection for a drone that isn't there.
 *  - `offline`     — DEBOUNCED: true only after the drone has been continuously
 *                    `connected:false` for `graceMs` (~a few seconds). A single
 *                    poll blip never sets it. Drives an offline overlay + a
 *                    `hidden` visibility gate, NOT an unmount.
 *
 * So a transient flip = no unmount, no overlay; a sustained disconnect = overlay
 * (panel stays mounted, WebRTC stays alive for instant recovery on reconnect).
 */
export function useConnectedGate(vehicleId: string, graceMs = 4000) {
  const rosterConnected =
    useGcs((s) => s.vehicles.find((v) => v.id === vehicleId)?.connected) ?? false;
  // The roster `connected` flag is only trustworthy while the GCS WebSocket is up.
  // If the backend link drops (`socketOpen=false`), the roster/telemetry FREEZE at
  // their last values — a drone could read "connected" forever while we actually
  // know nothing. Treat a dropped GCS link as not-connected so the DEBOUNCED
  // `offline` overlay fires (truthful "reconnecting…") instead of showing a frozen
  // feed as live. The panel STAYS mounted (WebRTC survives) for instant recovery.
  const socketOpen = useGcs((s) => s.socketOpen);
  const connected = rosterConnected && socketOpen;

  // Latches true on the first-ever connect and never goes back to false — the
  // panel (and its WebRTC) stays mounted across every later flip.
  const [mounted, setMounted] = useState(connected);
  // Debounced sustained-disconnect flag.
  const [offline, setOffline] = useState(!connected);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (connected) {
      // Connected (or reconnected): mount immediately and clear any pending
      // "going offline" timer + the offline overlay.
      if (timer.current) {
        clearTimeout(timer.current);
        timer.current = null;
      }
      setMounted(true);
      setOffline(false);
      return;
    }
    // connected === false: DON'T act immediately — a blip clears before graceMs.
    // Only flip to offline after a sustained window of disconnection.
    if (timer.current) return; // already counting down
    timer.current = setTimeout(() => {
      timer.current = null;
      setOffline(true);
    }, graceMs);
    return () => {
      if (timer.current) {
        clearTimeout(timer.current);
        timer.current = null;
      }
    };
  }, [connected, graceMs]);

  return { mounted, offline, connected };
}
