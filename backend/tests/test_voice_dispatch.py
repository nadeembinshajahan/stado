"""Functional tests for the voice tool dispatcher (app/voice.dispatch).

NO HARDWARE / NO CLOUD: stubs heavy deps, replaces the registry with fake
links, and monkeypatches the mavlink command layer + onboard_track + recorder so
each tool's REAL effect is observable. The focus is intent-vs-implementation:
does each declared tool actually reach the right vehicle and report truthfully?

Run: cd backend && PYTHONPATH=. uv run python -m pytest tests/test_voice_dispatch.py -q
Deps: pytest, pytest-asyncio (for @pytest.mark.asyncio) OR asyncio.run.  These
tests use asyncio.run so NO extra plugin is required.
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

import app.voice as voice  # noqa: E402
from app.mavlink import commands as mav_commands  # noqa: E402
from app.mavlink.registry import Vehicle, registry  # noqa: E402


class FakeLink:
    def __init__(self, connected=True, **state):
        base = {"connected": connected, "armed": False, "mode": "HOLD",
                "lat": 47.397, "lon": 8.545, "alt_rel": 20.0, "alt_msl": 520.0,
                "heading": 0.0, "groundspeed": 1.0, "battery_pct": 88,
                "gps_fix": 3, "satellites": 12}
        base.update(state)
        self._state = base
        self.connection_string = "fake://link"

    def snapshot(self):
        return dict(self._state)


def install_fleet(ow_connected=True, our_connected=True, active="overwatch",
                  outrider_caps=False):
    """Build a 2-vehicle fake fleet. Overwatch is a full MAVLink vehicle (offboard
    + missions). Outrider is a DDS-bridge vehicle: it does NOT support GCS OFFBOARD
    (its tracking/yaw is closed onboard), and by default (outrider_caps=False) this
    fixture also leaves MISSION_* off so the EXCLUSION mechanism can still be
    exercised in isolation. NOTE: in PRODUCTION Outrider is now MISSION-capable
    (the Jetson bridge runs missions onboard) — see config.fleet and
    test_production_fleet_outrider_is_mission_capable. Pass outrider_caps=True to
    give Outrider full capabilities for a 2nd mission/offboard-capable drone."""
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


async def _noop_sleep(*_a, **_k):
    """Replacement for asyncio.sleep that does NOT recurse into a patched name."""
    return None


@pytest.fixture(autouse=True)
def reset_state():
    voice._pending_survey = None
    voice._pending_fleet_survey = None
    voice._last_perimeters = []
    voice._outrider_follow_profile = None
    yield


@pytest.fixture
def rec(monkeypatch):
    r: dict[str, list] = {k: [] for k in (
        "arm", "disarm", "takeoff", "land", "rtl", "hold", "set_mode",
        "goto", "orbit", "completion")}

    async def fake_arm(link, force=False, timeout=3.0):
        r["arm"].append(link)
        return {"ok": True, "armed": True, "result": 0, "result_name": "ACCEPTED",
                "reason": None, "statustexts": []}

    async def fake_disarm(link, force=False, timeout=3.0):
        r["disarm"].append(link)
        return {"ok": True, "armed": False, "result": 0, "result_name": "ACCEPTED",
                "reason": None, "statustexts": []}

    async def fake_takeoff(link, alt=10.0, override=False):
        r["takeoff"].append((link, alt))

    async def fake_land(link):
        r["land"].append(link)

    async def fake_rtl(link):
        r["rtl"].append(link)

    async def fake_hold(link):
        r["hold"].append(link)

    async def fake_set_mode(link, name):
        r["set_mode"].append((link, name))

    async def fake_goto(link, lat, lon, alt, speed=-1.0, override=False):
        r["goto"].append((link, lat, lon, alt))

    async def fake_orbit(link, lat, lon, alt, radius=20.0, velocity=3.0, clockwise=True, override=False):
        r["orbit"].append((link, lat, lon, radius))

    for n, f in (("arm", fake_arm), ("disarm", fake_disarm), ("takeoff", fake_takeoff),
                 ("land", fake_land), ("rtl", fake_rtl), ("hold", fake_hold),
                 ("set_mode", fake_set_mode), ("goto", fake_goto), ("orbit", fake_orbit)):
        monkeypatch.setattr(voice.commands, n, f)

    # Completion module spawns asyncio tasks; stub each to just record.
    for cname in ("takeoff", "land", "rtl", "goto", "orbit"):
        monkeypatch.setattr(voice.completion, cname,
                            lambda *a, _c=cname, **k: r["completion"].append(_c))
    return r


# ── voice auto-start wires the map-objects geolocation loop ───────────────────
def test_ensure_pipeline_starts_map_objects_loop(monkeypatch):
    """BUGFIX: a target acquired via the VOICE agent (not REST /api/vision/start)
    never appeared on the map because _ensure_pipeline started the pipeline + wired
    on_setpoint but NEVER started the map-objects loop — the ONLY event carrying a
    geolocated lat/lon. _ensure_pipeline must now call start_map_objects_task, like
    the REST start handler does, so voice tracking pins to the map."""
    import app.vision_api as VA

    class _FakePipe:
        _running = False
        def __init__(self):
            self.on_setpoint = None
            self.started = False
        def start(self):
            self.started = True
            self._running = True

    fake = _FakePipe()
    monkeypatch.setattr(voice, "get_pipeline", lambda: None)  # nothing running yet
    monkeypatch.setattr(voice, "init_pipeline", lambda src, model: fake)
    started = {"n": 0}
    monkeypatch.setattr(VA, "start_map_objects_task",
                        lambda: started.__setitem__("n", started["n"] + 1))

    pipe = voice._ensure_pipeline()
    assert pipe is fake and fake.started, "pipeline must be started"
    assert pipe.on_setpoint is VA._on_setpoint, "follow setpoint sink must be wired"
    assert started["n"] == 1, "voice auto-start must schedule the map-objects loop"


def test_ensure_pipeline_running_does_not_restart_map_objects(monkeypatch):
    """If a pipeline is ALREADY running, _ensure_pipeline returns it untouched and
    does NOT (re)start the loop — the REST start already owns it (no double-start)."""
    import app.vision_api as VA

    class _RunningPipe:
        _running = True

    running = _RunningPipe()
    monkeypatch.setattr(voice, "get_pipeline", lambda: running)
    started = {"n": 0}
    monkeypatch.setattr(VA, "start_map_objects_task",
                        lambda: started.__setitem__("n", started["n"] + 1))
    assert voice._ensure_pipeline() is running
    assert started["n"] == 0, "a running pipeline must not re-trigger the loop"


# ── select_vehicle ────────────────────────────────────────────────────────────
def test_select_vehicle_sets_active(rec):
    install_fleet()
    res = run(voice.dispatch("select_vehicle", {"name": "outrider"}))
    assert res["ok"] and res["active"] == "outrider"


def test_select_vehicle_unknown(rec):
    install_fleet()
    res = run(voice.dispatch("select_vehicle", {"name": "banana"}))
    assert res["ok"] is False


# ── arm routing + truthful denial ─────────────────────────────────────────────
def test_arm_named_routes_correctly(rec):
    ow, our = install_fleet()
    res = run(voice.dispatch("arm", {"vehicle": "outrider"}))
    assert res["ok"] and res["armed"] is True and res["vehicle"] == "Outrider"
    assert rec["arm"] == [our]


def test_arm_denied_reports_reason(rec, monkeypatch):
    install_fleet()

    async def deny(link, force=False, timeout=3.0):
        return {"ok": False, "armed": None, "result": 2, "result_name": "DENIED",
                "reason": "Arming denied: no GPS", "statustexts": []}

    monkeypatch.setattr(voice.commands, "arm", deny)
    res = run(voice.dispatch("arm", {}))
    assert res["ok"] is False and "no GPS" in res["note"]


def test_arm_unknown_vehicle(rec):
    install_fleet()
    res = run(voice.dispatch("arm", {"vehicle": "zzz"}))
    assert res["ok"] is False and "unknown vehicle" in res["error"]


# ── arm_check: arm → wait → disarm, and FINDING about offline single targets ──
def test_arm_check_arms_then_disarms(rec, monkeypatch):
    ow, our = install_fleet()
    monkeypatch.setattr(voice.asyncio, "sleep", _noop_sleep)
    res = run(voice.dispatch("arm_check", {"vehicle": "overwatch"}))
    assert res["ok"] is True
    assert rec["arm"] == [ow] and rec["disarm"] == [ow]


def test_arm_check_aborts_and_disarms_already_armed_on_denial(rec, monkeypatch):
    """Fleet arm_check: if the 2nd drone's arm is denied, the 1st (already armed)
    must be disarmed and the check reported as failed — never leave one armed."""
    ow, our = install_fleet()
    monkeypatch.setattr(voice.asyncio, "sleep", _noop_sleep)
    seq = {"n": 0}

    async def arm_first_ok_then_deny(link, force=False, timeout=3.0):
        rec["arm"].append(link)
        seq["n"] += 1
        if seq["n"] == 1:
            return {"ok": True, "armed": True, "result": 0, "result_name": "ACCEPTED",
                    "reason": None, "statustexts": []}
        return {"ok": False, "armed": None, "result": 2, "result_name": "DENIED",
                "reason": "denied", "statustexts": []}

    monkeypatch.setattr(voice.commands, "arm", arm_first_ok_then_deny)
    res = run(voice.dispatch("arm_check", {"vehicle": "all"}))
    assert res["ok"] is False
    assert rec["disarm"] == [ow], "the already-armed drone must be disarmed on abort"


def test_arm_check_single_offline_target_returns_offline(rec, monkeypatch):
    """FIX (M7): arm_check now connectivity-checks a NAMED/active target too, not
    just 'all'/'both'. An offline named drone returns a clean 'offline' and is
    NEVER passed to commands.arm (no multi-second timeout, no false 'arm denied')."""
    ow, our = install_fleet(our_connected=False)
    monkeypatch.setattr(voice.asyncio, "sleep", _noop_sleep)
    res = run(voice.dispatch("arm_check", {"vehicle": "outrider"}))
    assert res["ok"] is False and "offline" in res["error"]
    assert rec["arm"] == [], "an OFFLINE named vehicle must NOT be armed"


def test_arm_check_all_no_connected(rec):
    install_fleet(ow_connected=False, our_connected=False)
    res = run(voice.dispatch("arm_check", {"vehicle": "all"}))
    assert res["ok"] is False and "no connected" in res["error"]


# ── takeoff / land / rtl route + spawn completion ────────────────────────────
def test_takeoff_routes_and_arms_via_commands(rec):
    ow, our = install_fleet()
    res = run(voice.dispatch("takeoff", {"vehicle": "outrider", "altitude_m": 25}))
    assert res["ok"] is True
    assert rec["takeoff"] and rec["takeoff"][0][0] is our
    assert "takeoff" in rec["completion"]


def test_takeoff_arm_denied_reports_failure_and_no_completion(rec, monkeypatch):
    """SAFETY GUARD (voice takeoff): commands.takeoff gates NAV_TAKEOFF on a
    confirmed arm and returns {ok, reason}; when arming is DENIED it does NOT
    launch. The voice handler must honour that — report ok:false + the reason and
    NOT start a 'takeoff complete' completion watcher — instead of falsely telling
    the operator the drone is lifting off when it never armed."""
    ow, our = install_fleet(active="overwatch")

    async def fake_takeoff_denied(link, alt=10.0, override=False):
        rec["takeoff"].append((link, alt))
        return {"ok": False, "armed": False, "result": 2, "result_name": "DENIED",
                "reason": "Arming denied: GPS fix required", "altitude": alt}

    monkeypatch.setattr(voice.commands, "takeoff", fake_takeoff_denied)
    res = run(voice.dispatch("takeoff", {"vehicle": "overwatch", "altitude_m": 30}))
    assert res["ok"] is False, "arm-denied takeoff must NOT report success"
    assert "GPS fix required" in (res.get("reason") or ""), "must surface PX4 reason"
    assert "takeoff" not in rec["completion"], (
        "must NOT start a completion watcher for a drone that never armed"
    )


def test_land_routes_to_active(rec):
    ow, our = install_fleet(active="outrider")
    # active auto-resolves: both connected, set-active=outrider connected -> outrider
    res = run(voice.dispatch("land", {}))
    assert res["ok"] is True and rec["land"] == [our]


# ── get_status truthfulness ───────────────────────────────────────────────────
def test_get_status_offline_returns_no_link(rec):
    install_fleet(our_connected=False)
    res = run(voice.dispatch("get_status", {"vehicle": "outrider"}))
    assert res["connected"] is False and res["link"] == "NO LINK"
    assert "lat" not in res, "offline status must NOT leak stale telemetry fields"


def test_get_status_connected_returns_telemetry(rec):
    install_fleet()
    res = run(voice.dispatch("get_status", {"vehicle": "overwatch"}))
    assert res["connected"] is True and res["battery_pct"] == 88


# ── record tool ───────────────────────────────────────────────────────────────
def test_record_start_routes_to_active_stream(rec, monkeypatch):
    install_fleet(active="overwatch")
    started = []
    monkeypatch.setattr(voice.recorder, "start",
                        lambda stream: (started.append(stream) or {"ok": True}))
    res = run(voice.dispatch("record", {"action": "start"}))
    assert res["ok"] is True
    assert started == ["drone"], "active=overwatch maps to go2rtc stream 'drone'"


def test_record_both_streams(rec, monkeypatch):
    install_fleet()
    started = []
    monkeypatch.setattr(voice.recorder, "start",
                        lambda stream: (started.append(stream) or {"ok": True}))
    res = run(voice.dispatch("record", {"action": "start", "vehicle": "both"}))
    assert res["ok"] is True and set(started) == {"drone", "outrider"}


def test_record_start_failure_surfaces_error(rec, monkeypatch):
    install_fleet()
    monkeypatch.setattr(voice.recorder, "start",
                        lambda stream: {"ok": False, "error": "ffmpeg not found on PATH"})
    res = run(voice.dispatch("record", {"action": "start", "vehicle": "overwatch"}))
    assert res["ok"] is False and "ffmpeg" in res["error"]


def test_record_stop_reports_not_recording(rec, monkeypatch):
    install_fleet()
    monkeypatch.setattr(voice.recorder, "is_recording", lambda s: False)
    monkeypatch.setattr(voice.recorder, "stop", lambda s: {"ok": True, "recording": False})
    res = run(voice.dispatch("record", {"action": "stop", "vehicle": "overwatch"}))
    assert res["ok"] is True and "Overwatch" in res["not_recording"]


def test_record_unknown_action(rec):
    install_fleet()
    res = run(voice.dispatch("record", {"action": "pause"}))
    assert res["ok"] is False and "unknown record action" in res["error"]


def test_record_unknown_vehicle(rec):
    install_fleet()
    res = run(voice.dispatch("record", {"action": "start", "vehicle": "ghost"}))
    assert res["ok"] is False


# ── onboard track/follow (Outrider) routes over UDP seam ──────────────────────
def test_track_target_outrider_seeds_onboard(rec, monkeypatch):
    install_fleet(active="outrider")

    async def fake_frame():
        return b"x" * 5000

    async def fake_resolve(jpeg, desc, backend="qwen"):
        return [0.2, 0.2, 0.5, 0.6]  # x0,y0,x1,y1

    monkeypatch.setattr(voice, "grab_outrider_frame", fake_frame)
    monkeypatch.setattr(voice.grounding, "resolve_target", fake_resolve)
    seeded = {}
    monkeypatch.setattr(voice.onboard_track, "seed",
                        lambda x, y, w, h: (seeded.update(dict(x=x, y=y, w=w, h=h)) or {"ok": True}))
    profiles = []
    monkeypatch.setattr(voice.onboard_track, "set_profile",
                        lambda p: (profiles.append(p) or {"ok": True, "profile": "car"}))
    res = run(voice.dispatch("track_target", {"vehicle": "outrider", "description": "the truck"}))
    assert res["ok"] is True
    assert abs(seeded["w"] - 0.3) < 1e-6 and abs(seeded["h"] - 0.4) < 1e-6
    # "the truck" → car class is auto-inferred, the PROFILE pre-armed onboard,
    # and remembered so a later `follow` flies the car envelope.
    assert profiles == ["car"]
    assert voice._outrider_follow_profile == "car"
    assert res.get("profile") == "car"


def test_track_target_outrider_no_frame(rec, monkeypatch):
    install_fleet(active="outrider")

    async def no_frame():
        return None

    monkeypatch.setattr(voice, "grab_outrider_frame", no_frame)
    res = run(voice.dispatch("track_target", {"vehicle": "outrider", "description": "x"}))
    assert res["ok"] is False and "not reachable" in res["error"]


def test_follow_outrider_routes_to_onboard(rec, monkeypatch):
    install_fleet(active="outrider")
    calls = []
    monkeypatch.setattr(voice.onboard_track, "follow",
                        lambda enable, profile=None: (calls.append((enable, profile))
                                                      or {"ok": True, "profile": profile}))
    res = run(voice.dispatch("follow", {"vehicle": "outrider", "enable": True}))
    # No prior track_target ⇒ no inferred profile ⇒ onboard default (None passed).
    assert res["follow"] is True and calls == [(True, None)]


def test_follow_outrider_uses_inferred_profile_from_track(rec, monkeypatch):
    """The class from track_target ('follow the car') auto-selects the follow
    speed envelope: a later `follow` (no description) flies the car profile."""
    install_fleet(active="outrider")

    async def fake_frame():
        return b"x" * 5000

    async def fake_resolve(jpeg, desc, backend="qwen"):
        return [0.2, 0.2, 0.5, 0.6]

    monkeypatch.setattr(voice, "grab_outrider_frame", fake_frame)
    monkeypatch.setattr(voice.grounding, "resolve_target", fake_resolve)
    monkeypatch.setattr(voice.onboard_track, "seed", lambda x, y, w, h: {"ok": True})
    monkeypatch.setattr(voice.onboard_track, "set_profile",
                        lambda p: {"ok": True, "profile": "car"})
    calls = []
    monkeypatch.setattr(voice.onboard_track, "follow",
                        lambda enable, profile=None: (calls.append((enable, profile))
                                                      or {"ok": True, "profile": profile}))
    run(voice.dispatch("track_target",
                       {"vehicle": "outrider", "description": "the white car"}))
    res = run(voice.dispatch("follow", {"vehicle": "outrider", "enable": True}))
    assert res["follow"] is True and calls == [(True, "car")]
    # disabling never carries a profile
    run(voice.dispatch("follow", {"vehicle": "outrider", "enable": False}))
    assert calls[-1] == (False, None)


# ── H1: GCS capability guards — turn / OFFBOARD refused for Outrider ────────────
def test_turn_routes_to_reposition_for_outrider(monkeypatch):
    """Outrider has no GCS OFFBOARD, so 'turn' must NOT use the offboard yaw-rate
    commands.turn (which would strand it). It routes to commands.turn_to_heading
    (DO_REPOSITION yaw), which rides the same command path as goto/orbit over DDS."""
    install_fleet(active="outrider")
    offboard_called = []
    reposition_called = []

    async def fake_turn(link, degrees, direction):
        offboard_called.append((degrees, direction))
        return {}

    async def fake_turn_to_heading(link, degrees, direction):
        reposition_called.append((degrees, direction))
        return {"from_heading": 0, "to_heading": 90, "direction": direction, "via": "reposition"}

    monkeypatch.setattr(voice.commands, "turn", fake_turn)
    monkeypatch.setattr(voice.commands, "turn_to_heading", fake_turn_to_heading)
    res = run(voice.dispatch("turn", {"vehicle": "outrider", "degrees": 90, "direction": "right"}))
    assert res["ok"] is True
    assert offboard_called == [], "offboard commands.turn must NOT run for Outrider"
    assert reposition_called == [(90, "right")], "turn must route to turn_to_heading for Outrider"


def test_turn_allowed_for_overwatch(monkeypatch):
    """Overwatch (full MAVLink) keeps full turn capability."""
    install_fleet(active="overwatch")
    called = []

    async def fake_turn(link, degrees, direction):
        called.append((degrees, direction))
        return {"from_heading": 0, "to_heading": 90, "direction": direction}

    monkeypatch.setattr(voice.commands, "turn", fake_turn)
    res = run(voice.dispatch("turn", {"vehicle": "overwatch", "degrees": 90, "direction": "left"}))
    assert res["ok"] is True
    assert called == [(90.0, "left")]


def test_set_mode_offboard_refused_for_outrider(rec, monkeypatch):
    """H1: never DO_SET_MODE→OFFBOARD a DDS-bridge Outrider — refuse it; set_mode
    must NOT be called. A non-OFFBOARD mode (HOLD) is still allowed for Outrider."""
    install_fleet(active="outrider")
    res = run(voice.dispatch("set_mode", {"vehicle": "outrider", "mode": "OFFBOARD"}))
    assert res["ok"] is False and res["capability"] == "offboard"
    assert rec["set_mode"] == [], "OFFBOARD must NOT be relayed to Outrider"
    # a normal mode still works on Outrider
    res2 = run(voice.dispatch("set_mode", {"vehicle": "outrider", "mode": "HOLD"}))
    assert res2["ok"] is True
    assert [name for _l, name in rec["set_mode"]] == ["HOLD"]


def test_set_mode_offboard_allowed_for_overwatch(rec):
    """Overwatch may be commanded into OFFBOARD (full MAVLink autopilot)."""
    install_fleet(active="overwatch")
    res = run(voice.dispatch("set_mode", {"vehicle": "overwatch", "mode": "OFFBOARD"}))
    assert res["ok"] is True
    assert [name for _l, name in rec["set_mode"]] == ["OFFBOARD"]


def test_gcs_follow_offboard_refused_for_a_non_offboard_vehicle(monkeypatch):
    """H1: a GCS-side (vision-pipeline) follow streams OFFBOARD setpoints. If the
    active vehicle can't take GCS OFFBOARD it must be refused and start_offboard
    never scheduled. (Outrider's ONBOARD follow is a different, supported path
    handled earlier; here we exercise the GCS-OFFBOARD branch by making Outrider
    the non-active target via the offboard guard on a non-onboard vehicle.)"""
    # Build a fleet whose ACTIVE vehicle is offboard-incapable but NOT the onboard
    # Outrider path: easiest is to make overwatch incapable for this one check.
    ow = FakeLink(connected=True)
    our = FakeLink(connected=True)
    registry._vehicles = {
        "overwatch": Vehicle("overwatch", "Overwatch", "hex", ow,
                             supports_offboard=False, supports_missions=True),
        "outrider": Vehicle("outrider", "Outrider", "quad", our,
                            supports_offboard=False, supports_missions=False),
    }
    registry._order = ["overwatch", "outrider"]
    registry._active = "overwatch"

    started = []
    async def fake_start_offboard(link):
        started.append(link)
    monkeypatch.setattr(voice.commands, "start_offboard", fake_start_offboard)
    res = run(voice.dispatch("follow", {"vehicle": "overwatch", "enable": True}))
    assert res["ok"] is False and res["capability"] == "offboard"
    assert started == [], "start_offboard must NOT be scheduled for a refused follow"


# ── survey staging / confirm gating ───────────────────────────────────────────
def test_execute_survey_nothing_staged(rec):
    install_fleet()
    res = run(voice.dispatch("execute_survey", {}))
    assert res["ok"] is False and "no planned survey" in res["error"]


def test_cancel_survey_clears_both(rec):
    install_fleet()
    voice._pending_survey = {"label": "x", "polygon": [], "vehicle": None, "altitude": 30}
    res = run(voice.dispatch("cancel_survey", {}))
    assert res["ok"] is True and res["cancelled"] is True
    assert voice._pending_survey is None and voice._pending_fleet_survey is None


def test_unknown_tool(rec):
    install_fleet()
    res = run(voice.dispatch("frobnicate", {}))
    assert res["ok"] is False and "unknown tool" in res["error"]


# ── M6: a RuntimeError resolving the target must NOT escape dispatch ──────────
def test_dispatch_empty_registry_returns_error_not_raise(rec):
    """FIX (M6): registry.active_vehicle() raises RuntimeError when the registry
    is empty. That resolution now lives INSIDE dispatch's try, so dispatch returns
    {ok:false, error} instead of letting the exception tear down the voice session
    receive loop. (No `vehicle` arg → active resolution path.)"""
    registry._vehicles = {}
    registry._order = []
    registry._active = None
    res = run(voice.dispatch("hold", {}))
    assert res["ok"] is False and "error" in res


# ── M7: arm_check single-target connectivity (active path) ────────────────────
def test_arm_check_active_offline_returns_offline(rec, monkeypatch):
    """FIX (M7): the no-`vehicle` (active) arm_check path also connectivity-checks
    and returns a clean offline error without arming."""
    install_fleet(ow_connected=False, our_connected=False, active="overwatch")
    monkeypatch.setattr(voice.asyncio, "sleep", _noop_sleep)
    res = run(voice.dispatch("arm_check", {}))
    assert res["ok"] is False and "offline" in res["error"]
    assert rec["arm"] == []


# ── survey/search scale to the CONNECTED fleet count (1 connected -> 1 drone) ──
def _stub_fleet_survey(monkeypatch):
    """Capture split_rect's zone count + gap_m, plan_and_fly's sep_m, and the
    fleet_zones map push; fake the fly. The captured gap_m/sep_m let a test assert
    the VERTICAL (15 m) vs HORIZONTAL (~5 m) separation decoupling (preflight-02 F2)."""
    cap: dict = {"split_n": None, "gap_m": None, "sep_m": None, "fleet_zones": None}

    def fake_split(lat, lon, w, h, hdg, n=2, gap_m=5.0):
        cap["split_n"] = n
        cap["gap_m"] = gap_m
        return [[(0.0, 0.0), (0.0, 1.0), (1.0, 1.0)] for _ in range(n)]

    async def fake_plan_and_fly(vehicles, zones, base_alt, line_spacing_m, sep_m,
                                source_polygon=None):
        cap["sep_m"] = sep_m
        return [{"vehicle": v, "name": f"Zone {i + 1}", "polygon": [[0.0, 0.0]],
                 "altitude": 30.0 + i * 15, "waypoints": 4, "path": [[1.0, 2.0]]}
                for i, v in enumerate(vehicles)]

    def fake_publish(msg):
        if isinstance(msg, dict) and msg.get("type") == "fleet_zones":
            cap["fleet_zones"] = msg

    monkeypatch.setattr(voice.coordinated, "split_rect", fake_split)
    monkeypatch.setattr(voice.coordinated, "plan_and_fly", fake_plan_and_fly)
    monkeypatch.setattr(voice.hub, "publish_threadsafe", fake_publish)
    return cap


def test_survey_area_one_connected_uses_one_drone(monkeypatch):
    """Only Overwatch connected → survey the WHOLE area with one drone (1 zone)."""
    install_fleet(ow_connected=True, our_connected=False)
    cap = _stub_fleet_survey(monkeypatch)
    res = run(voice.dispatch("survey_area", {"size_m": 200}))
    assert res["ok"] and res["vehicles_used"] == 1, res
    assert cap["split_n"] == 1, "1 connected drone must give 1 zone (whole area)"
    assert cap["fleet_zones"] and len(cap["fleet_zones"]["zones"]) == 1
    assert cap["fleet_zones"]["zones"][0]["vehicle"] == "overwatch"
    assert cap["fleet_zones"]["zones"][0]["path"], "the drone's path is pushed to the map"


def test_survey_area_two_capable_connected_splits_and_shows_both_paths(monkeypatch):
    """Both connected AND both mission-capable → divide into 2 zones, push BOTH
    drones' paths. (outrider_caps=True gives Outrider mission capability.)"""
    install_fleet(ow_connected=True, our_connected=True, outrider_caps=True)
    cap = _stub_fleet_survey(monkeypatch)
    res = run(voice.dispatch("survey_area", {"size_m": 200}))
    assert res["ok"] and res["vehicles_used"] == 2, res
    assert cap["split_n"] == 2, "2 mission-capable drones must split into 2 zones"
    assert {z["vehicle"] for z in cap["fleet_zones"]["zones"]} == {"overwatch", "outrider"}
    assert all(z["path"] for z in cap["fleet_zones"]["zones"]), "both paths shown on map"
    # preflight-02 F2: VERTICAL separation is a firm 15 m; HORIZONTAL corridor stays
    # small (~5 m). The two are decoupled in the survey_area dispatch.
    assert cap["sep_m"] == 15.0, "survey vertical separation must be 15 m"
    assert cap["gap_m"] == 5.0, "horizontal zone corridor must stay ~5 m (not widened)"


