"""End-to-end voice-bridge test against a FAKE Qwen Realtime server.

Spins up a local WebSocket server that speaks the Model Studio realtime
protocol (session.update in; audio deltas, transcripts, and a scripted
function call out), points QWEN_WS_URL at it, and drives the real
/ws/voice endpoint with FastAPI's TestClient. Verifies the full loop:

  browser PCM + PTT markers → append/commit/response.create upstream
  upstream tool call        → dispatch() → function_call_output + response.create
  upstream audio delta      → binary PCM to the browser
  upstream transcripts      → heard/said JSON to the browser

No DASHSCOPE_API_KEY or network access needed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading

import pytest

websockets = pytest.importorskip("websockets")


RECORDED: dict = {"events": [], "tool_output": None}


async def _fake_qwen(ws):
    """Minimal Qwen Realtime impersonator for one session."""
    await ws.send(json.dumps({"type": "session.created", "session": {"id": "sess_fake"}}))
    committed = False
    async for raw in ws:
        ev = json.loads(raw)
        RECORDED["events"].append(ev["type"])
        if ev["type"] == "input_audio_buffer.commit":
            committed = True
        elif ev["type"] == "response.create" and committed:
            committed = False
            # Scripted turn: transcription, then a tool call.
            await ws.send(json.dumps({
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "arm the drone",
            }))
            await ws.send(json.dumps({
                "type": "response.function_call_arguments.done",
                "name": "get_status",
                "call_id": "call_fake123",
                "arguments": "{}",
            }))
        elif ev["type"] == "conversation.item.create" and ev.get("item", {}).get("type") == "function_call_output":
            RECORDED["tool_output"] = ev["item"]
            # Follow-up response.create arrives next; answer with audio + text.
            await ws.send(json.dumps({
                "type": "response.audio_transcript.delta",
                "delta": "Status reported.",
            }))
            await ws.send(json.dumps({
                "type": "response.audio.delta",
                "delta": base64.b64encode(b"\x01\x02" * 240).decode(),
            }))
            await ws.send(json.dumps({"type": "response.done"}))


class FakeQwenServer(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.port: int | None = None
        self._ready = threading.Event()

    def run(self):
        async def main():
            async with websockets.serve(_fake_qwen, "127.0.0.1", 0) as server:
                self.port = server.sockets[0].getsockname()[1]
                self._ready.set()
                await asyncio.Future()

        asyncio.run(main())

    def wait_ready(self):
        assert self._ready.wait(5), "fake qwen server failed to start"


@pytest.fixture()
def fake_qwen(monkeypatch):
    srv = FakeQwenServer()
    srv.start()
    srv.wait_ready()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-fake")
    monkeypatch.setenv("QWEN_WS_URL", f"ws://127.0.0.1:{srv.port}/api-ws/v1/realtime")
    RECORDED["events"].clear()
    RECORDED["tool_output"] = None
    return srv


def test_voice_ws_bridges_to_qwen(fake_qwen):
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client:
        with client.websocket_connect("/ws/voice") as ws:
            ready = ws.receive_json()
            assert ready["type"] == "ready"
            assert "qwen" in ready["model"]

            # PTT press → 100 ms of mic PCM → PTT release.
            ws.send_text('{"type":"activity_start"}')
            ws.send_bytes(b"\x00\x01" * 1600)
            ws.send_text('{"type":"activity_end"}')

            got: dict[str, object] = {}
            for _ in range(8):
                msg = ws.receive()
                if "text" in msg:
                    j = json.loads(msg["text"])
                    got[j["type"]] = j
                    if j["type"] == "tool_result":
                        break
                elif "bytes" in msg:
                    got["audio"] = msg["bytes"]

            # Upstream got the manual-VAD sequence.
            assert "input_audio_buffer.append" in RECORDED["events"]
            assert "input_audio_buffer.commit" in RECORDED["events"]
            assert "response.create" in RECORDED["events"]
            # session.update carried the tool schema.
            assert RECORDED["events"][0] == "session.update"

            # The scripted tool call round-tripped through dispatch().
            assert got["heard"]["text"] == "arm the drone"
            assert got["tool"]["name"] == "get_status"
            assert got["tool_result"]["name"] == "get_status"
            out = RECORDED["tool_output"]
            assert out is not None and out["call_id"] == "call_fake123"
            json.loads(out["output"])  # dispatch result is JSON

            # Reply audio + transcript reached the browser.
            for _ in range(4):
                if "audio" in got and "said" in got:
                    break
                msg = ws.receive()
                if "text" in msg:
                    j = json.loads(msg["text"])
                    got[j["type"]] = j
                elif "bytes" in msg:
                    got["audio"] = msg["bytes"]
            assert got["said"]["text"] == "Status reported."
            assert isinstance(got.get("audio"), bytes) and len(got["audio"]) == 480
