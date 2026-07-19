"""Failsafe tests for the smart-RTL low-battery alert pump (app/main.py).

This is the operator's primary battery-floor warning: it must fire ONCE per
descent below the floor (not 10x/sec), reach BOTH the hub (banner + phone push)
and the voice queue (spoken RTL prompt), name the RIGHT vehicle, and NEVER warn
for a disconnected/disarmed/recovered drone. None of this was covered before.

NO HARDWARE: stubs vision deps, captures hub.publish + queue_voice_alert.

Run: cd backend && PYTHONPATH=. python -m pytest tests/test_low_battery_alert.py -q
"""
from __future__ import annotations

import asyncio
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

import app.main as main  # noqa: E402
from app.config import settings  # noqa: E402


def snap(**over):
    """A battery snapshot. Defaults: armed, connected, healthy battery."""
    base = {"armed": True, "connected": True, "battery_pct": 100.0}
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _capture(monkeypatch):
    """Capture hub publishes + voice alerts; reset the per-vehicle latch + settings."""
    published: list[dict] = []
    spoken: list[str] = []

    async def fake_publish(msg):
        published.append(msg)

    monkeypatch.setattr(main.hub, "publish", fake_publish)
    monkeypatch.setattr(main, "queue_voice_alert", lambda text: spoken.append(text))
    monkeypatch.setattr(settings, "smart_rtl_enabled", True)
    monkeypatch.setattr(settings, "low_battery_pct", 20.0)
    main._LOW_BATT_WARNED.clear()
    yield published, spoken
    main._LOW_BATT_WARNED.clear()


def run(vid, name, s):
    asyncio.run(main._check_low_battery(vid, name, s))


# ── fires correctly ──────────────────────────────────────────────────────────
def test_warns_once_when_armed_drone_at_floor(_capture):
    published, spoken = _capture
    run("outrider", "Outrider", snap(battery_pct=18.0))  # below 20 floor
    # Exactly one hub event AND one voice alert.
    assert len(published) == 1
    assert len(spoken) == 1
    ev = published[0]
    assert ev["type"] == "low_battery"
    assert ev["vehicle"] == "outrider"  # names the right vehicle
    assert ev["name"] == "Outrider"
    assert ev["battery_pct"] == 18.0
    assert ev["threshold"] == 20.0
    # The spoken prompt must instruct RTL on the SPECIFIC low vehicle.
    assert "outrider" in spoken[0]


def test_latches_no_repeat_spam_below_floor(_capture):
    published, spoken = _capture
    for _ in range(10):  # telemetry pump runs ~10 Hz
        run("outrider", "Outrider", snap(battery_pct=15.0))
    assert len(published) == 1  # ONE warning, not ten
    assert len(spoken) == 1


def test_warns_at_zero_percent(_capture):
    published, _ = _capture
    run("outrider", "Outrider", snap(battery_pct=0.0))
    assert len(published) == 1


def test_per_vehicle_latch_independent(_capture):
    published, _ = _capture
    run("overwatch", "Overwatch", snap(battery_pct=10.0))
    run("outrider", "Outrider", snap(battery_pct=12.0))
    # Both low → two distinct warnings, one per vehicle.
    assert {p["vehicle"] for p in published} == {"overwatch", "outrider"}
    assert len(published) == 2


# ── does NOT fire (no phantom / false alerts) ────────────────────────────────
def test_no_warn_when_disconnected(_capture):
    published, spoken = _capture
    run("outrider", "Outrider", snap(battery_pct=5.0, connected=False))
    assert published == [] and spoken == []


def test_no_warn_when_disarmed(_capture):
    published, spoken = _capture
    run("outrider", "Outrider", snap(battery_pct=5.0, armed=False))
    assert published == [] and spoken == []


def test_no_warn_when_battery_unknown(_capture):
    published, spoken = _capture
    run("outrider", "Outrider", snap(battery_pct=None))
    assert published == [] and spoken == []


def test_no_warn_above_floor(_capture):
    published, spoken = _capture
    run("outrider", "Outrider", snap(battery_pct=60.0))
    assert published == [] and spoken == []


def test_no_warn_when_smart_rtl_disabled(_capture, monkeypatch):
    published, spoken = _capture
    monkeypatch.setattr(settings, "smart_rtl_enabled", False)
    run("outrider", "Outrider", snap(battery_pct=5.0))
    assert published == [] and spoken == []


# ── recovery (battery swap) re-arms the warning for the next descent ─────────
def test_latch_resets_on_recovery_then_re_warns(_capture):
    published, _ = _capture
    run("outrider", "Outrider", snap(battery_pct=15.0))  # warn #1
    assert len(published) == 1
    # Battery swapped → well above floor+5 → latch resets (no event).
    run("outrider", "Outrider", snap(battery_pct=95.0))
    assert len(published) == 1
    # Drops again on the next flight → warns again.
    run("outrider", "Outrider", snap(battery_pct=14.0))  # warn #2
    assert len(published) == 2


def test_deadband_does_not_reset_latch(_capture):
    """In the thr..thr+5 hysteresis band the latch neither warns nor resets, so a
    drone hovering around the floor can't re-warn every poll."""
    published, _ = _capture
    run("outrider", "Outrider", snap(battery_pct=18.0))  # below floor → warn
    assert len(published) == 1
    run("outrider", "Outrider", snap(battery_pct=23.0))  # in deadband (20..25)
    run("outrider", "Outrider", snap(battery_pct=18.0))  # back below floor
    # Still latched (deadband didn't reset) → no second warning.
    assert len(published) == 1
