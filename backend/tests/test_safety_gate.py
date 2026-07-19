"""Ready-for-Flight safety gate tests.

Guards against the exact class of incident that triggered this feature: a
command hitting the vehicle from *any* source (HTTP, voice, coordination) when
the operator has not authorized flight. Also covers the airborne auto-lock and
the mid-flight-restart seed.

Run: cd backend && PYTHONPATH=. uv run python -m pytest tests/test_safety_gate.py -q
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

# Stub optional deps before importing the app (matches test_api_commands.py).
for _name, _attrs in (("ultralytics", {"YOLO": object}), ("moondream", {})):
    if _name not in sys.modules:
        _m = types.ModuleType(_name)
        for k, v in _attrs.items():
            setattr(_m, k, v)
        sys.modules[_name] = _m

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import safety  # noqa: E402
from app.api import router  # noqa: E402
from app.mavlink import commands as mav_commands  # noqa: E402
from app.mavlink.registry import Vehicle, registry  # noqa: E402


class FakeLink:
    def __init__(self, connected=True, **state):
        base = {"connected": connected, "armed": False, "mode": "HOLD",
                "lat": 47.397, "lon": 8.545, "alt_rel": 0.0, "alt_msl": 520.0,
                "heading": 0.0, "groundspeed": 0.0, "battery_pct": 90,
                "gps_fix": 3, "satellites": 12}
        base.update(state)
        self._state = base
        self.connection_string = "fake://link"

    def snapshot(self):
        return dict(self._state)


@pytest.fixture
def fleet():
    """Two fake vehicles. Returns (ow_link, our_link)."""
    ow = FakeLink(connected=True, name="Overwatch")
    our = FakeLink(connected=True, name="Outrider")
    registry._vehicles = {
        "overwatch": Vehicle("overwatch", "Overwatch", "hex", ow,
                             supports_offboard=True, supports_missions=True,
                             supports_autotune=True),
        "outrider": Vehicle("outrider", "Outrider", "quad", our,
                            supports_offboard=False, supports_missions=False,
                            supports_autotune=False),
    }
    registry._order = ["overwatch", "outrider"]
    registry._active = "overwatch"
    return ow, our


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def gate_off():
    """Force the gate OFF for both vehicles at the start of a gate test — the
    default autouse fixture in conftest.py seeds it ON."""
    safety.reset_for_tests()
    yield
    # conftest's fixture will reset again after this test.


@pytest.fixture
def fake_commands(monkeypatch):
    """Non-recording fake commands: enough for the gate tests to check that
    /command/land actually reaches the handler. Uses the truthful arm shape."""
    async def fake_arm(link, force=False, timeout=3.0):
        return {"ok": True, "armed": True, "result": 0, "result_name": "ACCEPTED",
                "reason": None, "statustexts": []}

    async def fake_disarm(link, force=False, timeout=3.0):
        return {"ok": True, "armed": False, "result": 0, "result_name": "ACCEPTED",
                "reason": None, "statustexts": []}

    async def fake_takeoff(link, alt=10.0, override=False):
        return {"ok": True, "reason": None}

    async def fake_noop(*a, **k):
        return None

    for name, fn in (("arm", fake_arm), ("disarm", fake_disarm),
                     ("takeoff", fake_takeoff), ("land", fake_noop),
                     ("rtl", fake_noop), ("hold", fake_noop), ("brake", fake_noop),
                     ("set_mode", fake_noop), ("goto", fake_noop),
                     ("orbit", fake_noop)):
        monkeypatch.setattr(mav_commands, name, fn)
        import app.api as api_mod
        monkeypatch.setattr(api_mod.commands, name, fn)


# ── 1. defaults ──────────────────────────────────────────────────────────────
def test_gate_defaults_off(fleet, gate_off, client):
    """A backend that has never seen a set_ready call reports OFF for every vehicle."""
    r = client.get("/api/safety/ready_for_flight")
    assert r.status_code == 200
    entries = {v["vehicle"]: v for v in r.json()["vehicles"]}
    assert entries["overwatch"]["ready"] is False
    assert entries["outrider"]["ready"] is False
    assert entries["overwatch"]["locked"] is False


# ── 2. gated command refused ─────────────────────────────────────────────────
def test_gated_arm_refused_when_off(fleet, gate_off, client, fake_commands):
    r = client.post("/api/command/arm")
    assert r.status_code == 422
    body = r.json()
    assert body["detail"]["error"] == "ready_for_flight_off"
    assert body["detail"]["vehicle"] == "overwatch"


def test_gated_takeoff_refused_when_off(fleet, gate_off, client, fake_commands):
    r = client.post("/api/command/takeoff", json={"altitude": 10.0})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "ready_for_flight_off"


def test_gated_orbit_refused_when_off(fleet, gate_off, client, fake_commands):
    r = client.post("/api/command/orbit",
                    json={"lat": 47.5, "lon": 8.5, "alt": 20.0,
                          "radius": 25.0, "velocity": 4.0})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "ready_for_flight_off"


# ── 3. recovery commands bypass the gate ─────────────────────────────────────
def test_land_bypasses_gate(fleet, gate_off, client, fake_commands):
    r = client.post("/api/command/land")
    assert r.status_code == 200


def test_rtl_bypasses_gate(fleet, gate_off, client, fake_commands):
    r = client.post("/api/command/rtl")
    assert r.status_code == 200


def test_hold_bypasses_gate(fleet, gate_off, client, fake_commands):
    r = client.post("/api/command/hold")
    assert r.status_code == 200


def test_brake_bypasses_gate(fleet, gate_off, client, fake_commands):
    r = client.post("/api/command/brake")
    assert r.status_code == 200


def test_disarm_bypasses_gate(fleet, gate_off, client, fake_commands):
    r = client.post("/api/command/disarm")
    assert r.status_code == 200


def test_mode_land_bypasses_gate(fleet, gate_off, client, fake_commands):
    r = client.post("/api/command/mode", json={"name": "LAND"})
    assert r.status_code == 200


def test_mode_rtl_bypasses_gate(fleet, gate_off, client, fake_commands):
    r = client.post("/api/command/mode", json={"name": "RTL"})
    assert r.status_code == 200


def test_mode_position_gated(fleet, gate_off, client, fake_commands):
    """POSITION isn't a recovery mode — should be blocked by the gate."""
    r = client.post("/api/command/mode", json={"name": "POSITION"})
    assert r.status_code == 422