def test_survey_area_excludes_mission_incapable_outrider(monkeypatch):
    """H2: with the PRODUCTION fleet (Outrider can't run missions), a fleet survey
    with both drones connected surveys with Overwatch ONLY — Outrider is EXCLUDED
    from the zone split (its DDS bridge can't run MISSION_*), not given a zone whose
    upload would silently time out. The exclusion is reported to the operator."""
    install_fleet(ow_connected=True, our_connected=True)  # Outrider caps False (prod)
    cap = _stub_fleet_survey(monkeypatch)
    res = run(voice.dispatch("survey_area", {"size_m": 200}))
    assert res["ok"] and res["vehicles_used"] == 1, res
    assert cap["split_n"] == 1, "only the mission-capable drone gets a zone"
    assert {z["vehicle"] for z in cap["fleet_zones"]["zones"]} == {"overwatch"}
    assert res["excluded"] == ["outrider"]
    assert res["excluded_note"] and "Outrider" in res["excluded_note"]


def test_survey_area_with_fleet_is_same_connected_count_handler(monkeypatch):
    """survey_area_with_fleet routes through the same connected-count handler.
    Mission-capable Outrider (outrider_caps=True) → 2 zones."""
    install_fleet(ow_connected=True, our_connected=True, outrider_caps=True)
    cap = _stub_fleet_survey(monkeypatch)
    res = run(voice.dispatch("survey_area_with_fleet", {"size_m": 150}))
    assert res["ok"] and res["vehicles_used"] == 2 and cap["split_n"] == 2


