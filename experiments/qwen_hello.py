#!/usr/bin/env python3
"""Qwen Realtime hello-world — the go/no-go smoke test for STADO's voice bridge.

Opens a Qwen Realtime WebSocket (Alibaba Cloud Model Studio), sends a spoken
test utterance as PCM16@16kHz, and verifies we get back at least one of:

  1. streamed TEXT   (response.audio_transcript.delta / response.text.delta)
  2. a TOOL CALL     (response.function_call_arguments.done)
  3. streamed AUDIO  (response.audio.delta, PCM16@24kHz)

The session registers one tool (`get_fleet_status`) and the default utterance
is "What is the current fleet status?", so a healthy run exercises all three:
tool call -> tool result -> spoken answer.

Usage:
    export DASHSCOPE_API_KEY=sk-...
    # International (Singapore) accounts need the workspace-scoped endpoint:
    #   export QWEN_WS_URL="wss://<WorkspaceId>.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime"
    python3 experiments/qwen_hello.py                 # spoken question (needs macOS `say` or an --audio file)
    python3 experiments/qwen_hello.py --text "what is the fleet status?"   # text-only turn, still exercises tools+audio
    python3 experiments/qwen_hello.py --audio my_16k_mono.pcm

Requires: pip install websockets
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import websockets
except ImportError:
    sys.exit("pip install websockets  (>=12) and re-run")

MODEL = os.environ.get("QWEN_REALTIME_MODEL", "qwen3.5-omni-plus-realtime")
# Default to the classic international endpoint; Singapore workspace-scoped
# accounts must override via QWEN_WS_URL (see module docstring).
WS_URL = os.environ.get(
    "QWEN_WS_URL", "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
)
API_KEY = os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("QWEN_API_KEY")

UTTERANCE = "What is the current fleet status?"

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_fleet_status",
            "description": (
                "Get the live status of every drone in the fleet: connectivity, "
                "flight mode, armed state, altitude, battery."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]

FAKE_FLEET = {
    "overwatch": {"connected": True, "mode": "HOLD", "armed": False, "battery_pct": 93},
    "outrider": {"connected": True, "mode": "POSITION", "armed": False, "battery_pct": 88},
}


def synth_utterance_pcm() -> bytes:
    """Make a PCM16@16kHz mono sample of UTTERANCE using macOS `say`."""
    if not shutil.which("say") or not shutil.which("afconvert"):
        sys.exit(
            "No canned audio available: install nothing, just pass --audio <pcm16-16k-mono file>"
            " or --text, or run on macOS where `say`/`afconvert` exist."
        )
    with tempfile.TemporaryDirectory() as td:
        aiff = Path(td) / "u.aiff"
        wav = Path(td) / "u.wav"
        subprocess.run(["say", "-o", str(aiff), UTTERANCE], check=True)
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
            check=True,
        )
        raw = wav.read_bytes()
        # Strip the 44-byte canonical WAV header; find the 'data' chunk properly.
        idx = raw.find(b"data")
        return raw[idx + 8 :] if idx != -1 else raw[44:]


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", help="send a text turn instead of audio")
    ap.add_argument("--audio", help="path to raw PCM16@16kHz mono file to send")
    ap.add_argument("--voice", default=os.environ.get("QWEN_VOICE", "Ethan"))
    args = ap.parse_args()

    if not API_KEY:
        sys.exit("DASHSCOPE_API_KEY (or QWEN_API_KEY) is not set")

    url = f"{WS_URL}?model={MODEL}"
    headers = {"Authorization": f"Bearer {API_KEY}"}
    print(f"[hello] connecting {url}")
    t0 = time.monotonic()

    got: dict[str, bool] = {"text": False, "tool": False, "audio": False}
    audio_out = bytearray()
    transcript: list[str] = []

    try:
        try:
            _connect = websockets.connect(url, additional_headers=headers, max_size=None)
        except TypeError:  # websockets < 14 spells it `extra_headers`
            _connect = websockets.connect(url, extra_headers=headers, max_size=None)
        async with _connect as ws:
            print(f"[hello] connected in {time.monotonic()-t0:.2f}s")

            async def send(ev: dict) -> None:
                await ws.send(json.dumps(ev))

            await send({
                "type": "session.update",
                "session": {
                    "modalities": ["text", "audio"],
                    "voice": args.voice,
                    "input_audio_format": "pcm",
                    "output_audio_format": "pcm",
                    "instructions": (
                        "You are STADO, a terse drone ground-control voice agent. "
                        "When asked about the fleet, call get_fleet_status and report it."
                    ),
                    "tools": TOOLS,
                    "turn_detection": None,  # manual / push-to-talk
                },
            })

            if args.text:
                await send({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": args.text}],
                    },
                })
                await send({"type": "response.create"})
            else:
                pcm = Path(args.audio).read_bytes() if args.audio else synth_utterance_pcm()
                print(f"[hello] sending {len(pcm)} bytes of PCM16@16k ({len(pcm)/32000:.1f}s)")
                CHUNK = 3200  # 100 ms
                for i in range(0, len(pcm), CHUNK):
                    await send({
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm[i : i + CHUNK]).decode(),
                    })
                await send({"type": "input_audio_buffer.commit"})
                await send({"type": "response.create"})

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=deadline - time.monotonic())
                except (asyncio.TimeoutError, websockets.ConnectionClosed) as e:
                    print(f"[hello] stream ended: {e!r}")
                    break
                ev = json.loads(raw)
                et = ev.get("type", "?")

                if et == "error":
                    print(f"[hello] ERROR event: {json.dumps(ev.get('error', ev), indent=2)}")
                elif et == "response.audio.delta":
                    audio_out.extend(base64.b64decode(ev["delta"]))
                    if not got["audio"]:
                        print(f"[hello] <- first audio delta at +{time.monotonic()-t0:.2f}s")
                    got["audio"] = True
                elif et in ("response.audio_transcript.delta", "response.text.delta"):
                    transcript.append(ev.get("delta", ""))
                    got["text"] = True
                elif et == "response.function_call_arguments.done":
                    got["tool"] = True
                    name, call_id = ev.get("name"), ev.get("call_id")
                    fargs = ev.get("arguments")
                    print(f"[hello] <- TOOL CALL {name}({fargs}) call_id={call_id}")
                    await send({
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": json.dumps(FAKE_FLEET),
                        },
                    })
                    await send({"type": "response.create"})
                elif et == "response.done":
                    print(f"[hello] <- response.done at +{time.monotonic()-t0:.2f}s")
                    # If a tool call is still being answered, keep listening;
                    # otherwise we have our verdict.
                    if got["audio"] or (got["text"] and not got["tool"]):
                        break
                elif et in ("session.created", "session.updated"):
                    print(f"[hello] <- {et}")
                else:
                    print(f"[hello] <- {et}")
    except Exception as exc:  # noqa: BLE001
        print(f"[hello] connection failed: {exc!r}")
        print(
            "[hello] hints: 401/403 -> wrong key or wrong region endpoint "
            "(intl keys need the intl/Singapore URL; set QWEN_WS_URL). "
            "404 -> model name not enabled for this account/region."
        )
        return 2

    if audio_out:
        out = Path(__file__).with_name("reply_24k.pcm")
        out.write_bytes(bytes(audio_out))
        print(f"[hello] wrote {len(audio_out)} bytes -> {out}")
        print("        play: ffplay -f s16le -ar 24000 -ch_layout mono " + str(out))
    if transcript:
        print(f"[hello] transcript: {''.join(transcript)!r}")

    print(f"[hello] RESULT: text={got['text']} tool={got['tool']} audio={got['audio']}")
    ok = any(got.values())
    print("[hello] " + ("PASS — Qwen Realtime end-to-end working" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
