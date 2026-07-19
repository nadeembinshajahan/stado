"""Offline tests for backend/app/mavlink/registry.py — VehicleRegistry.

Focus: the active-vehicle resolution rules (`_resolve_active`) that decide which
drone a per-drone/voice command actually targets. A wrong choice here = commands
hit the wrong vehicle. No hardware: a FakeLink supplies a mutable snapshot.

Run: cd backend && PYTHONPATH=. uv run python -m pytest tests/test_mavlink_registry.py -v
or:  cd backend && PYTHONPATH=. uv run python tests/test_mavlink_registry.py
"""
from __future__ import annotations

from app.mavlink.registry import Vehicle, VehicleRegistry


class FakeLink:
    def __init__(self, connected=True, connection_string="fake://"):
        self._connected = connected
        self.connection_string = connection_string
        self.started = False
        self.on_event = None

    def snapshot(self):
        return {"connected": self._connected}

    def start(self):
        self.started = True

    def stop(self):
        self.started = False


def _fleet(ow_connected=True, our_connected=True, active="overwatch"):
    """Build a fresh 2-vehicle registry with controllable connectivity."""
    reg = VehicleRegistry()
    reg._vehicles = {
        "overwatch": Vehicle("overwatch", "Overwatch", "hex", FakeLink(ow_connected)),
        "outrider": Vehicle("outrider", "Outrider", "quad", FakeLink(our_connected)),
    }
    reg._order = ["overwatch", "outrider"]
    reg._active = active
    return reg


def test_active_choice_kept_when_connected():
    """Rule 1: if the operator's chosen active vehicle is connected, keep it."""
    reg = _fleet(ow_connected=True, our_connected=True, active="outrider")
    assert reg.active_id() == "outrider"
    print("OK connected set-active is honored")


def test_offline_active_falls_through_to_connected_one():
    """Rule 2: chosen active is OFFLINE but the other is connected → target the
    connected one (so an offline set-active never steals commands)."""
    reg = _fleet(ow_connected=False, our_connected=True, active="overwatch")
    assert reg.active_id() == "outrider", (
        "offline set-active must yield to the connected vehicle"
    )
    print("OK offline active falls through to the connected vehicle")


def test_multiple_connected_prefers_set_active():
    """Rule 3: multiple connected → the explicit set-active wins."""
    reg = _fleet(ow_connected=True, our_connected=True, active="outrider")
    assert reg.active_id() == "outrider"
    reg.set_active("overwatch")
    assert reg.active_id() == "overwatch"
    print("OK with several connected, the explicit set-active wins")


def test_none_connected_keeps_defined_active():
    """Rule 4: nothing connected → keep the defined active (always defined)."""
    reg = _fleet(ow_connected=False, our_connected=False, active="overwatch")
    assert reg.active_id() == "overwatch"
    print("OK none connected keeps the defined active id")


def test_first_connected_in_registry_order():
    """Rule 3 tie-break: set-active offline, several connected → FIRST in order."""
    reg = _fleet(ow_connected=True, our_connected=True, active="overwatch")
    # make the set-active offline so resolution must search order
    reg._vehicles["overwatch"].link._connected = False
    reg.set_active("overwatch")
    assert reg.active_id() == "outrider"  # only remaining connected
    # now both connected again but a 3rd added; order = ow, our, third
    reg._vehicles["overwatch"].link._connected = True
    reg._vehicles["third"] = Vehicle("third", "Third", "quad", FakeLink(True))
    reg._order.append("third")
    # set-active points at an offline phantom → first connected by order = overwatch
    reg._vehicles["overwatch"].link._connected = True
    reg._active = "ghost"  # not connected / unknown
    assert reg.active_id() == "overwatch", "should pick first connected in order"
    print("OK first-connected-in-order tie-break")


def test_set_active_unknown_raises():
    reg = _fleet()
    try:
        reg.set_active("nope")
    except KeyError:
        print("OK set_active rejects unknown id")
        return
    raise AssertionError("set_active should raise KeyError for unknown id")