# ── 4. gate ON allows commands ──────────────────────────────────────────────
def test_gate_on_allows_arm(fleet, gate_off, client, fake_commands):
    # Enable the gate for overwatch (the active drone).
    r = client.put("/api/safety/ready_for_flight",
                   json={"vehicle": "overwatch", "ready": True})
    assert r.status_code == 200
    assert r.json()["ready"] is True

    r = client.post("/api/command/arm")
    assert r.status_code == 200


def test_gate_on_allows_takeoff(fleet, gate_off, client, fake_commands):
    client.put("/api/safety/ready_for_flight",
               json={"vehicle": "overwatch", "ready": True})
    r = client.post("/api/command/takeoff", json={"altitude": 10.0})
    assert r.status_code == 200


# ── 5. mid-flight lock ─────────────────────────────────────────────────────
def test_gate_locked_when_armed_and_airborne(fleet, gate_off, client, fake_commands):
    """Once the vehicle is armed+airborne (alt_rel > 1m), the gate is locked ON
    — the operator cannot turn it OFF (that would kill recovery paths)."""
    ow, _our = fleet
    # Simulate armed + airborne telemetry on the fake link.
    ow._state["armed"] = True
    ow._state["alt_rel"] = 5.0

    # Gate is currently OFF (default). Turning it ON is always fine.
    r = client.put("/api/safety/ready_for_flight",
                   json={"vehicle": "overwatch", "ready": True})
    assert r.status_code == 200

    # Trying to turn it OFF while armed+airborne → 409.
    r = client.put("/api/safety/ready_for_flight",
                   json={"vehicle": "overwatch", "ready": False})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "ready_for_flight_locked"


def test_gate_unlocks_after_disarm(fleet, gate_off, client, fake_commands):
    ow, _our = fleet
    ow._state["armed"] = True
    ow._state["alt_rel"] = 5.0
    client.put("/api/safety/ready_for_flight",
               json={"vehicle": "overwatch", "ready": True})

    # Now "land" and disarm: airborne condition clears.
    ow._state["armed"] = False
    ow._state["alt_rel"] = 0.0
    r = client.put("/api/safety/ready_for_flight",
                   json={"vehicle": "overwatch", "ready": False})
    assert r.status_code == 200
    assert r.json()["ready"] is False


def test_gate_toggle_off_below_airborne_threshold(fleet, gate_off, client, fake_commands):
    """Armed but on the ground (alt_rel <= 1m) does NOT lock the gate. This lets
    the operator disable the gate if they armed by mistake."""
    ow, _our = fleet
    ow._state["armed"] = True
    ow._state["alt_rel"] = 0.5  # below AIRBORNE_M threshold
    client.put("/api/safety/ready_for_flight",
               json={"vehicle": "overwatch", "ready": True})
    r = client.put("/api/safety/ready_for_flight",
                   json={"vehicle": "overwatch", "ready": False})
    assert r.status_code == 200


