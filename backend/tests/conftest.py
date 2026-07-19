"""Shared pytest fixtures for the backend suite.

`test_coordination.py` rebinds module-level callables on the shared
`app.mavlink.commands` module (orbit/goto/start_offboard) to record-only fakes
and never restores them. Without isolation those fakes leak into later test
modules (e.g. `test_mavlink_commands`) and break the real-command assertions —
order-dependently. This autouse fixture snapshots and restores the shared
command entrypoints around every test, so a monkeypatch in one module can't
poison another regardless of collection order. Each test that wants the fakes
re-installs them itself, so this is transparent to the coordination tests.
"""
from __future__ import annotations

import pytest

from app import safety as _safety
from app.mavlink import commands as _commands

# Command entrypoints a test might rebind on the shared module.
_GUARDED = (
    "arm", "disarm", "takeoff", "land", "rtl", "hold", "brake",
    "goto", "orbit", "set_home", "set_speed", "move_relative", "turn",
    "start_offboard", "set_mode",
)


@pytest.fixture(autouse=True)
def _restore_commands():
    saved = {n: getattr(_commands, n) for n in _GUARDED if hasattr(_commands, n)}
    try:
        yield
    finally:
        for n, fn in saved.items():
            setattr(_commands, n, fn)


@pytest.fixture(autouse=True)
def _reset_max_altitude():
    """The max-altitude ceiling is process-global. Clear it around every test so a
    ceiling set by one test (or its config default) can't leak into another and
    silently refuse an unrelated altitude command."""
    _safety.clear_max_altitude()
    try:
        yield
    finally:
        _safety.clear_max_altitude()


@pytest.fixture(autouse=True)
def _ready_for_flight_default_on():
    """Ready-for-Flight is process-global and defaults OFF (a safety default —
    real backends require the operator to explicitly arm the gate before any
    flight command). Existing tests were written assuming a drone commands-ready
    state, so seed the gate ON for both fleet vehicles here. Tests specifically
    exercising the gate should call `_safety.reset_for_tests()` themselves to
    return to a clean OFF state."""
    _safety.reset_for_tests()
    for vid in ("overwatch", "outrider"):
        _safety.set_ready(vid, True)
    try:
        yield
    finally:
        _safety.reset_for_tests()