def test_survey_area_no_connected_drones_errors(monkeypatch):
    install_fleet(ow_connected=False, our_connected=False)
    _stub_fleet_survey(monkeypatch)
    res = run(voice.dispatch("survey_area", {"size_m": 100}))
    assert res["ok"] is False and "no connected" in res["error"]


# ── PRODUCTION CONFIG: Outrider is now MISSION-capable (un-excluded) ──────────
def test_production_fleet_outrider_is_mission_capable():
    """Outrider now flies surveys/missions via the onboard executor in the Jetson
    bridge, so the PRODUCTION fleet (config.fleet) marks it supports_missions=True
    — it is NO LONGER excluded from fleet surveys. (supports_offboard stays False:
    its tracking/yaw is still closed onboard, not via GCS OFFBOARD.) The env
    override OUTRIDER_SUPPORTS_MISSIONS=0 can still turn it back off for a deploy
    without the new bridge."""
    import importlib
    import app.config as config

    # Default (no env override) → mission-capable.
    importlib.reload(config)
    fleet = {v["id"]: v for v in config.fleet()}
    assert fleet["outrider"]["supports_missions"] is True, (
        "Outrider must be mission-capable now that the bridge runs missions onboard"
    )
    assert fleet["outrider"]["supports_offboard"] is False, (
        "Outrider still has no GCS OFFBOARD (onboard tracking) — unchanged"
    )
    assert fleet["overwatch"]["supports_missions"] is True


