"""Tests for the runtime fleet MAX-ALTITUDE CEILING (app/safety.py).

Covers, fully offline (no hardware / no cloud models):
  * the safety module: set/get/clear, check_altitude raise + override audit,
    fit_stack_under_ceiling fitting/refusing;
  * command-layer enforcement (commands.takeoff/goto/orbit refuse above ceiling,
    allow with override);
  * voice dispatch: too-high takeoff/goto/orbit/survey REFUSED with the ceiling
    reason; override=true allows it; set/clear tools; ceiling in get_status;
  * staggered fleet takeoff fits UNDER the ceiling (top <= ceiling, >= 15 m sep)
    and refuses when it can't fit;
  * REST: POST/GET /api/safety/max_altitude + ceiling-refused commands return 422,
    /state echoes the ceiling.

Run: cd backend && PYTHONPATH=. .venv/bin/python -m pytest tests/test_max_altitude_ceiling.py -q
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

# Stub heavy/optional deps BEFORE importing app modules.
for _name, _attrs in (("ultralytics", {"YOLO": object}), ("moondream", {})):
    if _name not in sys.modules:
        _m = types.ModuleType(_name)
        for k, v in _attrs.items():
            setattr(_m, k, v)
        sys.modules[_name] = _m

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.voice as voice  # noqa: E402
from app import safety  # noqa: E402
from app.api import router  # noqa: E402
from app.mavlink import commands  # noqa: E402
from app.survey import coordinated  # noqa: E402
from app.mavlink.registry import Vehicle, registry  # noqa: E402


# ── fakes / fixtures ──────────────────────────────────────────────────────────
class FakeLink:
    def __init__(self, connected=True, **state):
        base = {"connected": connected, "armed": False, "mode": "HOLD",
                "lat": 47.397, "lon": 8.545, "alt_rel": 20.0, "alt_msl": 520.0,
                "heading": 0.0, "groundspeed": 1.0, "battery_pct": 88,
                "gps_fix": 3, "satellites": 12}
        base.update(state)
        self._state = base
        self.connection_string = "fake://link"
        self.long_cmds: list = []
        self.int_cmds: list = []

    def snapshot(self):
        return dict(self._state)

    # commands.orbit/goto use command_int; commands.takeoff uses command_long.
    def command_long(self, command, *params, confirmation=0):
        self.long_cmds.append((int(command), tuple(params)))

    # QWEN PORT: the SITL takeoff patch (scripts/patch_commands_takeoff.py)
    # switches to TAKEOFF mode before arming, so takeoff() now calls set_mode.
    def set_mode(self, base_mode, main_mode, sub_mode):
        self._state["mode"] = "TAKEOFF"

    def command_int(self, command, x, y, z, p1=0, p2=0, p3=0, p4=0, frame=0):
        self.int_cmds.append((int(command), x, y, z))


def install_fleet(ow_connected=True, our_connected=True, active="overwatch",
                  outrider_caps=False):
    ow = FakeLink(connected=ow_connected)
    our = FakeLink(connected=our_connected)
    registry._vehicles = {
        "overwatch": Vehicle("overwatch", "Overwatch", "hex", ow,
                             supports_offboard=True, supports_missions=True),
        "outrider": Vehicle("outrider", "Outrider", "quad", our,
                            supports_offboard=outrider_caps,
                            supports_missions=outrider_caps),
    }
    registry._order = ["overwatch", "outrider"]
    registry._active = active
    return ow, our


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── 1. safety module ───────────────────────────────────────────────────────────
def test_default_no_ceiling():
    assert safety.get_max_altitude() is None
    assert safety.exceeds(9999) is False  # no ceiling => nothing exceeds


def test_set_get_clear():
    assert safety.set_max_altitude(80) == 80.0
    assert safety.get_max_altitude() == 80.0
    assert safety.exceeds(81) is True and safety.exceeds(80) is False
    safety.clear_max_altitude()
    assert safety.get_max_altitude() is None


def test_set_rejects_non_positive():
    with pytest.raises(ValueError):
        safety.set_max_altitude(0)
    with pytest.raises(ValueError):
        safety.set_max_altitude(-5)


def test_check_altitude_raises_with_reason():
    safety.set_max_altitude(50)
    with pytest.raises(safety.CeilingExceeded) as ei:
        safety.check_altitude(60)
    assert "60" in str(ei.value) and "50" in str(ei.value)
    assert "override required" in str(ei.value)
    assert ei.value.requested == 60.0 and ei.value.ceiling == 50.0


def test_check_altitude_override_bypasses_and_audits(monkeypatch):
    safety.set_max_altitude(50)
    logged: list = []
    monkeypatch.setattr(safety, "_audit_override",
                        lambda alt, ceil, ctx: logged.append((alt, ceil, ctx)))
    safety.check_altitude(60, override=True, context="takeoff")  # no raise
    assert logged == [(60, 50, "takeoff")]


def test_fit_stack_under_ceiling_lowers_whole_stack():
    safety.set_max_altitude(40)
    # base 50 → top wants 50, sep 15, floor 8: ceiling 40 fits (40 >= 8+15), so
    # the top is lowered to the ceiling and the bottom sits sep below.
    top, bottom = safety.fit_stack_under_ceiling(50, 15.0, 8.0)
    assert top == 40.0
    assert bottom == 25.0  # 40 - 15
    assert top - bottom >= 15.0


def test_fit_stack_refuses_when_ceiling_too_low():
    safety.set_max_altitude(20)  # < floor(8) + sep(15) = 23 → can't fit
    with pytest.raises(safety.CeilingExceeded):
        safety.fit_stack_under_ceiling(50, 15.0, 8.0)


def test_fit_stack_no_ceiling_unconstrained():
    top, bottom = safety.fit_stack_under_ceiling(50, 15.0, 8.0)
    assert top == 50.0 and bottom == 35.0


# ── 2. command-layer enforcement ────────────────────────────────────────────────
def test_command_takeoff_refused_above_ceiling(monkeypatch):
    safety.set_max_altitude(30)
    link = FakeLink()
    armed: list = []

    async def fake_arm(l, force=False, timeout=3.0):
        armed.append(l)
        return {"ok": True}

    monkeypatch.setattr(commands, "arm", fake_arm)
    with pytest.raises(safety.CeilingExceeded):
        run(commands.takeoff(link, 45))
    assert armed == [], "must refuse BEFORE arming (no motors spun for a refused climb)"


def test_command_takeoff_override_allows(monkeypatch):
    safety.set_max_altitude(30)
    link = FakeLink(alt_rel=0.0)  # on the ground (home-altitude gate: alt_rel ~= 0)

    async def fake_arm(l, force=False, timeout=3.0):
        return {"ok": True}

    monkeypatch.setattr(commands, "arm", fake_arm)
    res = run(commands.takeoff(link, 45, override=True))
    assert res["ok"] is True and res["altitude"] == 45


def test_command_goto_refused_then_override(monkeypatch):
    safety.set_max_altitude(30)
    link = FakeLink()
    with pytest.raises(safety.CeilingExceeded):
        run(commands.goto(link, 47.0, 8.0, 50))
    assert link.int_cmds == [], "no DO_REPOSITION sent on refusal"
    run(commands.goto(link, 47.0, 8.0, 50, override=True))
    assert link.int_cmds, "override sends the reposition"


def test_command_orbit_refused(monkeypatch):
    safety.set_max_altitude(30)
    link = FakeLink()
    with pytest.raises(safety.CeilingExceeded):
        run(commands.orbit(link, 47.0, 8.0, 50, 20, 3))
    assert link.int_cmds == []


# ── 3. voice dispatch enforcement ────────────────────────────────────────────────
@pytest.fixture
def voice_cmds(monkeypatch):
    """Record voice command-layer calls (the real ceiling checks still run in
    dispatch before these; the command layer is faked so nothing is sent)."""
    rec: dict[str, list] = {"takeoff": [], "goto": [], "orbit": []}

    async def fake_arm(link, force=False, timeout=3.0):
        return {"ok": True, "armed": True, "result": 0, "result_name": "ACCEPTED",
                "reason": None, "statustexts": []}

    async def fake_takeoff(link, alt=10.0, override=False):
        rec["takeoff"].append((link, alt, override))
        return {"ok": True, "altitude": alt}

    async def fake_goto(link, lat, lon, alt, speed=-1.0, override=False):
        rec["goto"].append((link, alt, override))

    async def fake_orbit(link, lat, lon, alt, radius=20.0, velocity=3.0,
                         clockwise=True, override=False):
        rec["orbit"].append((link, alt, override))

    monkeypatch.setattr(voice.commands, "arm", fake_arm)
    monkeypatch.setattr(voice.commands, "takeoff", fake_takeoff)
    monkeypatch.setattr(voice.commands, "goto", fake_goto)
    monkeypatch.setattr(voice.commands, "orbit", fake_orbit)
    for cname in ("takeoff", "land", "rtl", "goto", "orbit"):
        monkeypatch.setattr(voice.completion, cname, lambda *a, **k: None)
    return rec


def test_voice_set_and_clear_max_altitude():
    install_fleet()
    res = run(voice.dispatch("set_max_altitude", {"altitude_m": 75}))
    assert res["ok"] and res["max_altitude_m"] == 75.0
    assert safety.get_max_altitude() == 75.0
    res2 = run(voice.dispatch("clear_max_altitude", {}))
    assert res2["ok"] and res2["max_altitude_m"] is None
    assert safety.get_max_altitude() is None


def test_voice_takeoff_refused_above_ceiling(voice_cmds):
    install_fleet()
    safety.set_max_altitude(40)
    res = run(voice.dispatch("takeoff", {"vehicle": "overwatch", "altitude_m": 60}))
    assert res["ok"] is False
    assert res["override_required"] is True
    assert res["ceiling_m"] == 40.0 and res["requested_m"] == 60.0
    assert "60" in res["error"] and "40" in res["error"]
    assert voice_cmds["takeoff"] == [], "refused takeoff must not reach the command layer"


def test_voice_takeoff_override_allows_and_announces(voice_cmds):
    install_fleet()
    safety.set_max_altitude(40)
    res = run(voice.dispatch("takeoff",
                             {"vehicle": "overwatch", "altitude_m": 60, "override": True}))
    assert res["ok"] is True
    assert res["override_applied"] is True
    assert "override" in res["note"].lower()
    assert voice_cmds["takeoff"] and voice_cmds["takeoff"][0][1:] == (60.0, True)


def test_voice_goto_point_refused_then_override(voice_cmds, monkeypatch):
    install_fleet()
    safety.set_max_altitude(40)
    monkeypatch.setattr(voice.pois, "find",
                        lambda n: {"name": "LZ", "lat": 47.0, "lng": 8.0})
    res = run(voice.dispatch("goto_point", {"name": "LZ", "altitude_m": 70}))
    assert res["ok"] is False and res["override_required"] is True
    assert voice_cmds["goto"] == []
    res2 = run(voice.dispatch("goto_point",
                              {"name": "LZ", "altitude_m": 70, "override": True}))
    assert res2["ok"] is True and voice_cmds["goto"][0][1:] == (70.0, True)


def test_voice_orbit_point_refused(voice_cmds, monkeypatch):
    install_fleet()
    safety.set_max_altitude(40)
    monkeypatch.setattr(voice.pois, "find",
                        lambda n: {"name": "B", "lat": 47.0, "lng": 8.0})
    res = run(voice.dispatch("orbit_point", {"name": "B", "altitude_m": 55}))
    assert res["ok"] is False and res["override_required"] is True
    assert voice_cmds["orbit"] == []


def test_voice_get_status_echoes_ceiling(voice_cmds):
    install_fleet()
    safety.set_max_altitude(90)
    res = run(voice.dispatch("get_status", {"vehicle": "overwatch"}))
    assert res["connected"] is True and res["max_altitude_m"] == 90.0
    # offline also echoes it
    install_fleet(our_connected=False)
    safety.set_max_altitude(90)
    res2 = run(voice.dispatch("get_status", {"vehicle": "outrider"}))
    assert res2["connected"] is False and res2["max_altitude_m"] == 90.0


# ── 4. staggered fleet takeoff vs ceiling ─────────────────────────────────────────
def test_capped_stack_fits_under_ceiling():
    install_fleet()
    safety.set_max_altitude(60)
    # 2-drone stack from base 50: assign_altitudes → outrider 50, overwatch 65.
    # Capped: top must be <= 60, sep >= 15, top lowered uniformly.
    alts = coordinated.assign_altitudes_capped(
        ["overwatch", "outrider"], base_alt=50, sep_m=15.0)
    assert max(alts.values()) <= 60.0, "top drone must not exceed the ceiling"
    assert alts["overwatch"] > alts["outrider"], "Overwatch stays higher"
    assert alts["overwatch"] - alts["outrider"] >= 15.0, "15 m separation kept"


def test_capped_stack_refuses_when_ceiling_too_low():
    install_fleet()
    safety.set_max_altitude(15)  # < floor(3) + sep(15) = 18 → cannot fit the pair
    with pytest.raises(safety.CeilingExceeded):
        coordinated.assign_altitudes_capped(
            ["overwatch", "outrider"], base_alt=50, sep_m=15.0)


# ── 5. survey vs ceiling (voice) ──────────────────────────────────────────────────
def _stub_fleet_survey(monkeypatch):
    flew: dict = {"called": False}

    def fake_split(lat, lon, w, h, hdg, n=2, gap_m=5.0):
        return [[(0.0, 0.0), (0.0, 1.0), (1.0, 1.0)] for _ in range(n)]

    async def fake_plan_and_fly(vehicles, zones, base_alt, line_spacing_m, sep_m,
                                source_polygon=None):
        flew["called"] = True
        return [{"vehicle": v, "name": v, "polygon": [[0.0, 0.0]], "altitude": 30.0,
                 "waypoints": 4, "path": [[1.0, 2.0]]} for v in vehicles]

    monkeypatch.setattr(voice.coordinated, "split_rect", fake_split)
    monkeypatch.setattr(voice.coordinated, "plan_and_fly", fake_plan_and_fly)
    monkeypatch.setattr(voice.hub, "publish_threadsafe", lambda m: None)
    return flew


def test_voice_survey_refused_above_ceiling(monkeypatch):
    install_fleet(ow_connected=True, our_connected=False)
    flew = _stub_fleet_survey(monkeypatch)
    # survey base_alt is 30 (single drone → top 30). A 25 m ceiling refuses it.
    safety.set_max_altitude(25)
    res = run(voice.dispatch("survey_area", {"size_m": 200}))
    assert res["ok"] is False and res["override_required"] is True
    assert flew["called"] is False, "a refused survey must never fly"


def test_voice_survey_allowed_under_ceiling(monkeypatch):
    install_fleet(ow_connected=True, our_connected=False)
    flew = _stub_fleet_survey(monkeypatch)
    safety.set_max_altitude(50)  # survey top 30 <= 50 → allowed
    res = run(voice.dispatch("survey_area", {"size_m": 200}))
    assert res["ok"] is True and flew["called"] is True


# ── 6. REST surface ────────────────────────────────────────────────────────────
def test_rest_set_get_clear_ceiling(client):
    install_fleet()
    assert client.get("/api/safety/max_altitude").json() == {"max_altitude_m": None}
    r = client.post("/api/safety/max_altitude", json={"altitude_m": 100})
    assert r.status_code == 200 and r.json()["max_altitude_m"] == 100.0
    assert client.get("/api/safety/max_altitude").json()["max_altitude_m"] == 100.0
    assert client.request("DELETE", "/api/safety/max_altitude").json()["max_altitude_m"] is None
    assert safety.get_max_altitude() is None


def test_rest_set_rejects_non_positive(client):
    install_fleet()
    r = client.post("/api/safety/max_altitude", json={"altitude_m": 0})
    assert r.status_code == 400


def test_rest_state_echoes_ceiling(client):
    install_fleet()
    safety.set_max_altitude(70)
    body = client.get("/api/state").json()
    assert body["max_altitude_m"] == 70.0


def test_rest_takeoff_refused_above_ceiling_422(client, monkeypatch):
    install_fleet()
    safety.set_max_altitude(30)
    armed: list = []

    async def fake_arm(link, force=False, timeout=3.0):
        armed.append(link)
        return {"ok": True}

    monkeypatch.setattr(commands, "arm", fake_arm)
    r = client.post("/api/command/takeoff", json={"altitude": 45, "vehicle": "overwatch"})
    assert r.status_code == 422
    assert "45" in r.json()["detail"] and "30" in r.json()["detail"]
    assert armed == [], "refused takeoff never arms"


def test_rest_takeoff_override_allows(client, monkeypatch):
    install_fleet()
    safety.set_max_altitude(30)

    async def fake_takeoff(link, alt=10.0, override=False):
        return {"ok": True, "altitude": alt}

    monkeypatch.setattr(commands, "takeoff", fake_takeoff)
    r = client.post("/api/command/takeoff",
                    json={"altitude": 45, "vehicle": "overwatch", "override": True})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_rest_fleet_takeoff_fits_under_ceiling(client, monkeypatch):
    install_fleet()
    safety.set_max_altitude(60)
    sent: list = []

    async def fake_takeoff(link, alt=10.0, override=False):
        sent.append(alt)
        return {"ok": True, "altitude": alt}

    monkeypatch.setattr(commands, "takeoff", fake_takeoff)
    r = client.post("/api/command/takeoff", json={"altitude": 50, "vehicle": "all"})
    assert r.status_code == 200
    vehicles = r.json()["vehicles"]
    alts = {vid: v["altitude"] for vid, v in vehicles.items()}
    assert max(alts.values()) <= 60.0, "top drone must be at/under the ceiling"
    assert alts["overwatch"] > alts["outrider"], "Overwatch higher"
    assert alts["overwatch"] - alts["outrider"] >= 15.0, "15 m separation"


def test_rest_fleet_takeoff_refused_when_ceiling_too_low(client, monkeypatch):
    install_fleet()
    safety.set_max_altitude(15)  # < floor(3)+sep(15)=18 → can't fit the 15 m-separated pair
    sent: list = []

    async def fake_takeoff(link, alt=10.0, override=False):
        sent.append(alt)
        return {"ok": True, "altitude": alt}

    monkeypatch.setattr(commands, "takeoff", fake_takeoff)
    r = client.post("/api/command/takeoff", json={"altitude": 50, "vehicle": "all"})
    assert r.status_code == 422
    assert sent == [], "no drone takes off when the stack can't fit"


def test_rest_goto_refused_above_ceiling_422(client):
    install_fleet()
    safety.set_max_altitude(30)
    r = client.post("/api/command/goto", json={"lat": 47.0, "lon": 8.0, "alt": 50})
    assert r.status_code == 422


def test_rest_orbit_refused_above_ceiling_422(client):
    install_fleet()
    safety.set_max_altitude(30)
    r = client.post("/api/command/orbit", json={"lat": 47.0, "lon": 8.0, "alt": 50})
    assert r.status_code == 422
