"""STADO's realtime voice session: Qwen Realtime (Alibaba Cloud Model Studio).

This module owns the wire protocol of the live voice loop — duplex audio +
tool-calling over Model Studio's native realtime WebSocket API. Everything the
drone commands hook into lives in `voice.py` and is shared with the REST
surface: the tool schema (`tool_declarations`), the dispatcher (`dispatch` —
including the Ready-for-Flight gate), the system prompt, and the alert/event
queues.

Wire protocol (per the Model Studio "Qwen-Omni-Realtime" docs):
  - connect: wss://…/api-ws/v1/realtime?model=qwen3.5-omni-plus-realtime
             with header `Authorization: Bearer $DASHSCOPE_API_KEY`
  - session.update: modalities/voice/pcm formats/instructions/tools/
             turn_detection (null = manual push-to-talk — matches our PTT UX)
  - mic up:  input_audio_buffer.append (base64 PCM16@16kHz mono)
             → input_audio_buffer.commit → response.create on PTT release
  - audio down: response.audio.delta (base64 PCM16@24kHz mono)
  - tools:   response.function_call_arguments.done →
             conversation.item.create{function_call_output} → response.create

The browser streams PCM16@16k up / PCM16@24k down — exactly Qwen Realtime's
audio contract, so the frontend needs no transcoding.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid

from fastapi import WebSocket, WebSocketDisconnect

log = logging.getLogger("gcs.voice.qwen")

# Classic international endpoint. Singapore workspace-scoped accounts override:
#   QWEN_WS_URL=wss://<WorkspaceId>.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime
DEFAULT_WS_URL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
DEFAULT_MODEL = "qwen3.5-omni-plus-realtime"
DEFAULT_VOICE = "Ethan"


def _cfg() -> dict:
    from .config import settings

    return {
        "api_key": os.environ.get("DASHSCOPE_API_KEY")
        or os.environ.get("QWEN_API_KEY")
        or settings.dashscope_api_key,
        "url": os.environ.get("QWEN_WS_URL", DEFAULT_WS_URL),
        "model": os.environ.get("QWEN_REALTIME_MODEL", DEFAULT_MODEL),
        "voice": os.environ.get("QWEN_VOICE", DEFAULT_VOICE),
    }


def qwen_tools() -> list[dict]:
    """The full STADO tool surface in Qwen Realtime's `session.update` format.

    voice.py declares the tools once (as OpenAI-style function dicts) for the
    whole GCS — the realtime session just wraps them. Zero drift between the
    voice surface and the dispatcher."""
    from .voice import tool_declarations

    return [{"type": "function", "function": fn} for fn in tool_declarations()]


async def _connect(url: str, headers: dict):
    """Open the upstream WS across websockets-lib versions (>=12)."""
    import websockets

    try:
        return await websockets.connect(url, additional_headers=headers, max_size=None)
    except TypeError:  # websockets < 14 spells it `extra_headers`
        return await websockets.connect(url, extra_headers=headers, max_size=None)


async def qwen_voice_session(ws: WebSocket, manual_vad: bool = True) -> None:
    """Bridge an (already accepted) browser WebSocket to a Qwen Realtime session.

    Three concurrent tasks per session:
      browser_to_qwen   mic PCM + PTT activity markers → audio buffers upstream
      qwen_to_browser   audio out, transcripts, tool calls → dispatch()
      alert_pump        backend-raised alerts/events spoken proactively by STADO
    """
    # Late imports — voice.py imports this module lazily from inside voice_ws,
    # so by now `app.voice` is fully initialised and safe to import from.
    from .voice import (
        SYSTEM_PROMPT,
        _fleet_status_line,
        _pending_voice_alerts,
        _pending_voice_events,
        _resolve_vehicle_id,
        dispatch,
    )
    from . import pois, regions
    from .mavlink.registry import registry
    from .ws.hub import hub

    cfg = _cfg()
    if not cfg["api_key"]:
        await ws.send_json({"type": "error", "message": "DASHSCOPE_API_KEY not set"})
        await ws.close()
        return

    # Per-session context: the operator's live markers + ground-truth fleet
    # connectivity are composed into the system instruction at connect time.
    poi_ctx = pois.context_line()
    region_ctx = regions.context_line()
    # A realtime voice model under a terse radio-operator prompt can drift into
    # NARRATING an action ("Overwatch proceeding to point B and initiating
    # orbit") without actually calling the tool. Reinforce hard: every operator
    # command is a tool call, compound commands are one tool call per action,
    # no narration of unexecuted work. Also spelled out for compound
    # "goto + orbit" cases.
    QWEN_TOOL_USE_TAIL = (
        "\n\nCRITICAL — TOOL USE:\n"
        "- When the operator gives ANY command (takeoff, land, goto, orbit, move, "
        "turn, set speed, arm, disarm, follow, survey, etc.), you MUST call the "
        "matching tool in the SAME turn. Do NOT reply with only text that "
        "describes what you would do — always CALL the tool.\n"
        "- If a single utterance contains multiple actions (e.g. 'both drones go "
        "to point B AND start orbiting', 'take off and then follow the target'), "
        "emit ONE tool call per action per vehicle — all in the same turn. For "
        "'both go to B and orbit': call goto_point twice AND orbit_point twice, "
        "not just the gotos.\n"
        "- Never announce that you 'will initiate' or 'will begin' an action "
        "later — issue the tool call NOW, then acknowledge tersely. The "
        "completion [SYSTEM] events tell you when it's actually done.\n"
        "- If you find yourself about to reply with a plan but no tool_calls, "
        "STOP and emit the tool_calls instead."
    )
    instruction = (
        SYSTEM_PROMPT
        + QWEN_TOOL_USE_TAIL
        + (("\n" + poi_ctx) if poi_ctx else "")
        + (("\n" + region_ctx) if region_ctx else "")
        + _fleet_status_line()
    )

    url = f"{cfg['url']}?model={cfg['model']}"
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}
    log.info("qwen realtime: connecting %s (voice=%s)", url, cfg["voice"])

    try:
        upstream = await _connect(url, headers)
    except Exception as exc:  # noqa: BLE001
        log.exception("qwen realtime: connect failed")
        await ws.send_json({"type": "error", "message": f"Qwen connect failed: {exc}"})
        await ws.close()
        return

    # Serialize ALL upstream sends — audio chunks, tool results, and alert
    # injections race from three tasks; concurrent writes corrupt the WS.
    send_lock = asyncio.Lock()

    async def send(ev: dict) -> None:
        async with send_lock:
            await upstream.send(json.dumps(ev))

    try:
        await send({
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "voice": cfg["voice"],
                "input_audio_format": "pcm",   # PCM16 @16 kHz mono (matches browser)
                "output_audio_format": "pcm",  # PCM16 @24 kHz mono (matches browser)
                "instructions": instruction,
                "tools": qwen_tools(),
                # PTT (manual): the browser marks utterance boundaries, we commit
                # on release. Open-mic: let Qwen's semantic VAD segment turns —
                # it also gives us server-side barge-in (semantic interruption).
                "turn_detection": None if manual_vad else {"type": "semantic_vad"},
            },
        })
        await ws.send_json({"type": "ready", "model": cfg["model"]})

        # True while the model owes us a reply — guards duplicate response.create.
        # Cleared only when the server emits `response.done` (source of truth) or
        # when we successfully send `response.cancel`. Never speculatively cleared:
        # doing so races the server and produces
        # "Conversation already has an active response" errors on the next create.
        response_active = asyncio.Event()
        # True if the current response contained at least one tool call — after
        # response.done fires for a tool-carrying response, we auto-create the
        # follow-up so the model actually speaks the tool outcome. (Qwen does not
        # auto-continue after a tool.)
        response_has_tool = False
        # Bytes appended to the audio buffer since the last clear/commit. Guards
        # activity_end from committing an empty buffer (Qwen's minimum is roughly
        # one 100 ms frame, ~3200 bytes at PCM16@16kHz mono; a tap-and-release
        # or a muted mic can produce zero bytes → 'buffer too small' error).
        audio_bytes_since_clear = 0
        # Only commit if we have at least this many bytes buffered (~50 ms).
        MIN_COMMIT_BYTES = 1600

        async def request_response() -> None:
            if not response_active.is_set():
                response_active.set()
                await send({"type": "response.create"})

        async def browser_to_qwen() -> None:
            nonlocal audio_bytes_since_clear
            try:
                while True:
                    msg = await ws.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    if "bytes" in msg and msg["bytes"] is not None:
                        chunk = msg["bytes"]
                        audio_bytes_since_clear += len(chunk)
                        await send({
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk).decode(),
                        })
                    elif "text" in msg and msg["text"]:
                        text = msg["text"]
                        if text == '{"type":"activity_start"}':
                            if manual_vad:
                                # Barge-in: kill any reply mid-stream, start clean.
                                if response_active.is_set():
                                    await send({"type": "response.cancel"})
                                    response_active.clear()
                                await send({"type": "input_audio_buffer.clear"})
                                audio_bytes_since_clear = 0
                        elif text == '{"type":"activity_end"}':
                            if manual_vad:
                                if audio_bytes_since_clear >= MIN_COMMIT_BYTES:
                                    await send({"type": "input_audio_buffer.commit"})
                                    audio_bytes_since_clear = 0
                                    # request_response is a no-op if a response is
                                    # still active — that's fine, response.done
                                    # will fire it after the current one closes.
                                    await request_response()
                                else:
                                    # Too-short PTT — silently drop instead of
                                    # letting Qwen error 'buffer too small'.
                                    log.info(
                                        "qwen realtime: dropping short PTT "
                                        "(%d bytes < %d minimum)",
                                        audio_bytes_since_clear, MIN_COMMIT_BYTES,
                                    )
                                    await send({"type": "input_audio_buffer.clear"})
                                    audio_bytes_since_clear = 0
                        else:
                            # Typed text turn (debug console path).
                            await send({
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": text}],
                                },
                            })
                            response_active.clear()
                            await request_response()
            except (WebSocketDisconnect, RuntimeError):
                pass

        async def qwen_to_browser() -> None:
            nonlocal response_has_tool
            import websockets

            while True:
                try:
                    raw = await upstream.recv()
                except websockets.ConnectionClosed:
                    log.info("qwen realtime: upstream closed")
                    break
                ev = json.loads(raw)
                et = ev.get("type", "")

                if et == "response.audio.delta":
                    await ws.send_bytes(base64.b64decode(ev["delta"]))
                elif et == "response.audio_transcript.delta":
                    if ev.get("delta"):
                        await ws.send_json({"type": "said", "text": ev["delta"]})
                elif et == "conversation.item.input_audio_transcription.completed":
                    if ev.get("transcript"):
                        await ws.send_json({"type": "heard", "text": ev["transcript"]})
                elif et == "response.function_call_arguments.done":
                    name = ev.get("name") or ""
                    try:
                        args = json.loads(ev.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    await ws.send_json({"type": "tool", "name": name, "args": args})
                    result = await dispatch(name, args)
                    await send({
                        "type": "conversation.item.create",
                        "item": {
                            "id": "item_" + uuid.uuid4().hex[:24],
                            "type": "function_call_output",
                            "call_id": ev.get("call_id"),
                            "output": json.dumps(result),
                        },
                    })
                    # Flag that this response carried a tool — after the server's
                    # response.done, we auto-create the follow-up so the model
                    # speaks the outcome. Do NOT speculatively clear response_active
                    # here (races the server → "already has an active response").
                    response_has_tool = True
                    await ws.send_json({"type": "tool_result", "name": name, "result": result})
                    # Attribute the action to the right vehicle for the flight
                    # recorder — explicit `vehicle` arg, else the active drone.
                    spoken = args.get("vehicle")
                    action_vid = (
                        _resolve_vehicle_id(str(spoken)) if spoken else None
                    ) or registry.active_id()
                    hub.publish_threadsafe({
                        "type": "voice_command", "name": name,
                        "args": args, "result": result, "vehicle": action_vid,
                    })
                elif et == "response.done":
                    response_active.clear()
                    # If this response carried a tool call, the model owes us a
                    # spoken outcome — kick off the follow-up response now.
                    if response_has_tool:
                        response_has_tool = False
                        await request_response()
                elif et == "error":
                    err = ev.get("error", ev)
                    log.warning("qwen realtime error event: %s", err)
                    # Benign races (e.g. cancel with no active response) stay
                    # server-side; still surface to the console for debugging.
                    try:
                        await ws.send_json({
                            "type": "error",
                            "message": str(err.get("message", err)) if isinstance(err, dict) else str(err),
                        })
                    except Exception:  # noqa: BLE001
                        pass

        async def alert_pump() -> None:
            """Speak backend-raised alerts + action completions (queued by the
            telemetry watchdog, completion tracker, etc. in voice.py)."""

            async def inject(text: str) -> None:
                try:
                    await send({
                        "type": "conversation.item.create",
                        "item": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": text}],
                        },
                    })
                    # If the model is mid-reply, wait for it to finish (the
                    # item is already in the conversation), then ask it to
                    # speak. Give up after ~10 s — the item still gets picked
                    # up on the next turn.
                    for _ in range(50):
                        if not response_active.is_set():
                            break
                        await asyncio.sleep(0.2)
                    await request_response()
                except Exception:  # noqa: BLE001
                    pass

            while True:
                await asyncio.sleep(0.4)
                if _pending_voice_alerts:
                    alerts, _pending_voice_alerts[:] = list(_pending_voice_alerts), []
                    for a in alerts:
                        await inject(f"[SYSTEM ALERT] {a}")
                if _pending_voice_events:
                    events, _pending_voice_events[:] = list(_pending_voice_events), []
                    await inject("[SYSTEM] " + " ".join(events))

        b2q = asyncio.create_task(browser_to_qwen())
        q2b = asyncio.create_task(qwen_to_browser())
        pump = asyncio.create_task(alert_pump())
        # First finisher (browser hangup or upstream close) tears the rest down.
        done, pending = await asyncio.wait(
            {b2q, q2b, pump}, return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                raise exc
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        log.exception("qwen voice session error")
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            await upstream.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