def test_production_fleet_outrider_missions_env_override_off(monkeypatch):
    """The env override still lets a deploy WITHOUT the new bridge disable Outrider
    missions (OUTRIDER_SUPPORTS_MISSIONS=0 → excluded from surveys again)."""
    import importlib
    import app.config as config

    monkeypatch.setenv("OUTRIDER_SUPPORTS_MISSIONS", "0")
    importlib.reload(config)
    try:
        fleet = {v["id"]: v for v in config.fleet()}
        assert fleet["outrider"]["supports_missions"] is False
    finally:
        monkeypatch.delenv("OUTRIDER_SUPPORTS_MISSIONS", raising=False)
        importlib.reload(config)


def test_survey_splits_between_overwatch_and_outrider_at_staggered_alts(monkeypatch):
    """The un-exclusion in action: with BOTH drones connected and mission-capable
    (production), a fleet survey splits into 2 zones — one per drone — and the
    deconfliction stays at the firm 15 m vertical separation."""
    install_fleet(ow_connected=True, our_connected=True, outrider_caps=True)
    cap = _stub_fleet_survey(monkeypatch)
    res = run(voice.dispatch("survey_area", {"size_m": 200}))
    assert res["ok"] and res["vehicles_used"] == 2
    assert cap["split_n"] == 2
    assert {z["vehicle"] for z in cap["fleet_zones"]["zones"]} == {"overwatch", "outrider"}
    assert res.get("excluded") in (None, [], )  # Outrider no longer excluded
    assert cap["sep_m"] == 15.0, "vertical separation stays >= 15 m"


