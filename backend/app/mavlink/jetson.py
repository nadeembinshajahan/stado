"""Outrider Jetson onboard-tracking control channel — STUB / SEAM.

WHY THIS EXISTS
---------------
Overwatch (SIYI/MAVLink) is fully working and untouched. Outrider is a PX4
quad with a Jetson onboard running PX4's uXRCE-DDS bridge plus the
``stado_jetson`` stack (see /Users/nadeem/sfa/jetson_server). Two facts drive
the design:

1. PX4 telemetry + commands reach this GCS as plain MAVLink-over-WiFi. The GCS
   is pure pymavlink, so Outrider's link is just another `MavlinkLink` — point
   `OUTRIDER_CONNECTION` at the Jetson's IP:port. No DDS/ROS2 in this backend.

2. Target TRACKING for Outrider runs ONBOARD the Jetson (low-latency follow
   loop), NOT here. The only thing the GCS contributes is the INITIAL track
   region, produced by the existing VLM grounding (`vision/grounding.py`).
   So the GCS needs a tiny side-channel to the Jetson to: (a) push a seed box +
   description, (b) clear/stop the track, (c) read track status. Video comes
   back over its own URL (go2rtc/MJPEG), not through this channel.

This module is that side-channel, and nothing more. It is:
  * OPT-IN — disabled unless `settings.outrider_jetson_host` is set.
  * NON-DESTRUCTIVE — importing or calling it never touches the Overwatch path,
    never adds a hard dependency (httpx is already a backend dep), and degrades
    to a clear "not configured" result.

FLAG FOR USER (real decisions, deliberately NOT hard-coded):
  * Transport: this stub assumes a small HTTP control channel on the Jetson
    (POST /track, POST /untrack, GET /status). The Jetson stado_jetson WS
    server already speaks a JSON `track_object {bbox,...}` / `untrack_object`
    protocol over WebSocket (ws_server.py) — we could instead reuse that WS.
    HTTP is chosen here only because it is the smallest stateless seam; switch
    to WS by reimplementing the three methods. Either way the GCS-facing API
    below stays identical.
  * Coordinate convention: bbox is normalized [x0, y0, x1, y1] in 0..1, which
    is exactly what `grounding.resolve_target()` returns and what
    stado_vision's `track_roi` expects (it converts norm→pixels onboard).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("gcs.jetson")


class JetsonTrackClient:
    """Thin client for the Outrider Jetson's onboard-tracking control channel.

    Stateless and lazily-configured: every call re-reads `settings`, so flipping
    `OUTRIDER_JETSON_HOST` at runtime (or in tests) takes effect immediately and
    an unconfigured client is a guaranteed no-op.
    """

    @property
    def configured(self) -> bool:
        return bool(settings.outrider_jetson_host)

    def _base_url(self) -> str:
        host = settings.outrider_jetson_host
        port = settings.outrider_jetson_track_port
        return f"http://{host}:{port}"

    async def seed_track(
        self, box_norm: list[float], description: str, *, track_id: str = "gcs",
        src_w: int | None = None, src_h: int | None = None,
    ) -> dict[str, Any]:
        """Send the VLM-seeded initial region to the Jetson to BEGIN onboard
        tracking. `box_norm` is normalized [x0, y0, x1, y1] in 0..1.

        ASPECT CONTRACT: a normalized box is only valid if the frame the VLM saw
        and the Jetson's tracker frame share the same aspect/crop. The GCS grabs
        the Jetson's `/rgb` stream (same frame stado_vision tracks), so they match
        by construction — and `src_w`/`src_h` (the exact pixels of the seed frame)
        are sent so the Jetson endpoint can ASSERT
        `src_w/src_h ≈ rgb_frame.cols/rgb_frame.rows` and reject a mismatched seed
        rather than track the wrong, stretched region.

        Returns {"ok": bool, ...}. Never raises — a transport error becomes a
        structured error result so the caller (voice/api) can report it.
        """
        if not self.configured:
            return {"ok": False, "error": "outrider jetson not configured"}
        if not (isinstance(box_norm, (list, tuple)) and len(box_norm) == 4):
            return {"ok": False, "error": "box must be [x0,y0,x1,y1] normalized"}
        if not all(0.0 <= float(v) <= 1.0 for v in box_norm):
            return {"ok": False, "error": "box must be normalized 0..1"}
        payload: dict[str, Any] = {
            "track_id": track_id,
            "bbox": [float(v) for v in box_norm],
            "description": description,
        }
        # Aspect-validation hint for the Jetson (see ASPECT CONTRACT above).
        if src_w and src_h:
            payload["src_w"], payload["src_h"] = int(src_w), int(src_h)
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.post(f"{self._base_url()}/track", json=payload)
                r.raise_for_status()
                data = r.json() if r.content else {}
        except Exception as exc:  # noqa: BLE001
            log.warning("jetson seed_track failed: %s", exc)
            return {"ok": False, "error": f"jetson unreachable: {exc}"}
        return {"ok": True, "track_id": track_id, "jetson": data}

    async def stop_track(self, *, track_id: str = "gcs") -> dict[str, Any]:
        """Tell the Jetson to drop the track and stop the onboard follow loop."""
        if not self.configured:
            return {"ok": False, "error": "outrider jetson not configured"}
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.post(
                    f"{self._base_url()}/untrack", json={"track_id": track_id}
                )
                r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("jetson stop_track failed: %s", exc)
            return {"ok": False, "error": f"jetson unreachable: {exc}"}
        return {"ok": True}

    async def status(self) -> dict[str, Any]:
        """Read onboard track/follow status from the Jetson (best-effort)."""
        if not self.configured:
            return {"configured": False}
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{self._base_url()}/status")
                r.raise_for_status()
                return {"configured": True, "ok": True, **(r.json() if r.content else {})}
        except Exception as exc:  # noqa: BLE001
            return {"configured": True, "ok": False, "error": str(exc)}


# Module-level singleton — cheap and stateless; safe to import anywhere.
jetson_track = JetsonTrackClient()
