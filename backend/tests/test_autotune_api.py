"""Functional tests for the autotune REST endpoints (app/api.py) and the voice
dispatch tools (app/voice.py).

NO HARDWARE: a fake fleet + a stubbed autotune manager so the SAFETY GATES are
exercised in isolation — 404 unknown vehicle, 409 offline, the explicit
confirm:true requirement (no fire on a bare call), and structured returns. The
manager itself (state machine) is covered by test_autotune.py; here we assert the
API + voice layers gate correctly and route to the right drone.

Run: cd backend && .venv/bin/python -m pytest tests/test_autotune_api.py -q
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

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import router  # noqa: E402
from app.mavlink.registry import Vehicle, registry  # noqa: E402


class FakeLink:
    def __init__(self, connected=True):
        self._state = {"connected": connected}
        self.connection_string = "fake://link"

    def snapshot(self):
        return dict(self._state)


def install_fleet(ow_connected=True, our_connected=True, active="overwatch"):
    ow = FakeLink(connected=ow_connected)
    our = FakeLink(connected=our_connected)
    registry._vehicles = {
        "overwatch": Vehicle("overwatch", "Overwatch", "hex", ow,
                             supports_offboard=True, supports_missions=True,
                             supports_autotune=True),
        # Outrider: DDS bridge → autotune cmd 212 returns UNSUPPORTED, so it is NOT
        # autotune-capable (matches config.fleet()).
        "outrider": Vehicle("outrider", "Outrider", "quad", our,
                            supports_offboard=False, supports_missions=True,
                            supports_autotune=False),
    }
    registry._order = ["overwatch", "outrider"]
    registry._active = active
    return ow, our


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def fake_manager(monkeypatch):
    """Replace the autotune manager with a recorder so no real poll task spins."""
    rec: dict[str, list] = {"start": [], "cancel": [], "status": {}}

    async def fake_start(vid, link):
        rec["start"].append(vid)
        return {"state": "RUNNING", "progress": 0, "axis": None, "reason": None,
                "statustexts": [], "running": True}

    async def fake_cancel(vid, link):
        rec["cancel"].append(vid)
        return {"state": "CANCELLED", "progress": 0, "axis": None,
                "reason": "cancelled by operator", "statustexts": [], "running": False}

    def fake_status(vid):
        return rec["status"].get(vid)

    def fake_status_all():
        return list(rec["status"].values())

    import app.api as api
    monkeypatch.setattr(api.autotune_manager, "start", fake_start)
    monkeypatch.setattr(api.autotune_manager, "cancel", fake_cancel)
    monkeypatch.setattr(api.autotune_manager, "status", fake_status)
    monkeypatch.setattr(api.autotune_manager, "status_all", fake_status_all)
    return rec


# ── START gating ──────────────────────────────────────────────────────────────
def test_start_requires_confirm(client, fake_manager):
    """A bare call (confirm omitted/false) must NOT fire — it returns 409 with the
    safety preconditions and confirm_required so the UI states them and asks."""
    install_fleet()
    r = client.post("/api/autotune/start", json={"vehicle": "overwatch"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["confirm_required"] is True
    assert "ARMED" in detail["reason"] and "HOVERING" in detail["reason"]
    assert fake_manager["start"] == [], "no autotune may start without confirm"


def test_start_with_confirm_fires(client, fake_manager):
    install_fleet()
    r = client.post("/api/autotune/start",
                    json={"vehicle": "overwatch", "confirm": True})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["vehicle"] == "overwatch"
    assert body["state"] == "RUNNING"
    assert fake_manager["start"] == ["overwatch"], "must route to the named drone"


def test_start_outrider_refused_422(client, fake_manager):
    """Outrider reaches PX4 via the DDS bridge, where autotune's cmd 212 returns
    UNSUPPORTED — the capability guard must refuse with 422 and send NOTHING (never a
    false-start that leaves the drone armed/hovering), even with confirm:true."""
    install_fleet()
    r = client.post("/api/autotune/start",
                    json={"vehicle": "outrider", "confirm": True})
    assert r.status_code == 422
    assert fake_manager["start"] == [], "no cmd 212 may be sent to a DDS-bridge drone"


def test_start_unknown_vehicle_404(client, fake_manager):
    install_fleet()
    r = client.post("/api/autotune/start", json={"vehicle": "ghost", "confirm": True})
    assert r.status_code == 404
    assert fake_manager["start"] == []


def test_start_offline_vehicle_409(client, fake_manager):
    install_fleet(ow_connected=False)
    r = client.post("/api/autotune/start",
                    json={"vehicle": "overwatch", "confirm": True})
    assert r.status_code == 409
    assert "offline" in r.json()["detail"]
    assert fake_manager["start"] == [], "an offline drone must never be tuned"


def test_start_active_vehicle_when_omitted(client, fake_manager):
    install_fleet(active="overwatch")
    r = client.post("/api/autotune/start", json={"confirm": True})
    assert r.status_code == 200
    assert fake_manager["start"] == ["overwatch"], "omitted vehicle → active drone"


# ── STATUS ──────────────────────────────────────────────────────────────────
def test_status_idle_for_never_tuned(client, fake_manager):
    install_fleet()
    r = client.get("/api/autotune/status", params={"vehicle": "overwatch"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "IDLE" and body["running"] is False


def test_status_unknown_vehicle_404(client, fake_manager):
    install_fleet()
    r = client.get("/api/autotune/status", params={"vehicle": "ghost"})
    assert r.status_code == 404


def test_status_all(client, fake_manager):
    install_fleet()
    fake_manager["status"]["overwatch"] = {"vehicle": "overwatch", "state": "RUNNING",
                                           "progress": 42, "running": True}
    r = client.get("/api/autotune/status")
    assert r.status_code == 200
    assert r.json()["vehicles"][0]["progress"] == 42


# ── CANCEL ────────────────────────────────────────────────────────────────────
def test_cancel_routes_to_vehicle(client, fake_manager):
    install_fleet()
    r = client.post("/api/autotune/cancel", json={"vehicle": "overwatch"})
    assert r.status_code == 200
    assert r.json()["state"] == "CANCELLED"
    assert fake_manager["cancel"] == ["overwatch"]


def test_cancel_unknown_vehicle_404(client, fake_manager):
    install_fleet()
    r = client.post("/api/autotune/cancel", json={"vehicle": "ghost"})
    assert r.status_code == 404


# ── VOICE dispatch: run_autotune / cancel_autotune ────────────────────────────
def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def voice_mod(monkeypatch):
    import app.voice as voice

    rec: dict[str, list] = {"start": [], "cancel": [], "completion": []}

    async def fake_start(vid, link):
        rec["start"].append(vid)
        return {"state": "RUNNING", "progress": 0, "running": True}

    async def fake_cancel(vid, link):
        rec["cancel"].append(vid)
        return {"state": "CANCELLED", "running": False}

    monkeypatch.setattr(voice.autotune_manager, "start", fake_start)
    monkeypatch.setattr(voice.autotune_manager, "cancel", fake_cancel)
    monkeypatch.setattr(voice.completion, "autotune",
                        lambda *a, **k: rec["completion"].append(a[0]))
    return voice, rec


def test_voice_run_autotune_asks_before_confirm(voice_mod):
    """First call (no confirm) must NOT start — it surfaces the preconditions and
    confirm_required so STADO states them and asks the operator."""
    voice, rec = voice_mod
    install_fleet(active="overwatch")
    res = _run(voice.dispatch("run_autotune", {"vehicle": "overwatch"}))
    assert res["ok"] is False and res["confirm_required"] is True
    assert "ARMED" in res["reason"]
    assert rec["start"] == [], "no tune may start before the operator confirms"


def test_voice_run_autotune_starts_on_confirm(voice_mod):
    voice, rec = voice_mod
    install_fleet(active="overwatch")
    res = _run(voice.dispatch("run_autotune",
                              {"vehicle": "overwatch", "confirm": True}))
    assert res["ok"] is True and res["vehicle"] == "Overwatch"
    assert rec["start"] == ["overwatch"]
    assert rec["completion"] == ["Overwatch"], "a spoken completion watcher is armed"


def test_voice_run_autotune_outrider_refused(voice_mod):
    """Voice run_autotune on Outrider (DDS bridge) must refuse with ok:False and send
    NOTHING — no false-start — even with confirm:true."""
    voice, rec = voice_mod
    install_fleet(active="overwatch")
    res = _run(voice.dispatch("run_autotune",
                              {"vehicle": "outrider", "confirm": True}))
    assert res["ok"] is False
    assert "UNSUPPORTED" in res["error"] or "MAVLink-on-TELEM2" in res["error"]
    assert rec["start"] == [], "no cmd 212 may be sent to a DDS-bridge drone"


def test_voice_run_autotune_offline_refuses(voice_mod):
    voice, rec = voice_mod
    install_fleet(ow_connected=False)
    res = _run(voice.dispatch("run_autotune",
                              {"vehicle": "overwatch", "confirm": True}))
    assert res["ok"] is False and "offline" in res["error"]
    assert rec["start"] == []


def test_voice_run_autotune_unknown_vehicle(voice_mod):
    voice, rec = voice_mod
    install_fleet()
    res = _run(voice.dispatch("run_autotune", {"vehicle": "ghost", "confirm": True}))
    assert res["ok"] is False and "unknown vehicle" in res["error"]
    assert rec["start"] == []


def test_voice_cancel_autotune(voice_mod):
    voice, rec = voice_mod
    install_fleet(active="overwatch")
    res = _run(voice.dispatch("cancel_autotune", {"vehicle": "overwatch"}))
    assert res["ok"] is True and rec["cancel"] == ["overwatch"]