def test_survey_area_never_splits_into_more_zones_than_connected(monkeypatch):
    """PREFLIGHT (paranoid): with exactly ONE connected drone, the area is NEVER
    divided into 2 zones — a 2-zone split for 1 drone would leave half the area
    unflown. split_rect must be called with n == connected count."""
    install_fleet(ow_connected=True, our_connected=False)
    cap = _stub_fleet_survey(monkeypatch)
    run(voice.dispatch("survey_area", {"size_m": 200}))
    assert cap["split_n"] == 1, "1 connected drone must NEVER over-split the area"


# ── survey_region (named area): PLAN+PREVIEW, scales to CONNECTED count ─────────
def _stub_region_survey(monkeypatch, region):
    """Stub regions.find + split_rect + the map push so survey_region can be driven
    offline. Returns the captured split-n and pushed fleet_zones preview."""
    cap: dict = {"split_n": None, "fleet_zones": None}

    monkeypatch.setattr(voice.regions, "find", lambda name: region)

    def fake_split(lat, lon, w, h, hdg, n=2, gap_m=5.0):
        cap["split_n"] = n
        return [[(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)] for _ in range(n)]

    def fake_plan_survey(poly, altitude=30.0, line_spacing_m=25.0):
        from app.mavlink.missions import Waypoint
        return [Waypoint(lat=0.0, lon=0.0, alt=altitude),
                Waypoint(lat=0.0, lon=1.0, alt=altitude)]

    def fake_publish(msg):
        if isinstance(msg, dict) and msg.get("type") == "fleet_zones":
            cap["fleet_zones"] = msg

    monkeypatch.setattr(voice.coordinated, "split_rect", fake_split)
    monkeypatch.setattr(voice, "plan_survey", fake_plan_survey)
    monkeypatch.setattr(voice.hub, "publish_threadsafe", fake_publish)
    return cap


