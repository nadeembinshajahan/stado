"""HTTP routes for inspecting / replaying the structured audit log.

Own APIRouter (mounted by main.py) — does NOT touch api.py. Reads from the
logbus in-memory ring buffer so the endpoints are cheap and never block on disk.

Endpoints:
  GET /api/logs        — recent entries from the ring buffer, filterable.
  GET /api/logs/stream — Server-Sent Events tail (new entries as they arrive),
                         with a one-shot polled fallback (?poll=1).
  GET /api/logs/info   — log file path / rotation / status metadata.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from .api import require_token
from .logbus import logbus

log = logging.getLogger("gcs.logs_api")

# C1 AUTH: the audit log leaks the full command/telemetry trail, so gate the whole
# logs router behind the shared token (no-op when api_token is unset). The SSE
# stream additionally accepts ?token= since EventSource can't set headers.
router = APIRouter(prefix="/api/logs", dependencies=[Depends(require_token)])


@router.get("")
async def get_logs(
    limit: int = Query(200, ge=1, le=2000),
    since: int | None = Query(None, description="return entries with seq > since"),
    category: str | None = Query(None),
    vehicle: str | None = Query(None),
    direction: str | None = Query(None, alias="dir"),
) -> dict[str, Any]:
    """Return recent audit-log entries (oldest→newest), filterable.

    Use `since` (the `seq` of the last entry you saw) for incremental polling.
    """
    entries = logbus.recent(
        limit=limit,
        since=since,
        category=category,
        vehicle=vehicle,
        direction=direction,
    )
    return {
        "enabled": logbus.enabled,
        "count": len(entries),
        "last_seq": logbus.last_seq(),
        "entries": entries,
    }


@router.get("/info")
async def get_info() -> dict[str, Any]:
    """Log status: whether enabled, the current on-disk file, last seq."""
    return {
        "enabled": logbus.enabled,
        "current_path": logbus.current_path,
        "last_seq": logbus.last_seq(),
    }


@router.get("/stream")
async def stream_logs(
    category: str | None = Query(None),
    vehicle: str | None = Query(None),
    direction: str | None = Query(None, alias="dir"),
    token: str | None = Query(None, description="shared token (EventSource can't set headers)"),
    poll: int = Query(0, description="if 1, return a single JSON batch instead of SSE"),
) -> Any:
    """Tail new audit entries.

    Default: Server-Sent Events — one `data:` line per entry as it arrives.
    `?poll=1`: return the latest batch once (simple, robust polling fallback).

    Auth (when api_token is set) is enforced by the router-level require_token,
    which accepts the token via X-API-Token OR the ?token= query param (EventSource
    can't set headers) — hence the declared `token` param above.
    """
    if poll:
        entries = logbus.recent(
            limit=200, category=category, vehicle=vehicle, direction=direction
        )
        return {"last_seq": logbus.last_seq(), "entries": entries}

    async def _gen():
        cursor = logbus.last_seq()
        # Prime the cursor a little behind so the client gets immediate context.
        cursor = max(0, cursor - 1)
        try:
            while True:
                new = logbus.recent(
                    limit=2000,
                    since=cursor,
                    category=category,
                    vehicle=vehicle,
                    direction=direction,
                )
                for e in new:
                    cursor = max(cursor, int(e.get("seq", cursor)))
                    yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                # Heartbeat comment keeps proxies/clients from timing out.
                if not new:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:  # client disconnected
            return

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
