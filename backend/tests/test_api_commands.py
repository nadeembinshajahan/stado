"""Functional tests for the REST command/flight API (app/api.py).

NO HARDWARE: stubs the heavy vision deps, replaces the registry's vehicles with
fake links, and monkeypatches the mavlink command layer to RECORD calls instead
of sending. Verifies command ROUTING (right vehicle / connected vehicle), that
"success" responses reflect real work, and the takeoff/arm ack flows.

Run: cd backend && PYTHONPATH=. uv run python -m pytest tests/test_api_commands.py -q
Deps: pytest, httpx, fastapi (TestClient). ultralytics/moondream are stubbed.
"""
from __future__ import annotations

import sys
import types

import pytest

# ── stub heavy/optional deps BEFORE importing app modules ────────────────────
for _name, _attrs in (("ultralytics", {"YOLO": object}), ("moondream", {})):
    if _name not in sys.modules:
        _m = types.ModuleType(_name)
        for k, v in _attrs.items():
            setattr(_m, k, v)
        sys.modules[_name] = _m

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import router  # noqa: E402
from app.mavlink import commands as mav_commands  # noqa: E402
from app.mavlink import missions as mav_missions  # noqa: E402
from app.mavlink.registry import Vehicle, registry  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────
class FakeLink:
    def __init__(self, connected=True, **state):
        base = {"connected": connected, "armed": False, "mode": "HOLD",
                "lat": 47.397, "lon": 8.545, "alt_rel": 20.0, "alt_msl": 520.0,
                "heading": 0.0, "groundspeed": 0.0, "battery_pct": 90,
                "gps_fix": 3, "satellites": 12}
        base.update(state)
        self._state = base
        self.connection_string = "fake://link"

    def snapshot(self):
        return dict(self._state)