_REGION = {"name": "Sector 1", "center": [12.97, 77.59], "width_m": 200.0,
           "height_m": 200.0, "heading_deg": 0.0}


def test_survey_region_two_capable_connected_splits_into_two_staggered_zones(monkeypatch):
    """Both connected AND mission-capable → 2 zones, each at a DISTINCT altitude
    (Overwatch higher, by a firm >= 15 m VERTICAL separation — preflight-02 F2).
    outrider_caps=True makes Outrider mission-capable. This drives the REAL
    plan_fleet_survey/assign_altitudes (sep_m=15), only split_rect is stubbed."""
    install_fleet(ow_connected=True, our_connected=True, outrider_caps=True)
    cap = _stub_region_survey(monkeypatch, _REGION)
    res = run(voice.dispatch("survey_region", {"name": "Sector 1"}))
    assert res["ok"] and res["planned"] is True
    assert cap["split_n"] == 2, "2 mission-capable drones → 2 zones"
    zones = cap["fleet_zones"]["zones"]
    assert {z["vehicle"] for z in zones} == {"overwatch", "outrider"}
    alts = {z["vehicle"]: z["altitude"] for z in zones}
    assert alts["overwatch"] > alts["outrider"], "Overwatch must preview HIGHER"
    assert len({round(a, 3) for a in alts.values()}) == 2, "no shared altitude band"
    assert alts["overwatch"] - alts["outrider"] >= 15.0, (
        "survey vertical separation must be a firm >= 15 m")