def test_get_unknown_raises():
    reg = _fleet()
    try:
        reg.get("nope")
    except KeyError:
        print("OK get rejects unknown id")
        return
    raise AssertionError("get should raise KeyError for unknown id")


def test_active_vehicle_returns_resolved_not_stored():
    """active_vehicle() must return the RESOLVED vehicle (which may differ from
    the stored _active when the stored one is offline)."""
    reg = _fleet(ow_connected=False, our_connected=True, active="overwatch")
    v = reg.active_vehicle()
    assert v.id == "outrider", "active_vehicle must follow connectivity resolution"
    print("OK active_vehicle returns the resolved (connected) vehicle")


def test_is_connected_swallows_snapshot_exception():
    """A flaky snapshot must never break resolution (returns not-connected)."""
    reg = _fleet()

    class Boom:
        connection_string = "boom"
        def snapshot(self):
            raise RuntimeError("flaky")

    reg._vehicles["overwatch"].link = Boom()
    reg._active = "overwatch"
    # overwatch snapshot raises → treated as not connected → resolves to outrider
    assert reg.active_id() == "outrider"
    print("OK flaky snapshot is swallowed in resolution")


def test_add_first_vehicle_becomes_active():
    reg = VehicleRegistry()
    reg.add("a", "A", "quad", "udpin:0.0.0.0:1")
    assert reg._active == "a", "first added vehicle should become active"
    reg.add("b", "B", "quad", "udpin:0.0.0.0:2")
    assert reg._active == "a", "second add must not steal active"
    reg.add("c", "C", "quad", "udpin:0.0.0.0:3", active=True)
    assert reg._active == "c", "active=True should set active"
    print("OK add() active semantics")


def test_tagged_event_carries_vehicle_id():
    """start_all wires each link's on_event so emitted events carry the vehicle id."""
    captured = []
    reg = _fleet()
    reg.start_all(on_event=lambda e: captured.append(e))
    # start_all must have started every link and wired its on_event
    assert reg._vehicles["overwatch"].link.started
    assert reg._vehicles["outrider"].link.started
    # fire the wired callback as the reader thread would
    reg._vehicles["outrider"].link.on_event({"type": "armed", "armed": True})
    assert captured and captured[-1] == {
        "type": "armed", "armed": True, "vehicle": "outrider"
    }, captured
    print("OK start_all wires + tags events with the originating vehicle id")


# ── capability flags (preflight H1/H2) ─────────────────────────────────────────
def test_default_vehicle_is_fully_capable():
    """A Vehicle built without flags defaults to full capability (back-compat)."""
    v = Vehicle("x", "X", "quad", FakeLink(True))
    assert v.supports_offboard is True and v.supports_missions is True
    print("OK default capability flags are True")


def test_add_threads_capability_flags():
    """registry.add carries supports_offboard/supports_missions onto the Vehicle."""
    reg = VehicleRegistry()
    reg.add("overwatch", "Overwatch", "hex", "fake://",
            supports_offboard=True, supports_missions=True)
    reg.add("outrider", "Outrider", "quad", "fake://",
            supports_offboard=False, supports_missions=False)
    assert reg.supports_offboard("overwatch") is True
    assert reg.supports_missions("overwatch") is True
    assert reg.supports_offboard("outrider") is False
    assert reg.supports_missions("outrider") is False
    print("OK add threads capability flags; supports_* read them back")


def test_supports_unknown_id_is_false():
    """An unknown / None id is treated as INCAPABLE (refuse, don't half-execute)."""
    reg = _fleet()
    assert reg.supports_offboard("ghost") is False
    assert reg.supports_missions(None) is False
    print("OK unknown id => not capable")


def test_link_to_id_reverse_maps():
    """link_to_id maps a link object back to its vehicle id (else None)."""
    reg = _fleet()
    our_link = reg._vehicles["outrider"].link
    assert reg.link_to_id(our_link) == "outrider"
    assert reg.link_to_id(FakeLink(True)) is None
    print("OK link_to_id reverse-maps the link")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for fn in fns:
        try:
            fn()
        except AssertionError as e:
            failed.append((fn.__name__, e))
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - len(failed)}/{len(fns)} registry tests passed")
