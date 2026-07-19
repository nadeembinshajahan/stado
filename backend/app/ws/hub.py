from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

from ..logbus import logbus

log = logging.getLogger("gcs.hub")


def _audit_hub_event(message: dict[str, Any]) -> None:
    """Mirror every hub event into the structured audit log. Everything the
    frontend sees is logged in full EXCEPT high-rate telemetry, which is
    sampled per-vehicle (see logbus.log_telemetry). This single tap captures all
    agent ACTIONS (voice_command carries name/args/result/vehicle), command
    results (ack/statustext), mode/mission/flight events, low_battery, etc.
    Never raises into the publisher."""
    try:
        if not logbus.enabled:
            return
        mtype = message.get("type")
        vehicle = message.get("vehicle")
        if mtype == "telemetry":
            logbus.log_telemetry(vehicle, message.get("data") or {})
            return
        fields = {k: v for k, v in message.items() if k != "vehicle"}
        fields.setdefault("kind", mtype)
        logbus.log("hub", "out", vehicle, **fields)
    except Exception:
        pass


class Hub:
    """Fan-out of JSON messages to all connected WebSocket clients.

    Safe to publish from either the asyncio loop (`publish`) or a background
    thread (`publish_threadsafe`) — the MAVLink reader thread uses the latter.
    """

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)
        log.info("client connected (%d total)", len(self._clients))

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)
        log.info("client disconnected (%d total)", len(self._clients))

    async def publish(self, message: dict[str, Any]) -> None:
        # Audit-log the event regardless of whether any frontend is connected —
        # the record must be complete even with no UI attached. publish_threadsafe
        # routes through here too, so this single tap covers both entrypoints.
        _audit_hub_event(message)
        if not self._clients:
            return
        dead: list[WebSocket] = []
        for ws in list(self._clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    def publish_threadsafe(self, message: dict[str, Any]) -> None:
        """Schedule a publish from a non-asyncio thread."""
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.publish(message), self._loop)
        except RuntimeError:
            pass


hub = Hub()