def test_survey_region_excludes_mission_incapable_outrider(monkeypatch):
    """H2: with the production fleet (Outrider can't run missions), a named-region
    survey with both connected is planned for Overwatch ONLY — Outrider is excluded
    from the zone split and the exclusion is reported."""
    install_fleet(ow_connected=True, our_connected=True)  # Outrider caps False (prod)
    cap = _stub_region_survey(monkeypatch, _REGION)
    res = run(voice.dispatch("survey_region", {"name": "Sector 1"}))
    assert res["ok"] and res["planned"] is True
    assert cap["split_n"] == 1, "only the mission-capable drone gets a zone"
    zones = cap["fleet_zones"]["zones"]
    assert {z["vehicle"] for z in zones} == {"overwatch"}
    assert res["excluded"] == ["outrider"]


def test_survey_region_one_connected_uses_one_full_zone(monkeypatch):
    """Only one connected → ONE zone over the whole region (never split for 1)."""
    install_fleet(ow_connected=True, our_connected=False)
    cap = _stub_region_survey(monkeypatch, _REGION)
    res = run(voice.dispatch("survey_region", {"name": "Sector 1"}))
    assert res["ok"]
    assert cap["split_n"] == 1, "1 connected drone → 1 zone (whole region)"
    assert len(cap["fleet_zones"]["zones"]) == 1


