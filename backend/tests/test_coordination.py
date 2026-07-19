"""Smoke tests for the multi-drone coordination layer.

Stubs the per-vehicle MAVLink links (no SITL/hardware needed) and verifies:
  * coordinated_orbit staggers altitudes (Overwatch higher, >= 5 m apart) and radii
  * formation_flight computes a sane offset point and keeps Outrider below Overwatch
  * the behavior registry starts/stops/supersedes named tasks

Run: cd backend && PYTHONPATH=. uv run python -m tests.test_coordination
"""
from __future__ import annotations

import asyncio
import math

from app import coordination as coord
from app.coordination import SEP_M, Coordination, formation_offset_point, staggered_altitudes
from app.mavlink.registry import registry


class FakeLink:
    """Minimal stand-in for MavlinkLink: a mutable snapshot + recorded commands."""

    def __init__(self, **state):
        base = {"connected": True, "armed": True, "lat": None, "lon": None,
                "alt_rel": None, "alt_msl": None, "heading": 0.0, "mode": "HOLD"}
        base.update(state)
        self._state = base
        self.orbits: list[dict] = []
        self.gotos: list[dict] = []

    def snapshot(self):
        return dict(self._state)


def _install_fake_fleet(ow_state, our_state):
    """Replace the registry's two vehicles with fake-linked stand-ins; returns
    (overwatch_link, outrider_link) and restores nothing (test-only process)."""
    from app.mavlink.registry import Vehicle

    ow_link = FakeLink(**ow_state)
    our_link = FakeLink(**our_state)
    # Production capabilities: Outrider is a DDS-bridge vehicle (no GCS OFFBOARD,
    # no MISSION_*). formation/orbit use COMMAND_INT (supported), so these are
    # unaffected; pairing/search guards key off these flags (H1/H2).
    registry._vehicles = {
        "overwatch": Vehicle("overwatch", "Overwatch", "hex", ow_link,
                             supports_offboard=True, supports_missions=True),
        "outrider": Vehicle("outrider", "Outrider", "quad", our_link,
                            supports_offboard=False, supports_missions=False),
    }
    registry._order = ["overwatch", "outrider"]
    registry._active = "overwatch"
    return ow_link, our_link


def _patch_commands():
    """Monkeypatch commands.orbit/goto/start_offboard to record instead of send."""
    async def fake_orbit(link, lat, lon, alt, radius=20.0, velocity=3.0, clockwise=True, override=False):
        link.orbits.append({"lat": lat, "lon": lon, "alt": alt, "radius": radius})

    async def fake_goto(link, lat, lon, alt, speed=-1.0, override=False):
        link.gotos.append({"lat": lat, "lon": lon, "alt": alt})

    async def fake_offboard(link):
        return None

    coord.commands.orbit = fake_orbit
    coord.commands.goto = fake_goto
    coord.commands.start_offboard = fake_offboard


def test_staggered_altitudes():
    ow, our = staggered_altitudes(40.0)
    assert ow - our >= SEP_M, f"expected >= {SEP_M} m gap, got {ow - our}"
    assert ow > our, "Overwatch must be higher than Outrider"
    # Too-low request gets bumped so Outrider stays above the safe floor.
    ow2, our2 = staggered_altitudes(2.0)
    assert our2 >= coord.MIN_ALT_M
    assert ow2 - our2 >= SEP_M
    print("OK staggered_altitudes: 40 ->", (ow, our), " 2 ->", (ow2, our2))


def test_coordinated_orbit_staggering():
    ow_link, our_link = _install_fake_fleet(
        ow_state={"lat": 47.397, "lon": 8.545, "alt_rel": 40.0},
        our_state={"lat": 47.397, "lon": 8.545, "alt_rel": 20.0},
    )
    _patch_commands()
    res = asyncio.run(coord.coordinated_orbit(lat=47.397, lon=8.545, radius_m=25.0, altitude=40.0))
    assert res["ok"], res
    assert ow_link.orbits and our_link.orbits, "both drones should have been issued an orbit"
    ow_alt = ow_link.orbits[-1]["alt"]
    our_alt = our_link.orbits[-1]["alt"]
    assert ow_alt > our_alt, f"Overwatch ({ow_alt}) must orbit higher than Outrider ({our_alt})"
    assert ow_alt - our_alt >= SEP_M, f"altitudes must be >= {SEP_M} m apart, got {ow_alt - our_alt}"
    # Radii staggered so circles don't intersect (Overwatch wider).
    assert ow_link.orbits[-1]["radius"] > our_link.orbits[-1]["radius"]
    print(f"OK coordinated_orbit: Overwatch alt={ow_alt} r={ow_link.orbits[-1]['radius']} | "
          f"Outrider alt={our_alt} r={our_link.orbits[-1]['radius']}")


def test_formation_offset_point():
    # 12 m directly behind (bearing 180) a north-facing Overwatch -> due south.
    lat, lon = 47.397, 8.545
    tlat, tlon = formation_offset_point(lat, lon, heading_deg=0.0, offset_m=12.0, bearing_deg=180.0)
    assert tlat < lat, "behind a north-facing drone should be SOUTH (lower lat)"
    assert abs(tlon - lon) < 1e-6, "no east/west component when directly behind"
    # Distance is ~12 m.
    r = 6378137.0
    dist = math.radians(lat - tlat) * r
    assert abs(dist - 12.0) < 0.5, f"offset distance should be ~12 m, got {dist:.2f}"
    print(f"OK formation_offset_point: 12 m behind north-facing -> south by {dist:.2f} m")