def install_fleet(ow_connected=True, our_connected=True, active="overwatch",
                  outrider_caps=False):
    """Replace the registry with two fake-linked vehicles. Returns (ow, our).
    Mirrors PRODUCTION capabilities (config.fleet): Overwatch full MAVLink
    (offboard+missions), Outrider a DDS-bridge vehicle supporting NEITHER (H1/H2).
    Pass outrider_caps=True for a test needing a mission/offboard-capable Outrider."""
    ow = FakeLink(connected=ow_connected, name="Overwatch")
    our = FakeLink(connected=our_connected, name="Outrider")
    registry._vehicles = {
        "overwatch": Vehicle("overwatch", "Overwatch", "hex", ow,
                             supports_offboard=True, supports_missions=True,
                             supports_autotune=True),
        "outrider": Vehicle("outrider", "Outrider", "quad", our,
                            supports_offboard=outrider_caps,
                            supports_missions=outrider_caps,
                            supports_autotune=outrider_caps),
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
def calls(monkeypatch):
    """Record every command-layer call (no real MAVLink). Yields a dict of lists."""
    rec: dict[str, list] = {
        "arm": [], "disarm": [], "takeoff": [], "land": [], "rtl": [],
        "hold": [], "brake": [], "set_mode": [], "goto": [], "orbit": [],
        "upload": [], "start": [], "clear": [], "stop_all_coord": [],
    }

    async def fake_arm(link, force=False, timeout=3.0):
        rec["arm"].append((link, force))
        return {"ok": True, "armed": True, "result": 0, "result_name": "ACCEPTED",
                "reason": None, "statustexts": []}

    async def fake_disarm(link, force=False, timeout=3.0):
        rec["disarm"].append((link, force))
        return {"ok": True, "armed": False, "result": 0, "result_name": "ACCEPTED",
                "reason": None, "statustexts": []}

    async def fake_takeoff(link, alt=10.0, override=False):
        rec["takeoff"].append((link, alt))

    async def fake_land(link):
        rec["land"].append(link)

    async def fake_rtl(link):
        rec["rtl"].append(link)

    async def fake_hold(link):
        rec["hold"].append(link)

    async def fake_brake(link):
        rec["brake"].append(link)

    async def fake_set_mode(link, name):
        rec["set_mode"].append((link, name))

    async def fake_goto(link, lat, lon, alt, speed=-1.0, override=False):
        rec["goto"].append((link, lat, lon, alt))

    async def fake_orbit(link, lat, lon, alt, radius=20.0, velocity=3.0, clockwise=True, override=False):
        rec["orbit"].append((link, lat, lon, radius))

    async def fake_upload(link, wps):
        rec["upload"].append((link, list(wps)))
        return True

    async def fake_start(link):
        rec["start"].append(link)

    async def fake_clear(link):
        rec["clear"].append(link)

    monkeypatch.setattr(mav_commands, "arm", fake_arm)
    monkeypatch.setattr(mav_commands, "disarm", fake_disarm)
    monkeypatch.setattr(mav_commands, "takeoff", fake_takeoff)
    monkeypatch.setattr(mav_commands, "land", fake_land)
    monkeypatch.setattr(mav_commands, "rtl", fake_rtl)
    monkeypatch.setattr(mav_commands, "hold", fake_hold)
    monkeypatch.setattr(mav_commands, "brake", fake_brake)
    monkeypatch.setattr(mav_commands, "set_mode", fake_set_mode)
    monkeypatch.setattr(mav_commands, "goto", fake_goto)
    monkeypatch.setattr(mav_commands, "orbit", fake_orbit)
    monkeypatch.setattr(mav_missions, "upload", fake_upload)
    monkeypatch.setattr(mav_missions, "start", fake_start)
    monkeypatch.setattr(mav_missions, "clear", fake_clear)
    # api.py imported these names into its own namespace; patch there too.
    import app.api as api_mod
    monkeypatch.setattr(api_mod.commands, "arm", fake_arm)
    monkeypatch.setattr(api_mod.commands, "disarm", fake_disarm)
    monkeypatch.setattr(api_mod.commands, "takeoff", fake_takeoff)
    monkeypatch.setattr(api_mod.commands, "land", fake_land)
    monkeypatch.setattr(api_mod.commands, "rtl", fake_rtl)
    monkeypatch.setattr(api_mod.commands, "hold", fake_hold)
    monkeypatch.setattr(api_mod.commands, "brake", fake_brake)
    monkeypatch.setattr(api_mod.commands, "set_mode", fake_set_mode)
    monkeypatch.setattr(api_mod.commands, "goto", fake_goto)
    monkeypatch.setattr(api_mod.commands, "orbit", fake_orbit)
    monkeypatch.setattr(api_mod.missions, "upload", fake_upload)
    monkeypatch.setattr(api_mod.missions, "start", fake_start)
    monkeypatch.setattr(api_mod.missions, "clear", fake_clear)
    return rec


# ── vehicles / active scoping ────────────────────────────────────────────────
def test_vehicles_list_reports_connected_and_active(client):
    install_fleet(ow_connected=True, our_connected=False)
    r = client.get("/api/vehicles")
    assert r.status_code == 200
    by_id = {v["id"]: v for v in r.json()}
    assert by_id["overwatch"]["connected"] is True
    assert by_id["outrider"]["connected"] is False
    # active auto-follows connectivity: overwatch is the only connected one.
    assert by_id["overwatch"]["active"] is True


def test_vehicles_list_surfaces_capability_flags(client):
    """GET /api/vehicles carries the per-vehicle capability flags so the cockpit
    UI can grey-out actions a vehicle would refuse. Mirrors the registry's
    supports_* (Overwatch full MAVLink → all True; Outrider DDS-bridge → all
    False). This only SURFACES existing data — the guard logic is unchanged."""
    install_fleet()  # Overwatch fully capable, Outrider a DDS-bridge (all False)
    r = client.get("/api/vehicles")
    assert r.status_code == 200
    by_id = {v["id"]: v for v in r.json()}
    # Every item exposes the three flags.
    for v in r.json():
        assert "supports_offboard" in v
        assert "supports_missions" in v
        assert "supports_autotune" in v
    # Overwatch (full MAVLink) supports all three.
    assert by_id["overwatch"]["supports_offboard"] is True
    assert by_id["overwatch"]["supports_missions"] is True
    assert by_id["overwatch"]["supports_autotune"] is True
    # Outrider (DDS bridge) refuses all three — surfaced so the UI disables them.
    assert by_id["outrider"]["supports_offboard"] is False
    assert by_id["outrider"]["supports_missions"] is False
    assert by_id["outrider"]["supports_autotune"] is False


def test_set_active_unknown_404(client):
    install_fleet()
    r = client.post("/api/vehicle/active", json={"id": "ghost"})
    assert r.status_code == 404


# ── arm: real ack reflected ──────────────────────────────────────────────────
def test_arm_routes_to_named_vehicle(client, calls):
    ow, our = install_fleet()
    r = client.post("/api/command/arm", params={"vehicle": "outrider"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(calls["arm"]) == 1
    assert calls["arm"][0][0] is our, "arm must target the NAMED vehicle's link"


def test_arm_denied_surfaces_false(client, calls, monkeypatch):
    ow, our = install_fleet()

    async def deny(link, force=False, timeout=3.0):
        return {"ok": False, "armed": None, "result": 2, "result_name": "DENIED",
                "reason": "Arming denied: preflight", "statustexts": ["Arming denied: preflight"]}

    import app.api as api_mod
    monkeypatch.setattr(api_mod.commands, "arm", deny)
    r = client.post("/api/command/arm")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False, "a DENIED arm must NOT report ok:true"
    assert "preflight" in (body.get("reason") or "")


def test_arm_all_aggregates_per_vehicle(client, calls):
    install_fleet(ow_connected=True, our_connected=True)
    r = client.post("/api/command/arm", params={"vehicle": "all"})
    body = r.json()
    assert body["ok"] is True
    assert set(body["vehicles"].keys()) == {"overwatch", "outrider"}
    assert len(calls["arm"]) == 2


# ── takeoff: fleet 'all' with nobody connected must NOT fly an offline link (H3) ─
def test_takeoff_all_with_none_connected_409_and_no_command(client, calls):
    """FIX (H3): /command/takeoff 'all' must NOT fall back to an offline link and
    claim success. With NOBODY connected it 409s and issues no takeoff."""
    ow, our = install_fleet(ow_connected=False, our_connected=False)
    r = client.post("/api/command/takeoff", json={"altitude": 15, "vehicle": "all"})
    assert r.status_code == 409, "must refuse to take off with no connected drones"
    assert len(calls["takeoff"]) == 0, "no takeoff may be issued to an offline link"


def test_takeoff_unknown_vehicle_404(client, calls):
    """FIX (H4): an UNKNOWN vehicle id is a 404 — never silently routed to the
    active vehicle (a 'wrong vehicle' hazard). The REST takeoff validates the id."""
    ow, our = install_fleet()
    r = client.post("/api/command/takeoff", json={"altitude": 10, "vehicle": "ghost"})
    assert r.status_code == 404, "unknown id must 404, not fall back to active"
    assert len(calls["takeoff"]) == 0


# ── fleet safety commands (land/rtl/hold/brake) hit ALL connected ─────────────
def test_land_targets_all_connected(client, calls, monkeypatch):
    import app.api as api_mod
    monkeypatch.setattr(api_mod.coordination, "stop_all", lambda: [])
    ow, our = install_fleet(ow_connected=True, our_connected=True)
    r = client.post("/api/command/land")
    body = r.json()
    assert body["ok"] is True
    assert body["vehicles"] == {"overwatch": "ok", "outrider": "ok"}
    assert len(calls["land"]) == 2


def test_land_falls_back_to_active_when_none_connected(client, calls, monkeypatch):
    import app.api as api_mod
    monkeypatch.setattr(api_mod.coordination, "stop_all", lambda: [])
    ow, our = install_fleet(ow_connected=False, our_connected=False)
    r = client.post("/api/command/land")
    assert r.json() == {"ok": True, "vehicles": {"active": "ok"}}
    assert len(calls["land"]) == 1


# ── per-vehicle routing for goto/mode is IGNORED by REST (active only) ─────────
def test_goto_only_ever_targets_active(client, calls):
    """DESIGN NOTE: /command/goto, /move, /turn, /orbit, /mode, /speed,
    /mission/start, /mission/clear take NO vehicle arg — they always hit the
    active link. The frontend can't direct them to a specific drone via REST."""
    ow, our = install_fleet()
    r = client.post("/api/command/goto", json={"lat": 47.4, "lon": 8.5, "alt": 30})
    assert r.json()["ok"] is True
    assert calls["goto"][0][0] is ow  # active = overwatch


def test_mode_unknown_rejected(client, calls, monkeypatch):
    import app.api as api_mod
    # commands.C.MODES is the validation source; ensure a bad mode is 400.
    install_fleet()
    r = client.post("/api/command/mode", json={"name": "NONSENSE"})
    assert r.status_code == 400


# ── survey commit gating ──────────────────────────────────────────────────────
def test_survey_commit_400_when_nothing_staged(client, calls):
    install_fleet()
    import app.voice as voice
    voice._pending_survey = None
    r = client.post("/api/survey/commit")
    assert r.status_code == 400


def test_survey_upload_rejected_surfaces_502(client, monkeypatch):
    ow, our = install_fleet()

    async def reject(link, wps):
        return False

    import app.api as api_mod
    monkeypatch.setattr(api_mod.missions, "upload", reject)
    # Stage a survey directly.
    import app.voice as voice
    voice._pending_survey = {"label": "x", "polygon": [
        (47.0, 8.0), (47.001, 8.0), (47.001, 8.001), (47.0, 8.001)],
        "vehicle": None, "altitude": 30.0}
    r = client.post("/api/survey/commit")
    assert r.status_code == 502


# ── H1/H2: REST capability guards for Outrider (DDS-bridge vehicle) ─────────────
def test_rest_turn_uses_reposition_for_active_outrider(client, calls, monkeypatch):
    """/command/turn targets the active vehicle. With Outrider active (no GCS
    OFFBOARD) it must route to commands.turn_to_heading (DO_REPOSITION yaw) and
    succeed — NOT the offboard commands.turn (which would strand it), and no 422."""
    install_fleet(active="outrider")
    offboard, reposition = [], []
    async def fake_turn(link, degrees, direction):
        offboard.append((degrees, direction))
        return {}
    async def fake_turn_to_heading(link, degrees, direction):
        reposition.append((degrees, direction))
        return {"from_heading": 0, "to_heading": 90, "direction": direction, "via": "reposition"}
    import app.api as api_mod
    monkeypatch.setattr(api_mod.commands, "turn", fake_turn)
    monkeypatch.setattr(api_mod.commands, "turn_to_heading", fake_turn_to_heading)
    r = client.post("/api/command/turn", json={"degrees": 90, "direction": "right"})
    assert r.status_code == 200
    assert offboard == [], "offboard commands.turn must NOT run for Outrider"
    assert reposition == [(90, "right")], "turn must route to turn_to_heading for Outrider"


def test_rest_turn_allowed_for_active_overwatch(client, calls, monkeypatch):
    install_fleet(active="overwatch")
    turned = []
    async def fake_turn(link, degrees, direction):
        turned.append((degrees, direction))
        return {"from_heading": 0, "to_heading": 90, "direction": direction}
    import app.api as api_mod
    monkeypatch.setattr(api_mod.commands, "turn", fake_turn)
    r = client.post("/api/command/turn", json={"degrees": 90, "direction": "right"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert turned == [(90.0, "right")]


def test_rest_mode_offboard_refused_for_active_outrider(client, calls):
    """H1: /command/mode OFFBOARD on an active Outrider is refused (422) and
    set_mode is never called; a normal mode (HOLD) still passes through."""
    install_fleet(active="outrider")
    r = client.post("/api/command/mode", json={"name": "OFFBOARD"})
    assert r.status_code == 422
    assert calls["set_mode"] == [], "OFFBOARD must NOT be relayed to Outrider"
    r2 = client.post("/api/command/mode", json={"name": "HOLD"})
    assert r2.status_code == 200
    assert [name for _l, name in calls["set_mode"]] == ["HOLD"]


def test_rest_mode_offboard_allowed_for_overwatch(client, calls):
    install_fleet(active="overwatch")
    r = client.post("/api/command/mode", json={"name": "OFFBOARD"})
    assert r.status_code == 200
    assert [name for _l, name in calls["set_mode"]] == ["OFFBOARD"]


def test_rest_survey_refused_for_active_outrider(client, calls):
    """H2: /survey on an active Outrider is refused (422) BEFORE any upload — the
    DDS bridge can't run MISSION_*. missions.upload must never be called."""
    install_fleet(active="outrider")
    r = client.post("/api/survey", json={
        "polygon": [(47.0, 8.0), (47.001, 8.0), (47.001, 8.001), (47.0, 8.001)],
        "altitude": 30})
    assert r.status_code == 422
    assert calls["upload"] == [], "no mission upload to a DDS-bridge vehicle"


def test_rest_mission_start_refused_for_active_outrider_does_not_arm(client, calls):
    """H2: /mission/start on an active Outrider must 422 up front — it must NOT
    arm + MISSION_START a drone whose bridge can't run a mission."""
    install_fleet(active="outrider")
    r = client.post("/api/mission/start")
    assert r.status_code == 422
    assert calls["start"] == [], "MISSION_START must NOT be sent to Outrider"


def test_rest_mission_clear_refused_for_active_outrider(client, calls):
    install_fleet(active="outrider")
    r = client.post("/api/mission/clear")
    assert r.status_code == 422
    assert calls["clear"] == []


def test_rest_survey_stage_refused_for_outrider(client, calls):
    """H2: staging a survey explicitly for Outrider is refused (422)."""
    install_fleet(active="overwatch")
    r = client.post("/api/survey/stage", json={
        "polygon": [(47.0, 8.0), (47.001, 8.0), (47.001, 8.001), (47.0, 8.001)],
        "label": "x", "vehicle": "outrider", "altitude": 30})
    assert r.status_code == 422


def test_rest_coordinated_survey_excludes_outrider(client, calls):
    """H2: /survey/coordinated with both connected surveys with Overwatch ONLY —
    Outrider is excluded from the zone split (reported in `excluded`)."""
    install_fleet(ow_connected=True, our_connected=True, active="overwatch")
    r = client.post("/api/survey/coordinated", json={
        "polygon": [(47.0, 8.0), (47.002, 8.0), (47.002, 8.003), (47.0, 8.003)],
        "altitude": 30, "line_spacing_m": 25})
    assert r.status_code == 200
    body = r.json()
    assert body["excluded"] == ["outrider"]
    flown = {a["vehicle"] for a in body["assignments"] if not a.get("error")}
    assert "outrider" not in flown


def test_rest_coordinated_survey_explicit_outrider_422(client, calls):
    """H2: explicitly asking /survey/coordinated to use Outrider is a clear 422,
    not a silent drop."""
    install_fleet(ow_connected=True, our_connected=True, active="overwatch")
    r = client.post("/api/survey/coordinated", json={
        "polygon": [(47.0, 8.0), (47.002, 8.0), (47.002, 8.003), (47.0, 8.003)],
        "vehicles": ["outrider"], "altitude": 30})
    assert r.status_code == 422
    assert "Outrider" in r.json()["detail"]


def test_rest_coordinated_survey_vertical_sep_is_15m_horizontal_stays_small(client, calls):
    """preflight-02 F2: a 2-drone coordinated survey deconflicts VERTICALLY by a
    firm >= 15 m (operator-approved), regardless of the requested min_separation_m,
    while the HORIZONTAL zone corridor stays driven by min_separation_m (small, so
    the lawnmower lanes don't waste coverage). The two are decoupled."""
    # Both mission-capable so BOTH get a survey zone + altitude.
    install_fleet(ow_connected=True, our_connected=True, outrider_caps=True,
                  active="overwatch")
    r = client.post("/api/survey/coordinated", json={
        # A large region so a 2-way split is geometrically valid.
        "polygon": [(47.0, 8.0), (47.01, 8.0), (47.01, 8.01), (47.0, 8.01)],
        "altitude": 30, "line_spacing_m": 25,
        # Small requested separation → drives the HORIZONTAL corridor only.
        "min_separation_m": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    alts = {a["vehicle"]: a["altitude"] for a in body["assignments"]
            if a.get("altitude") is not None}
    assert set(alts) == {"overwatch", "outrider"}
    # VERTICAL: Overwatch above Outrider by a firm >= 15 m even though the request
    # asked for min_separation_m=5 (that 5 m governs the horizontal corridor, NOT
    # the altitude floor).
    assert alts["overwatch"] > alts["outrider"]
    assert alts["overwatch"] - alts["outrider"] >= 15.0, (
        f"vertical separation collapsed to {alts['overwatch'] - alts['outrider']} m")


# ── C2/H3: staggered fleet takeoff, connected-only, per-vehicle result ─────────
def test_takeoff_single_returns_per_vehicle_and_altitude(client, calls):
    """A single-drone takeoff issues to that drone and reports {ok, altitude}."""
    ow, our = install_fleet()
    r = client.post("/api/command/takeoff", json={"altitude": 12, "vehicle": "outrider"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["vehicle"] == "outrider"
    assert body["altitude"] == 12
    assert calls["takeoff"] == [(our, 12)]


def test_takeoff_single_offline_named_409(client, calls):
    """A named but OFFLINE drone must 409 — never fly an offline link."""
    ow, our = install_fleet(our_connected=False)
    r = client.post("/api/command/takeoff", json={"altitude": 10, "vehicle": "outrider"})
    assert r.status_code == 409
    assert len(calls["takeoff"]) == 0


def test_takeoff_fleet_staggers_altitudes_overwatch_higher(client, calls):
    """C2: 'all'/'both' takeoff must STAGGER altitudes — Overwatch higher, with
    >= 15 m vertical separation — never the same altitude for two drones launching
    ~10 m apart. Per-vehicle results carry the assigned altitudes."""
    ow, our = install_fleet(ow_connected=True, our_connected=True)
    r = client.post("/api/command/takeoff", json={"altitude": 30, "vehicle": "all"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    ow_alt = body["vehicles"]["overwatch"]["altitude"]
    our_alt = body["vehicles"]["outrider"]["altitude"]
    assert ow_alt > our_alt, "Overwatch must be assigned the higher band"
    assert ow_alt - our_alt >= 15.0, "must keep >= 15 m vertical separation"
    # Each drone got commanded to ITS OWN assigned altitude (not a shared value).
    by_link = {link: alt for link, alt in calls["takeoff"]}
    assert by_link[ow] == ow_alt and by_link[our] == our_alt
    assert ow_alt != our_alt


def test_takeoff_fleet_only_commands_connected(client, calls):
    """C2/H3: with one drone offline, fleet takeoff commands only the connected
    one (and doesn't stagger against a drone that isn't flying)."""
    ow, our = install_fleet(ow_connected=True, our_connected=False)
    r = client.post("/api/command/takeoff", json={"altitude": 20, "vehicle": "both"})
    assert r.status_code == 200
    body = r.json()
    assert set(body["vehicles"].keys()) == {"overwatch"}
    assert [link for link, _ in calls["takeoff"]] == [ow]


def test_takeoff_consumes_dict_return_arm_denied_reports_false(client, calls, monkeypatch):
    """H3: when commands.takeoff returns {ok, reason} (arm denied during takeoff),
    the endpoint surfaces ok:false + the reason instead of a blanket success."""
    ow, our = install_fleet()

    async def takeoff_arm_denied(link, alt=10.0, override=False):
        return {"ok": False, "reason": "Arming denied: preflight"}

    import app.api as api_mod
    monkeypatch.setattr(api_mod.commands, "takeoff", takeoff_arm_denied)
    r = client.post("/api/command/takeoff", json={"altitude": 10, "vehicle": "overwatch"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "preflight" in (body.get("reason") or "")


# ── fleet safety commands accept an optional `vehicle` (frontend sends 'all') ──
def test_hold_all_targets_every_connected(client, calls, monkeypatch):
    import app.api as api_mod
    monkeypatch.setattr(api_mod.coordination, "stop_all", lambda: [])
    ow, our = install_fleet(ow_connected=True, our_connected=True)
    r = client.post("/api/command/hold", params={"vehicle": "all"})
    body = r.json()
    assert body["ok"] is True
    assert body["vehicles"] == {"overwatch": "ok", "outrider": "ok"}
    assert len(calls["hold"]) == 2


def test_rtl_named_targets_only_that_drone(client, calls, monkeypatch):
    import app.api as api_mod
    monkeypatch.setattr(api_mod.coordination, "stop_all", lambda: [])
    ow, our = install_fleet()
    r = client.post("/api/command/rtl", params={"vehicle": "outrider"})
    body = r.json()
    assert body["ok"] is True and body["vehicles"] == {"outrider": "ok"}
    assert calls["rtl"] == [our]


def test_land_unknown_vehicle_404(client, calls, monkeypatch):
    import app.api as api_mod
    monkeypatch.setattr(api_mod.coordination, "stop_all", lambda: [])
    install_fleet()
    r = client.post("/api/command/land", params={"vehicle": "ghost"})
    assert r.status_code == 404


# ── H5: survey commit flies the staged spacing + fleet plan ───────────────────
def test_survey_commit_uses_staged_line_spacing(client, calls, monkeypatch):
    """H5: /survey/commit must fly the PREVIEWED grid — the staged line_spacing_m,
    not a hardcoded 25 m. We capture the spacing plan_survey is called with."""
    ow, our = install_fleet()
    seen: dict = {}

    import app.api as api_mod

    real_plan = api_mod.plan_survey

    def spy_plan(polygon, altitude=30.0, line_spacing_m=25.0, *a, **k):
        seen["spacing"] = line_spacing_m
        return real_plan(polygon, altitude, line_spacing_m, *a, **k)

    monkeypatch.setattr(api_mod, "plan_survey", spy_plan)
    import app.voice as voice
    voice._pending_fleet_survey = None
    voice._pending_survey = {"label": "x", "polygon": [
        (47.0, 8.0), (47.002, 8.0), (47.002, 8.002), (47.0, 8.002)],
        "vehicle": None, "altitude": 30.0, "line_spacing_m": 12.0}
    r = client.post("/api/survey/commit")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert seen["spacing"] == 12.0, "commit must use the staged spacing, not 25 m"


def test_survey_commit_flies_staged_fleet_survey(client, calls):
    """H5: a staged FLEET/region survey is flown per-drone by commit (not ignored
    in favor of a stale single-region plan)."""
    ow, our = install_fleet(ow_connected=True, our_connected=True)
    import app.voice as voice
    voice._pending_survey = None
    poly = [[47.0, 8.0], [47.002, 8.0], [47.002, 8.002], [47.0, 8.002]]
    voice._pending_fleet_survey = {"label": "Sector 1", "zones": [
        {"vehicle": "overwatch", "name": "Overwatch", "polygon": poly,
         "altitude": 45.0, "line_spacing_m": 25.0},
        {"vehicle": "outrider", "name": "Outrider", "polygon": poly,
         "altitude": 30.0, "line_spacing_m": 25.0},
    ]}
    r = client.post("/api/survey/commit")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["surveying"] == "Sector 1"
    assert len(body["assignments"]) == 2
    # Both drones got a mission uploaded + started.
    assert len(calls["upload"]) == 2 and len(calls["start"]) == 2
    # The fleet slot is consumed.
    assert voice._pending_fleet_survey is None


# ── C1 AUTH: optional shared token gates the command surface ──────────────────
def _token_client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_no_token_allows_everything(client, calls):
    """Default (api_token unset) — the surface is open, every request passes."""
    import app.config as cfg
    assert cfg.settings.api_token == ""  # default
    install_fleet()
    r = client.post("/api/command/arm")
    assert r.status_code == 200


def test_token_set_rejects_missing(monkeypatch, calls):
    """When api_token is set, a request with NO token is 401."""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "api_token", "s3cret")
    install_fleet()
    c = _token_client()
    r = c.post("/api/command/arm")
    assert r.status_code == 401


def test_token_set_accepts_matching_header(monkeypatch, calls):
    """A matching X-API-Token header passes."""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "api_token", "s3cret")
    install_fleet()
    c = _token_client()
    r = c.post("/api/command/arm", headers={"X-API-Token": "s3cret"})
    assert r.status_code == 200


def test_token_set_accepts_query_param(monkeypatch, calls):
    """A matching ?token= query param passes (for EventSource/SSE which can't set
    headers)."""
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "api_token", "s3cret")
    install_fleet()
    c = _token_client()
    r = c.post("/api/command/arm", params={"token": "s3cret"})
    assert r.status_code == 200


def test_token_set_rejects_wrong(monkeypatch, calls):
    import app.config as cfg
    monkeypatch.setattr(cfg.settings, "api_token", "s3cret")
    install_fleet()
    c = _token_client()
    r = c.post("/api/command/arm", headers={"X-API-Token": "nope"})
    assert r.status_code == 401