def test_survey_region_none_connected_previews_single_zone(monkeypatch):
    """No drones connected → a single-zone PREVIEW (not a split among every
    registered drone). Never more preview zones than 1 when nothing is up."""
    install_fleet(ow_connected=False, our_connected=False)
    cap = _stub_region_survey(monkeypatch, _REGION)
    res = run(voice.dispatch("survey_region", {"name": "Sector 1"}))
    assert res["ok"]
    assert cap["split_n"] == 1, "no connected drones → single preview zone, not a split"


# ── coordinated_orbit dispatch: both drones, staggered alt + radius ────────────
def test_coordinated_orbit_dispatch_staggers_both_drones(monkeypatch):
    """PREFLIGHT: 'both orbit this point' reaches BOTH drones at distinct,
    Overwatch-higher altitudes and different radii (circles never coincide)."""
    ow, our = install_fleet(ow_connected=True, our_connected=True)
    monkeypatch.setattr(voice.coord.coordination, "stop_all", lambda: [])
    orbits: list = []

    async def fake_orbit(link, lat, lon, alt, radius, vel, override=False):
        orbits.append({"link": link, "alt": alt, "radius": radius})

    monkeypatch.setattr(voice.coord.commands, "orbit", fake_orbit)
    res = run(voice.dispatch("coordinated_orbit",
                             {"lat": 12.97, "lon": 77.59, "radius_m": 25, "altitude": 40}))
    assert res["ok"]
    by = {id(o["link"]): o for o in orbits}
    assert id(ow) in by and id(our) in by, "both drones must be commanded"
    assert by[id(ow)]["alt"] > by[id(our)]["alt"], "Overwatch must orbit higher"
    assert by[id(ow)]["alt"] != by[id(our)]["alt"], "no shared altitude band"
    assert by[id(ow)]["radius"] > by[id(our)]["radius"], "Overwatch circle must be wider"