def test_formation_loop_keeps_outrider_below():
    ow_link, our_link = _install_fake_fleet(
        ow_state={"lat": 47.397, "lon": 8.545, "alt_rel": 50.0, "heading": 90.0},
        our_state={"lat": 47.397, "lon": 8.545, "alt_rel": 20.0, "heading": 90.0},
    )
    _patch_commands()

    async def run_once():
        c = Coordination()
        c.start_behavior("formation", lambda: coord._formation_loop(12.0, 180.0, period_s=0.05))
        await asyncio.sleep(0.12)  # let the ~1 Hz (here 0.05 s) loop tick a couple of times
        assert c.status()["running"] == ["formation"]
        c.stop("formation")
        await asyncio.sleep(0.05)
        assert c.status()["running"] == [], "behavior should be cancelled"

    asyncio.run(run_once())
    assert our_link.gotos, "formation loop should have repositioned Outrider"
    g = our_link.gotos[-1]
    # Outrider commanded >= SEP_M below Overwatch's 50 m.
    assert g["alt"] <= 50.0 - SEP_M, f"Outrider alt {g['alt']} must be >= {SEP_M} m below 50"
    print(f"OK formation loop: Outrider repositioned to alt={g['alt']} (Overwatch 50), "
          f"{len(our_link.gotos)} updates, start/stop verified")


def test_registry_supersedes():
    async def run():
        c = Coordination()
        ticks = {"a": 0, "b": 0}

        async def loop(key):
            while True:
                ticks[key] += 1
                await asyncio.sleep(0.02)

        c.start_behavior("x", lambda: loop("a"))
        await asyncio.sleep(0.05)
        c.start_behavior("x", lambda: loop("b"))  # supersede: cancels "a"
        await asyncio.sleep(0.05)
        a_after = ticks["a"]
        await asyncio.sleep(0.05)
        assert ticks["a"] == a_after, "old task should have been cancelled (no more ticks)"
        assert ticks["b"] > 0, "new task should be running"
        assert c.stop_all() == ["x"]
        await asyncio.sleep(0.02)
        assert c.status()["running"] == []

    asyncio.run(run())
    print("OK registry: same-named start supersedes old task; stop_all clears")


# ── H1/H2: coordination capability guards ──────────────────────────────────────
class _FakePipe:
    _running = True
    def get_jpeg(self):
        return b"x" * 5000


def test_pair_overwatch_scout_refused_when_scout_lacks_offboard(monkeypatch):
    """H1: the scout (Outrider) follows via GCS-side OFFBOARD (start_offboard +
    streamed setpoints). If Outrider can't take GCS OFFBOARD the pairing must be
    REFUSED before any frame grab / start_offboard, so a flying Outrider is never
    switched into OFFBOARD with no setpoints."""
    _install_fake_fleet({"connected": True, "lat": 12.97, "lon": 77.59, "alt_rel": 30},
                        {"connected": True, "lat": 12.97, "lon": 77.59, "alt_rel": 20})
    monkeypatch.setattr(coord, "get_pipeline", lambda: _FakePipe())
    started = []
    async def fake_start_offboard(link):
        started.append(link)
    monkeypatch.setattr(coord.commands, "start_offboard", fake_start_offboard)
    res = asyncio.run(coord.pair_overwatch_scout("the truck"))
    assert res["ok"] is False and res["capability"] == "offboard"
    assert started == [], "start_offboard must NOT be scheduled when pairing is refused"
    print("OK pair_overwatch_scout refused for a non-offboard scout")


def test_search_area_excludes_mission_incapable_outrider(monkeypatch):
    """H2: a coordinated SEARCH flies a lawnmower MISSION per zone, so Outrider
    (no MISSION_*) is EXCLUDED from the split — search runs on Overwatch only and
    reports the exclusion."""
    ow_link, our_link = _install_fake_fleet(
        {"connected": True, "lat": 12.97, "lon": 77.59, "alt_rel": 30},
        {"connected": True, "lat": 12.97, "lon": 77.59, "alt_rel": 20})
    split_calls = {}
    def fake_split(lat, lon, w, h, hdg, n=2, gap_m=5.0):
        split_calls["n"] = n
        return [[(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)] for _ in range(n)]
    async def fake_plan_and_fly(vehicles, zones, base_alt, line_spacing_m, sep_m,
                                source_polygon=None):
        return [{"vehicle": v, "name": v, "altitude": 30.0, "polygon": [[0, 0]]}
                for v in vehicles]
    monkeypatch.setattr(coord.coordinated, "split_rect", fake_split)
    monkeypatch.setattr(coord.coordinated, "plan_and_fly", fake_plan_and_fly)
    monkeypatch.setattr(coord, "get_pipeline", lambda: None)
    res = asyncio.run(coord.search_area(200))
    assert res["ok"] is True
    assert split_calls["n"] == 1, "only the mission-capable drone gets a search zone"
    assert res["excluded"] == ["outrider"]
    flown = {a["vehicle"] for a in res["assignments"]}
    assert "outrider" not in flown
    print("OK search_area excludes mission-incapable Outrider")


if __name__ == "__main__":
    test_staggered_altitudes()
    test_coordinated_orbit_staggering()
    test_formation_offset_point()
    test_formation_loop_keeps_outrider_below()
    test_registry_supersedes()
    print("\nALL COORDINATION SMOKE TESTS PASSED")
