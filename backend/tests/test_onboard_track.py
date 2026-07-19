"""Unit tests for app/onboard_track — the Outrider onboard-tracker :8771 channel.

Covers the per-class follow PROFILE extension: class→profile normalization, the
exact ASCII wire format of PROFILE / FOLLOW (the Jetson handler parses these), and
the backward-compatible behaviour (bare FOLLOW still works; an unknown profile
never flies the wrong envelope). No real UDP socket is opened — the module's
datagram send is captured.

Run: cd backend && PYTHONPATH=. .venv/bin/python -m pytest tests/test_onboard_track.py -q
"""
from __future__ import annotations

import pytest

from app import onboard_track as ot


@pytest.fixture
def sent(monkeypatch):
    """Capture every datagram onboard_track would send; force a configured host so
    _send doesn't short-circuit on 'host not configured'."""
    msgs: list[str] = []

    class _FakeSock:
        def settimeout(self, _t):
            pass

        def sendto(self, data, _addr):
            msgs.append(data.decode("ascii"))

        def close(self):
            pass

    monkeypatch.setattr(ot.settings, "outrider_jetson_host", "test-host.local")
    monkeypatch.setattr(ot.settings, "outrider_onboard_track_port", 8771)
    monkeypatch.setattr(ot.socket, "socket", lambda *a, **k: _FakeSock())
    return msgs


# ── class → profile normalization ───────────────────────────────────────────────
@pytest.mark.parametrize("word,expect", [
    ("car", "car"), ("the white pickup truck", "car"), ("a VEHICLE", "car"),
    ("that van", "car"), ("the suv", "car"),
    ("person", "person"), ("the man in the red shirt", "person"),
    ("a running pedestrian", "person"), ("that woman", "person"),
    ("custom", "custom"),
])
def test_normalize_profile_maps_class_words(word, expect):
    assert ot.normalize_profile(word) == expect


@pytest.mark.parametrize("word", ["", None, "the building", "a tree", "nonsense"])
def test_normalize_profile_unknown_returns_none(word):
    assert ot.normalize_profile(word) is None


# ── set_profile: PROFILE wire format + validation ───────────────────────────────
def test_set_profile_sends_canonical_name(sent):
    res = ot.set_profile("car")
    assert res["ok"] is True and res["profile"] == "car"
    assert sent == ["PROFILE car"]


def test_set_profile_normalizes_class_word(sent):
    res = ot.set_profile("the white truck")
    assert res["profile"] == "car"
    assert sent == ["PROFILE car"]


def test_set_profile_unknown_does_not_send(sent):
    res = ot.set_profile("the building")
    assert res["ok"] is False and "unknown" in res["reason"]
    assert sent == []  # never flies a wrong/guessed envelope


def test_set_profile_empty_falls_back_to_config_default(sent, monkeypatch):
    monkeypatch.setattr(ot.settings, "outrider_follow_profile", "person")
    res = ot.set_profile(None)
    assert res["profile"] == "person"
    assert sent == ["PROFILE person"]


# ── follow: FOLLOW wire format with optional profile token ──────────────────────
def test_follow_enable_with_profile_appends_token(sent):
    res = ot.follow(True, "car")
    assert res["follow"] is True and res["profile"] == "car"
    assert sent == ["FOLLOW 1 car"]


def test_follow_enable_normalizes_profile_token(sent):
    ot.follow(True, "the pickup truck")
    assert sent == ["FOLLOW 1 car"]


def test_follow_enable_without_profile_is_bare(sent):
    """Backward-compatible: a bare FOLLOW 1 (no token) still works."""
    res = ot.follow(True)
    assert res["follow"] is True and res["profile"] is None
    assert sent == ["FOLLOW 1"]


def test_follow_unknown_profile_falls_back_to_bare(sent):
    """An unknown profile must NOT abort the follow — it drops to bare FOLLOW 1
    (the controller uses its default envelope) rather than fail to follow."""
    res = ot.follow(True, "the building")
    assert res["follow"] is True and res["profile"] is None
    assert sent == ["FOLLOW 1"]


def test_follow_disable_is_always_bare(sent):
    res = ot.follow(False, "car")
    assert res["follow"] is False and res["profile"] is None
    assert sent == ["FOLLOW 0"]


# ── host-not-configured short-circuit (no socket touched) ───────────────────────
def test_set_profile_no_host_reports_reason(monkeypatch):
    monkeypatch.setattr(ot.settings, "outrider_jetson_host", "")
    res = ot.set_profile("car")
    # profile still validates, but the send fails because no host is configured
    assert res["ok"] is False and "not configured" in res["reason"]
    assert res["profile"] == "car"
