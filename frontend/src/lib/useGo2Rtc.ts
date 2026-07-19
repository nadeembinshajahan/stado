import { useEffect, useState } from "react";

export const GO2RTC = import.meta.env.VITE_GO2RTC_URL ?? "http://127.0.0.1:1984";

export type Go2RtcState = "idle" | "connecting" | "live" | "error";

/**
 * Connect to a go2rtc stream by name over WebRTC (recvonly) and pipe it into a
 * <video>. Shared by the main FPV feed ("drone") and the operator-defined
 * second feed ("feed2"). Reconnects whenever `stream` or `enabled` changes.
 */
export function useGo2RtcWebRTC(
  videoRef: React.RefObject<HTMLVideoElement>,
  stream: string,
  enabled: boolean,
) {
  const [state, setState] = useState<Go2RtcState>("idle");
  useEffect(() => {
    if (!enabled || !stream) {
      setState("idle");
      return;
    }
    let pc: RTCPeerConnection | null = null;
    let ws: WebSocket | null = null;
    let cancelled = false;
    setState("connecting");
    // NO external STUN. go2rtc advertises a localhost host candidate
    // (127.0.0.1:8555, see relay/go2rtc.yaml) and the browser runs on the same
    // Mac, so the media path is purely local — host candidates connect instantly.
    // A configured-but-unreachable STUN server (we fly offline / no internet in
    // the field) makes ICE gathering stall, so `ontrack` never fires and the feed
    // hangs on "connecting…". Empty iceServers = host-only, offline-safe.
    pc = new RTCPeerConnection({ iceServers: [] });
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });
    pc.ontrack = (e) => {
      if (videoRef.current) videoRef.current.srcObject = e.streams[0];
      setState("live");
    };
    const base = GO2RTC.replace(/^http/, "ws");
    ws = new WebSocket(`${base}/api/ws?src=${encodeURIComponent(stream)}`);
    ws.onopen = async () => {
      const offer = await pc!.createOffer();
      await pc!.setLocalDescription(offer);
      ws!.send(JSON.stringify({ type: "webrtc/offer", value: pc!.localDescription!.sdp }));
    };
    pc.onicecandidate = (e) => {
      if (e.candidate && ws?.readyState === 1)
        ws.send(JSON.stringify({ type: "webrtc/candidate", value: e.candidate.candidate }));
    };
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.type === "webrtc/answer") pc!.setRemoteDescription({ type: "answer", sdp: m.value });
      else if (m.type === "webrtc/candidate")
        pc!.addIceCandidate({ candidate: m.value, sdpMid: "0" }).catch(() => {});
    };
    ws.onerror = () => !cancelled && setState("error");
    return () => {
      cancelled = true;
      ws?.close();
      pc?.close();
    };
  }, [enabled, stream, videoRef]);
  return state;
}
