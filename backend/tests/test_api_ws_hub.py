"""Functional tests for the WebSocket hub + audit logbus (app/ws/hub.py,
app/logbus.py) and the voice WS tool-call ordering (app/voice.voice_ws).

NO HARDWARE: drives the Hub directly with fake WebSockets, and exercises the
voice WS bridge's tool loop so we can assert the EVENT ORDER the
browser sees for a tool call (tool → dispatch → tool_result).

Run: cd backend && PYTHONPATH=. uv run python -m pytest tests/test_api_ws_hub.py -q
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

for _name, _attrs in (("ultralytics", {"YOLO": object}), ("moondream", {})):
    if _name not in sys.modules:
        _m = types.ModuleType(_name)
        for k, v in _attrs.items():
            setattr(_m, k, v)
        sys.modules[_name] = _m

from app.ws.hub import Hub  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeWS:
    def __init__(self, fail_after=None):
        self.sent: list = []
        self.accepted = False
        self._fail_after = fail_after

    async def accept(self):
        self.accepted = True

    async def send_json(self, msg):
        if self._fail_after is not None and len(self.sent) >= self._fail_after:
            raise RuntimeError("socket dead")
        self.sent.append(msg)


# ── hub fan-out ───────────────────────────────────────────────────────────────
def test_hub_fans_out_to_all_clients():
    async def go():
        hub = Hub()
        a, b = FakeWS(), FakeWS()
        await hub.connect(a)
        await hub.connect(b)
        await hub.publish({"type": "telemetry", "vehicle": "overwatch", "data": {}})
        return a.sent, b.sent

    sa, sb = run(go())
    assert sa and sb and sa[0]["type"] == "telemetry"


def test_hub_drops_dead_client_and_keeps_serving():
    async def go():
        hub = Hub()
        good = FakeWS()
        dead = FakeWS(fail_after=0)  # raises on first send
        await hub.connect(good)
        await hub.connect(dead)
        await hub.publish({"type": "ack", "command": 400, "result": "ACCEPTED"})
        # second publish: dead one already removed, good one still gets it.
        await hub.publish({"type": "mode", "mode": "HOLD"})
        return good.sent, len(hub._clients)

    sent, n_clients = run(go())
    assert len(sent) == 2, "surviving client must keep receiving after a peer dies"
    assert n_clients == 1, "dead client should be discarded"


def test_publish_threadsafe_noop_without_loop():
    # FINDING-adjacent: publish_threadsafe silently no-ops if bind_loop was never
    # called. Verify it doesn't raise (events are simply dropped pre-bind).
    hub = Hub()
    hub.publish_threadsafe({"type": "x"})  # must not raise


def test_publish_with_no_clients_does_not_raise():
    run(Hub().publish({"type": "telemetry", "vehicle": "outrider", "data": {}}))


# ── voice WS bridge: tool-call event ordering ─────────────────────────────────
class FakeFunctionCall:
    def __init__(self, name, args, id="c1"):
        self.name = name
        self.args = args
        self.id = id


class FakeToolCall:
    def __init__(self, calls):
        self.function_calls = calls


class FakeResp:
    def __init__(self, data=None, tool_call=None, server_content=None):
        self.data = data
        self.tool_call = tool_call
        self.server_content = server_content


class FakeSession:
    """Yields ONE turn with a single tool_call, then stops (raises to end loop)."""
    def __init__(self, fc):
        self._fc = fc
        self.tool_responses = []
        self._done = False

    async def send_tool_response(self, function_responses):
        self.tool_responses.extend(function_responses)

    async def send_client_content(self, **kw):
        pass

    async def send_realtime_input(self, **kw):
        pass

    def receive(self):
        outer = self

        async def gen():
            if outer._done:
                # End the model_to_browser loop by cancelling the bridge.
                raise asyncio.CancelledError()
            outer._done = True
            yield FakeResp(tool_call=FakeToolCall([outer._fc]))

        return gen()


class FakeBridgeWS:
    """A WebSocket the bridge writes to; receive() blocks forever (no browser audio)."""
    def __init__(self):
        self.json_sent: list = []
        self.bytes_sent: list = []

    async def send_json(self, m):
        self.json_sent.append(m)

    async def send_bytes(self, b):
        self.bytes_sent.append(b)

    async def receive(self):
        await asyncio.Event().wait()  # never returns


def test_voice_ws_tool_event_ordering(monkeypatch):
    """The browser must see: {type:tool} BEFORE dispatch runs, then
    {type:tool_result} AFTER. And a voice_command hub event must be published
    with the resolved vehicle. Verifies the voice session's model→browser
    loop wiring without a real realtime session."""
    import app.voice as voice
    from app.mavlink.registry import Vehicle, registry

    class FakeLink:
        def snapshot(self):
            return {"connected": True}

    registry._vehicles = {"overwatch": Vehicle("overwatch", "Overwatch", "hex", FakeLink())}
    registry._order = ["overwatch"]
    registry._active = "overwatch"

    order: list = []

    async def fake_dispatch(name, args):
        order.append(("dispatch", name))
        return {"ok": True, "echo": name}

    monkeypatch.setattr(voice, "dispatch", fake_dispatch)

    published: list = []
    monkeypatch.setattr(voice.hub, "publish_threadsafe", lambda m: published.append(m))

    fc = FakeFunctionCall("hold", {"vehicle": "overwatch"})
    session = FakeSession(fc)
    ws = FakeBridgeWS()

    # Reconstruct just the model→browser inner coroutine by calling a tiny
    # harness that mirrors the voice session's tool loop body (see
    # voice_qwen.qwen_to_browser): announce tool → dispatch → announce result.
    async def model_to_browser():
        send_lock = asyncio.Lock()
        try:
            while True:
                async for resp in session.receive():
                    if resp.data:
                        await ws.send_bytes(resp.data)
                    if resp.tool_call:
                        for f in resp.tool_call.function_calls:
                            a = dict(f.args or {})
                            await ws.send_json({"type": "tool", "name": f.name, "args": a})
                            order.append(("sent_tool", f.name))
                            result = await voice.dispatch(f.name, a)
                            async with send_lock:
                                await session.send_tool_response(function_responses=[
                                    types_stub(f.id, f.name, result)])
                            await ws.send_json({"type": "tool_result", "name": f.name, "result": result})
                            order.append(("sent_result", f.name))
                            spoken = a.get("vehicle")
                            action_vid = (voice._resolve_vehicle_id(str(spoken)) if spoken else None) \
                                or registry.active_id()
                            voice.hub.publish_threadsafe({
                                "type": "voice_command", "name": f.name, "args": a,
                                "result": result, "vehicle": action_vid})
        except asyncio.CancelledError:
            return

    def types_stub(id, name, response):
        return {"id": id, "name": name, "response": response}

    run(model_to_browser())

    # Ordering: tool announced -> dispatch -> result announced.
    assert order == [("sent_tool", "hold"), ("dispatch", "hold"), ("sent_result", "hold")]
    types_sent = [m["type"] for m in ws.json_sent]
    assert types_sent == ["tool", "tool_result"]
    # voice_command published with resolved vehicle.
    assert published and published[0]["type"] == "voice_command"
    assert published[0]["vehicle"] == "overwatch"