# ── 6. voice dispatch respects the gate ─────────────────────────────────────
def test_voice_dispatch_takeoff_refused_when_off(fleet, gate_off, monkeypatch):
    from app import voice as voice_mod

    # Fake the arm+takeoff commands so if the gate leaks the call would fake-succeed.
    async def fake_arm(link, force=False, timeout=3.0):
        return {"ok": True, "armed": True, "result": 0, "result_name": "ACCEPTED",
                "reason": None, "statustexts": []}
    async def fake_takeoff(link, alt=10.0, override=False):
        return {"ok": True, "reason": None}

    monkeypatch.setattr(voice_mod.commands, "arm", fake_arm)
    monkeypatch.setattr(voice_mod.commands, "takeoff", fake_takeoff)

    res = asyncio.run(voice_mod.dispatch("takeoff",
                                          {"vehicle": "overwatch", "altitude_m": 10}))
    assert res.get("ok") is False
    assert "ready-for-flight" in res.get("error", "").lower()


def test_voice_dispatch_land_bypasses_gate(fleet, gate_off, monkeypatch):
    from app import voice as voice_mod

    async def fake_land(link):
        return None

    monkeypatch.setattr(voice_mod.commands, "land", fake_land)

    # completion.land also runs; stub it so it doesn't spawn a real watcher.
    monkeypatch.setattr(voice_mod.completion, "land",
                        lambda vname, link, cb: None)

    res = asyncio.run(voice_mod.dispatch("land", {"vehicle": "overwatch"}))
    # land bypasses the gate — should not return a gate refusal.
    assert (
        res is None
        or "ready-for-flight" not in str(res.get("error", "")).lower()
    )


# ── 7. unwind hooks fire on OFF transition ─────────────────────────────────
def test_unwind_hooks_run_on_off_transition(fleet, gate_off):
    calls: list[str] = []

    safety.register_unwind_hook(lambda vid: calls.append(f"hook1:{vid}"))
    safety.register_unwind_hook(lambda vid: calls.append(f"hook2:{vid}"))

    # Turn on then off — only the OFF transition fires hooks.
    safety.set_ready("overwatch", True)
    assert calls == []

    safety.set_ready("overwatch", False)
    assert calls == ["hook1:overwatch", "hook2:overwatch"]


def test_unwind_hooks_dont_run_on_on_transition(fleet, gate_off):
    calls: list[str] = []
    safety.register_unwind_hook(lambda vid: calls.append(vid))
    safety.set_ready("overwatch", True)
    assert calls == []


def test_unwind_hook_failure_doesnt_block_others(fleet, gate_off):
    calls: list[str] = []

    def bad_hook(vid):
        raise RuntimeError("boom")
    safety.register_unwind_hook(bad_hook)
    safety.register_unwind_hook(lambda vid: calls.append(vid))

    safety.set_ready("overwatch", True)
    safety.set_ready("overwatch", False)
    # Second hook still ran despite the first raising.
    assert calls == ["overwatch"]


# ── 8. restart seed ─────────────────────────────────────────────────────────
def test_seed_from_telemetry_flips_gate_on_when_airborne(fleet, gate_off):
    """A backend restart mid-flight should re-seed the gate ON so recovery works."""
    safety.seed_from_telemetry("overwatch", armed=True, alt_rel=5.0)
    assert safety.is_ready("overwatch") is True


def test_seed_from_telemetry_no_op_on_ground(fleet, gate_off):
    """On-the-ground vehicles should stay OFF after the first frame."""
    safety.seed_from_telemetry("overwatch", armed=False, alt_rel=0.0)
    assert safety.is_ready("overwatch") is False


def test_seed_from_telemetry_only_fires_once(fleet, gate_off):
    """After the first frame the seed is idempotent — subsequent frames don't
    flip the gate again even if telemetry shows airborne."""
    # First frame: not airborne → gate stays OFF and seed is marked done.
    safety.seed_from_telemetry("overwatch", armed=False, alt_rel=0.0)
    assert safety.is_ready("overwatch") is False
    # Second frame: airborne — but seed already happened, so it must NOT flip
    # the gate. (Real code path: the frontend/UI is expected to arm the gate.)
    safety.seed_from_telemetry("overwatch", armed=True, alt_rel=5.0)
    assert safety.is_ready("overwatch") is False


# ── 9. is_locked semantics ─────────────────────────────────────────────────
def test_is_locked_requires_both_armed_and_airborne():
    assert safety.is_locked(armed=False, alt_rel=10.0) is False
    assert safety.is_locked(armed=True, alt_rel=0.5) is False  # below threshold
    assert safety.is_locked(armed=True, alt_rel=None) is False  # unknown alt
    assert safety.is_locked(armed=True, alt_rel=5.0) is True
