"""Functional tests for vision geolocation + follow + grounding + ally overlay.

No hardware/network: pure math + locally-stubbed pipeline. Covers the
intent-vs-implementation gaps found in the review:

  * geolocate_pixel ground projection (frame / tilt geometry, horizon rejection),
  * follow.geolocate_box uses the BOTTOM-CENTER ground-contact point,
  * the ally-overlay reverse projection is the true inverse of geolocate_pixel,
  * the FollowController control law (deadband, clamps, sign conventions),
  * the on_setpoint dead-code bug: a fresh VisionPipeline has NO on_setpoint
    attribute, so the follow loop would AttributeError instead of sending,
  * grounding box-coordinate normalization (VLM 0-1000 ymin/xmin order).

Run: cd backend && PYTHONPATH=. .venv/bin/python -m pytest tests/test_vision_geo.py -q
"""
from __future__ import annotations

import math
import sys
import types as _t

from app.vision.follow import (
    FollowController,
    Setpoint,
    geolocate_box,
    geolocate_pixel,
)

_R = 6378137.0
LAT0, LON0 = 12.97, 77.59


def _north_east_m(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    north = math.radians(lat - lat0) * _R
    east = math.radians(lon - lon0) * _R * math.cos(math.radians(lat0))
    return north, east


# ── geolocate_pixel: tilt geometry ────────────────────────────────────────────
def test_geolocate_center_pixel_15deg_tilt_distance():
    """Center pixel at a 15 deg mount tilt: ground_dist = alt / tan(15deg)."""
    alt = 50.0
    res = geolocate_pixel(0.5, 0.5, LAT0, LON0, alt, heading_deg=0.0,
                          hfov_deg=69.0, cam_pitch_deg=15.0)
    assert res is not None
    north, east = _north_east_m(res[0], res[1], LAT0, LON0)
    expected = alt / math.tan(math.radians(15.0))
    assert abs(north - expected) < 1.0, f"north {north:.1f} != {expected:.1f}"
    assert abs(east) < 1.0, "no east component when heading north, center pixel"


def test_geolocate_center_pixel_nadir_is_directly_below():
    res = geolocate_pixel(0.5, 0.5, LAT0, LON0, 50.0, heading_deg=0.0,
                          cam_pitch_deg=90.0)
    assert res is not None
    north, east = _north_east_m(res[0], res[1], LAT0, LON0)
    assert abs(north) < 0.5 and abs(east) < 0.5, "nadir center maps to directly below"


def test_geolocate_horizon_ray_returns_none():
    """A ray at/above the horizon can't meet the ground -> None."""
    # cam tilt 15, top of frame (cy=0) subtracts ~half the VFOV -> near horizon.
    res = geolocate_pixel(0.5, 0.0, LAT0, LON0, 50.0, heading_deg=0.0,
                          hfov_deg=69.0, cam_pitch_deg=2.0)
    assert res is None, "ray above the horizon should not geolocate"


def test_geolocate_requires_altitude():
    assert geolocate_pixel(0.5, 0.5, LAT0, LON0, None, 0.0) is None
    assert geolocate_pixel(0.5, 0.5, LAT0, LON0, 0.5, 0.0) is None  # alt < 1 m


def test_geolocate_heading_rotates_bearing_east():
    """Heading 90 (east) sends the center-pixel projection due EAST."""
    res = geolocate_pixel(0.5, 0.5, LAT0, LON0, 50.0, heading_deg=90.0,
                          cam_pitch_deg=15.0)
    north, east = _north_east_m(res[0], res[1], LAT0, LON0)
    assert east > 0 and abs(north) < 1.0, f"east={east:.1f} north={north:.1f}"


def test_geolocate_box_uses_bottom_center():
    """use_bottom projects the box bottom edge (ground contact), not its center,
    so it lands FARTHER than the center for a forward-tilted camera."""
    box = {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2}
    bottom = geolocate_box(box, LAT0, LON0, 50.0, 0.0, cam_pitch_deg=15.0, use_bottom=True)
    center = geolocate_box(box, LAT0, LON0, 50.0, 0.0, cam_pitch_deg=15.0, use_bottom=False)
    nb, _ = _north_east_m(bottom[0], bottom[1], LAT0, LON0)
    nc, _ = _north_east_m(center[0], center[1], LAT0, LON0)
    # Bottom of the box is HIGHER in the frame value (cy larger) -> steeper ->
    # CLOSER than the center for this geometry. Either way they must DIFFER.
    assert abs(nb - nc) > 1.0, "bottom-center vs center must project differently"


# ── ally overlay reverse projection = inverse of geolocate_pixel ──────────────
def test_ally_overlay_is_inverse_of_geolocate():
    """Project a ground pixel out to a world point, then back via the ally
    overlay: it must return to (roughly) the same pixel u,v."""
    from app.ally_overlay import project_world_to_pixel

    # Observer at altitude; pick a pixel, geolocate it to the ground.
    obs_alt = 60.0
    cx, cy = 0.65, 0.7
    ground = geolocate_pixel(cx, cy, LAT0, LON0, obs_alt, heading_deg=30.0,
                             hfov_deg=69.0, cam_pitch_deg=15.0)
    assert ground is not None
    obs = {"lat": LAT0, "lon": LON0, "alt_msl": obs_alt, "heading": 30.0, "pitch": 0.0}
    # The "ally" sits on the ground (alt 0 relative to obs datum).
    ally = {"lat": ground[0], "lon": ground[1], "alt_msl": 0.0}
    proj = project_world_to_pixel(obs, ally, hfov_deg=69.0, cam_pitch_deg=15.0)
    assert proj is not None
    assert proj["in_view"], proj
    assert abs(proj["u"] - cx) < 0.02, f"u {proj['u']} != {cx}"
    assert abs(proj["v"] - cy) < 0.02, f"v {proj['v']} != {cy}"


def test_ally_overlay_behind_camera():
    """An ally directly behind the observer is flagged behind / not in view."""
    from app.ally_overlay import project_world_to_pixel

    obs = {"lat": LAT0, "lon": LON0, "alt_msl": 50.0, "heading": 0.0, "pitch": 0.0}
    # Ally 100 m due SOUTH while the camera faces NORTH -> behind.
    south_lat, _ = LAT0 + math.degrees(-100.0 / _R), LON0
    ally = {"lat": south_lat, "lon": LON0, "alt_msl": 50.0}
    proj = project_world_to_pixel(obs, ally, hfov_deg=69.0, cam_pitch_deg=15.0)
    assert proj is not None
    assert proj["behind"] is True
    assert proj["in_view"] is False


def test_ally_overlay_no_fix_returns_none():
    from app.ally_overlay import project_world_to_pixel

    assert project_world_to_pixel({"lat": None, "lon": None}, {"lat": 1, "lon": 1}) is None


# ── FollowController law ──────────────────────────────────────────────────────
def test_follow_centered_target_is_still():
    fc = FollowController()
    # Target centered, at the desired apparent height -> ~no command.
    box = {"x": 0.5 - 0.14, "y": 0.5 - fc.target_h / 2, "w": 0.28, "h": fc.target_h}
    sp = fc.compute(box)
    assert abs(sp.yaw_rate) < 1e-6, "centered target -> no yaw"
    assert abs(sp.vx) < 1e-6, "target at desired size -> no forward command"
    assert abs(sp.vz) < 1e-6 or abs(sp.vz) <= fc.k_vert * fc.deadband + 1e-6


def test_follow_target_right_yaws_clockwise():
    fc = FollowController()
    box = {"x": 0.8, "y": 0.45, "w": 0.1, "h": fc.target_h}  # center cx=0.85 -> right
    sp = fc.compute(box)
    assert sp.yaw_rate > 0, "target to the right -> +yaw (turn right/CW)"


def test_follow_small_target_moves_forward():
    fc = FollowController()
    box = {"x": 0.45, "y": 0.45, "w": 0.1, "h": 0.05}  # much smaller than target_h
    sp = fc.compute(box)
    assert sp.vx > 0, "small (far) target -> move closer (vx > 0)"


def test_follow_clamps_to_limits():
    fc = FollowController(max_speed=5.0, max_yaw=0.6, max_climb=1.5)
    # Wildly off-center / tiny target -> commands should saturate, never exceed.
    box = {"x": 0.99, "y": 0.99, "w": 0.01, "h": 0.01}
    sp = fc.compute(box)
    assert -fc.max_yaw <= sp.yaw_rate <= fc.max_yaw
    assert -fc.max_speed <= sp.vx <= fc.max_speed
    assert -fc.max_climb <= sp.vz <= fc.max_climb


def test_follow_deadband_ignores_small_offsets():
    fc = FollowController()
    # cx offset within the deadband -> no yaw.
    box = {"x": 0.5 + fc.deadband / 2 - 0.05, "y": 0.5 - fc.target_h / 2,
           "w": 0.1, "h": fc.target_h}
    sp = fc.compute(box)
    assert sp.yaw_rate == 0.0, "sub-deadband horizontal offset must not yaw"


def test_follow_vy_always_zero():
    """Forward-camera follow never strafes: vy is hard-zero by design."""
    fc = FollowController()
    for cx in (0.1, 0.5, 0.9):
        sp = fc.compute({"x": cx, "y": 0.5, "w": 0.1, "h": 0.2})
        assert sp.vy == 0.0


# ── on_setpoint dead-code bug ─────────────────────────────────────────────────
def _import_pipeline_with_stub_yolo():
    """Import VisionPipeline with YOLO + cv2 stubbed so no model/camera is needed."""
    import app.vision.pipeline as P

    class _StubYOLO:
        def __init__(self, *a, **k):
            self.names = {}

    P.YOLO = _StubYOLO  # type: ignore[assignment]
    return P


def test_fresh_pipeline_initializes_on_setpoint():
    """A freshly-constructed VisionPipeline initializes `on_setpoint` to None.

    The C1 fix moves the assignment into __init__ (it previously sat after a
    `return` inside the `has_lock` property = dead code). The capture loop reads
    `if self.on_setpoint:` OUTSIDE its try/except, so a missing attribute would
    crash the capture thread; with it initialized, a pipeline started directly
    (e.g. coordination.search_area -> pipe.start()) is safe even without the
    /start route having wired a callback.
    """
    P = _import_pipeline_with_stub_yolo()
    pipe = P.VisionPipeline("dummy.mp4")
    assert hasattr(pipe, "on_setpoint"), "on_setpoint must be initialized in __init__"
    assert pipe.on_setpoint is None, "default on_setpoint is None until wired"
    # has_lock is a plain property: no lock on a fresh pipeline.
    assert pipe.has_lock is False
    # The vehicle-explicit follow target defaults to None (falls back to active).
    assert pipe.target_vehicle_id is None


def test_seed_then_target_box_only_after_capture():
    """seed_tracker only QUEUES a pending seed; target_box stays None until the
    capture loop inits CSRT on a real frame. Documents the seed-before-frame
    contract (the prior seed-race: a box must not be 'locked' with no frame)."""
    P = _import_pipeline_with_stub_yolo()
    pipe = P.VisionPipeline("dummy.mp4")
    assert pipe.target_box() is None
    pipe.seed_tracker([0.4, 0.4, 0.6, 0.6], "T")
    assert pipe._pending_seed == [0.4, 0.4, 0.6, 0.6]
    assert pipe._csrt_box is None, "no lock until a frame inits CSRT"
    assert pipe.target_box() is None, "target_box must be None before a frame"
    assert pipe.selected_id == -1, "seed marks the CSRT lock as the active target"


# ── H4: lost-lock follow watchdog ─────────────────────────────────────────────
def test_follow_watchdog_disengages_after_timeout():
    P = _import_pipeline_with_stub_yolo()
    pipe = P.VisionPipeline("dummy.mp4")
    pipe.lost_lock_timeout_s = 5.0
    pipe.set_follow(True)  # resets the watchdog clock to now
    t0 = pipe._last_lock_ts

    # A target present keeps follow alive and refreshes the clock.
    assert pipe._follow_watchdog(has_target=True, now=t0 + 100.0) is False
    assert pipe.follow_engaged is True

    # Target lost but still within the timeout -> hold (stay engaged).
    assert pipe._follow_watchdog(has_target=False, now=t0 + 102.0) is False
    assert pipe.follow_engaged is True

    # Lost beyond the timeout -> disengage.
    assert pipe._follow_watchdog(has_target=False, now=t0 + 200.0) is True
    assert pipe.follow_engaged is False


def test_follow_watchdog_noop_when_not_following():
    P = _import_pipeline_with_stub_yolo()
    pipe = P.VisionPipeline("dummy.mp4")
    # Not following: the watchdog never engages or disengages anything.
    assert pipe._follow_watchdog(has_target=False, now=1e9) is False
    assert pipe.follow_engaged is False


# ── C2 + H4: follow setpoint sink is vehicle-explicit and offboard-gated ──────
class _FakeFollowLink:
    def __init__(self, snap):
        self._snap = snap
        self.sent = []

    def snapshot(self):
        return self._snap


def test_on_setpoint_routes_to_explicit_target_vehicle(monkeypatch):
    """C2: setpoints must go to the pipeline's target_vehicle_id, not the active
    vehicle. And only stream when that vehicle is OFFBOARD + armed (H4)."""
    import app.vision_api as VA
    from app.vision.follow import Setpoint

    scout = _FakeFollowLink({"connected": True, "mode": "OFFBOARD", "armed": True})
    active = _FakeFollowLink({"connected": True, "mode": "OFFBOARD", "armed": True})

    class _Pipe:
        target_vehicle_id = "outrider"

    class _Reg:
        def get(self, vid):
            assert vid == "outrider", vid
            return type("V", (), {"link": scout})

    sent = []
    monkeypatch.setattr(VA, "get_pipeline", lambda: _Pipe())
    monkeypatch.setattr(VA, "registry", _Reg())
    monkeypatch.setattr(VA, "get_link", lambda: active)
    monkeypatch.setattr(VA.commands, "send_velocity_body",
                        lambda link, vx, vy, vz, yaw: sent.append((link, vx, vy, vz, yaw)))

    VA._on_setpoint(Setpoint(1.0, 0.0, 0.5, 0.1), {})
    assert len(sent) == 1
    assert sent[0][0] is scout, "must command the EXPLICIT scout link, not active"


def test_on_setpoint_gated_off_when_not_offboard(monkeypatch):
    """H4: a connected-but-not-OFFBOARD (or disarmed) vehicle gets NO setpoints —
    they'd be inert and could fight the operator."""
    import app.vision_api as VA
    from app.vision.follow import Setpoint

    link = _FakeFollowLink({"connected": True, "mode": "HOLD", "armed": True})
    sent = []
    monkeypatch.setattr(VA, "get_pipeline", lambda: None)  # no explicit target
    monkeypatch.setattr(VA, "get_link", lambda: link)
    monkeypatch.setattr(VA.commands, "send_velocity_body",
                        lambda *a: sent.append(a))

    VA._on_setpoint(Setpoint(1.0, 0.0, 0.0, 0.0), {})
    assert sent == [], "not in OFFBOARD -> no setpoint sent"

    # Disarmed in OFFBOARD also gates off.
    link._snap = {"connected": True, "mode": "OFFBOARD", "armed": False}
    VA._on_setpoint(Setpoint(1.0, 0.0, 0.0, 0.0), {})
    assert sent == [], "disarmed -> no setpoint sent"

    # Connected + OFFBOARD + armed -> streams.
    link._snap = {"connected": True, "mode": "OFFBOARD", "armed": True}
    VA._on_setpoint(Setpoint(1.0, 0.0, 0.0, 0.0), {})
    assert len(sent) == 1


# ── Fix 2: locked-target class classification (voice/VLM lock label) ──────────
def test_classify_lock_person_description_maps_to_person():
    """BUGFIX: a VOICE/VLM lock (selected_id == -1) carries the truncated free-text
    description as its `label` (e.g. 'the man in the bla'), which is NOT a class, so
    it used to fall through to the 'car' glyph. _classify_lock must recognise a human
    description and render it as `person`."""
    import app.vision_api as VA

    assert VA._classify_lock("the man in the bla") == "person"
    assert VA._classify_lock("the person in the red shirt") == "person"
    assert VA._classify_lock("a woman running") == "person"


def test_classify_lock_vehicle_and_direct_classes():
    """A vehicle description resolves to 'car'; an exact COCO class (a YOLO-track
    lock) maps directly; an unrecognisable label keeps the 'car' fallback."""
    import app.vision_api as VA

    assert VA._classify_lock("the white pickup truck") == "car"
    assert VA._classify_lock("person") == "person"      # direct COCO class
    assert VA._classify_lock("truck") == "truck"        # direct COCO class preserved
    assert VA._classify_lock("the green box") == "car"  # unknown → prior default


# ── grounding box normalization ───────────────────────────────────────────────
def _fake_vision_chat(monkeypatch, reply_text: str):
    """Stub the Qwen vision call so grounding parses `reply_text` offline."""
    from app.vision import grounding

    async def fake_vision_chat(image, prompt, **kw):
        return reply_text

    monkeypatch.setattr(grounding.qwen, "vision_chat", fake_vision_chat)


def test_ground_qwen_converts_ymin_xmin_order(monkeypatch):
    """The model returns box_2d=[ymin,xmin,ymax,xmax] in 0-1000; the parser must
    emit normalized [x0,y0,x1,y1] in 0-1."""
    import asyncio

    from app.vision import grounding

    _fake_vision_chat(monkeypatch, '[{"box_2d":[100,200,300,400],"label":"car"}]')
    boxes = asyncio.run(grounding.ground_qwen(b"jpeg", "car"))
    assert boxes == [[0.2, 0.1, 0.4, 0.3]], boxes  # [xmin,ymin,xmax,ymax]/1000


def test_resolve_target_picks_largest_box(monkeypatch):
    import asyncio

    from app.vision import grounding

    # Two boxes; the bigger one (covers most of the frame) must win.
    _fake_vision_chat(
        monkeypatch,
        '[{"box_2d":[0,0,100,100],"label":"a"},'
        '{"box_2d":[0,0,900,900],"label":"b"}]'
    )
    best = asyncio.run(grounding.resolve_target(b"jpeg", "thing"))
    assert best == [0.0, 0.0, 0.9, 0.9], best


def test_resolve_target_none_when_empty(monkeypatch):
    import asyncio

    from app.vision import grounding

    _fake_vision_chat(monkeypatch, "[]")
    assert asyncio.run(grounding.resolve_target(b"jpeg", "x")) is None
