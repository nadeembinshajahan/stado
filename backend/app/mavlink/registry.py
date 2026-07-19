from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from .link import MavlinkLink

log = logging.getLogger("gcs.registry")


@dataclass
class Vehicle:
    """A single MAVLink vehicle: identity, capabilities, plus its own dedicated link.

    `supports_offboard` / `supports_missions` / `supports_autotune` describe what the
    vehicle's COMMAND transport can actually carry (preflight H1/H2). A DDS-bridge
    vehicle like Outrider relays COMMAND_LONG/INT only — no SET_POSITION_TARGET_LOCAL_NED
    (GCS OFFBOARD setpoints), no MISSION_* upload, and autotune's cmd 212 is rejected by
    PX4 Commander as UNSUPPORTED over DDS (reviews/autotune-over-dds-verdict.md) — so all
    three are False for it; a normal MAVLink autopilot (Overwatch) has all True. The
    voice + REST layers REFUSE the unsupported commands using these flags rather than
    hardcoding ids.
    """

    id: str
    name: str
    kind: str
    link: MavlinkLink
    supports_offboard: bool = True
    supports_missions: bool = True
    supports_autotune: bool = True


class VehicleRegistry:
    """Owns multiple `Vehicle`s, each with its own `MavlinkLink` (own connection
    string + reader thread), and tracks which one is the *active* vehicle.

    The active vehicle is what the legacy `get_link()` resolves to, so the
    existing single-vehicle flow keeps working unchanged.
    """

    def __init__(self) -> None:
        self._vehicles: dict[str, Vehicle] = {}
        self._order: list[str] = []  # preserve insertion order for list()
        self._active: str | None = None

    # ── construction ──────────────────────────────────────────────────────
    def add(
        self,
        id: str,
        name: str,
        kind: str,
        connection: str,
        *,
        active: bool = False,
        supports_offboard: bool = True,
        supports_missions: bool = True,
        supports_autotune: bool = True,
    ) -> Vehicle:
        vehicle = Vehicle(
            id=id, name=name, kind=kind, link=MavlinkLink(connection),
            supports_offboard=supports_offboard, supports_missions=supports_missions,
            supports_autotune=supports_autotune,
        )
        self._vehicles[id] = vehicle
        if id not in self._order:
            self._order.append(id)
        if active or self._active is None:
            self._active = id
        return vehicle

    # ── lookup ────────────────────────────────────────────────────────────
    def list(self) -> list[Vehicle]:
        return [self._vehicles[i] for i in self._order]

    def get(self, id: str) -> Vehicle:
        if id not in self._vehicles:
            raise KeyError(f"unknown vehicle id: {id}")
        return self._vehicles[id]

    # ── capabilities ──────────────────────────────────────────────────────
    def supports_offboard(self, id: str | None) -> bool:
        """True if vehicle `id` can be commanded via GCS-side OFFBOARD setpoints
        (turn / start_offboard / GCS follow). False for a DDS-bridge vehicle whose
        transport can't carry SET_POSITION_TARGET_LOCAL_NED (Outrider). Unknown
        id → False (refuse rather than half-execute)."""
        if id is None or id not in self._vehicles:
            return False
        return bool(self._vehicles[id].supports_offboard)

    def supports_missions(self, id: str | None) -> bool:
        """True if vehicle `id` can run the MISSION_* upload protocol (survey /
        mission). False for a DDS-bridge vehicle (Outrider). Unknown id → False."""
        if id is None or id not in self._vehicles:
            return False
        return bool(self._vehicles[id].supports_missions)

    def supports_autotune(self, id: str | None) -> bool:
        """True if vehicle `id` can run PX4 autotune over its command link. False for
        a DDS-bridge vehicle (Outrider): cmd 212 reaches PX4 but Commander returns
        UNSUPPORTED (reviews/autotune-over-dds-verdict.md), so the GCS must REFUSE up
        front instead of a false-start that leaves the drone armed/hovering. Unknown
        id → False."""
        if id is None or id not in self._vehicles:
            return False
        return bool(self._vehicles[id].supports_autotune)

    def link_to_id(self, link: MavlinkLink) -> str | None:
        """Reverse-map a link object back to its vehicle id (or None). Lets the
        capability guards work off a resolved link (e.g. get_link())."""
        for v in self._vehicles.values():
            if v.link is link:
                return v.id
        return None

    def _is_connected(self, id: str | None) -> bool:
        """True if vehicle `id` currently reports a live MAVLink link."""
        if id is None or id not in self._vehicles:
            return False
        try:
            return bool(self._vehicles[id].link.snapshot().get("connected"))
        except Exception:  # noqa: BLE001 — a flaky snapshot must never break resolution
            return False

    def _resolve_active(self) -> str | None:
        """The vehicle that per-drone/voice commands should actually target,
        auto-following connectivity so an OFFLINE set-active never steals
        commands from the drone that's really online.

        `set_active(id)` records the operator's explicit choice (`self._active`);
        this only overrides it when that choice is disconnected:
          1. set-active is CONNECTED            → keep it (honors the choice).
          2. exactly one vehicle connected      → that one.
          3. multiple connected                 → set-active if connected (case 1),
                                                   else the first connected (registry order).
          4. none connected                     → fall back to the set-active id, so
                                                   there is ALWAYS a defined active
                                                   (even if it's offline).
        """
        if self._is_connected(self._active):
            return self._active  # 1 (also covers 3 when the choice is connected)
        for id in self._order:  # registry order
            if self._is_connected(id):
                return id  # 2, or the first connected of several (3)
        return self._active  # 4 — none connected: keep the defined active

    def active_id(self) -> str | None:
        return self._resolve_active()

    def active_vehicle(self) -> Vehicle:
        active = self._resolve_active()
        if active is None:
            raise RuntimeError("no active vehicle set")
        return self._vehicles[active]

    def set_active(self, id: str) -> Vehicle:
        """Record the operator's EXPLICIT choice of active vehicle (voice
        select_vehicle / UI). The connectivity auto-rule (see `_resolve_active`)
        only overrides this stored choice when the chosen vehicle is offline."""
        if id not in self._vehicles:
            raise KeyError(f"unknown vehicle id: {id}")
        self._active = id
        log.info("active vehicle set to %s", id)
        return self._vehicles[id]

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start_all(self, on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        """Start every vehicle's reader thread. Each vehicle's `on_event` is
        wired so emitted events carry its own `vehicle` id before reaching the
        hub. A link that can't connect just keeps retrying — never crashes."""
        for v in self.list():
            if on_event is not None:
                v.link.on_event = self._tagged(v.id, on_event)
            v.link.start()
            log.info("started vehicle %s (%s) → %s", v.id, v.kind, v.link.connection_string)

    def stop_all(self) -> None:
        for v in self.list():
            try:
                v.link.stop()
            except Exception:
                log.exception("error stopping vehicle %s", v.id)

    @staticmethod
    def _tagged(
        vehicle_id: str, on_event: Callable[[dict[str, Any]], None]
    ) -> Callable[[dict[str, Any]], None]:
        def _emit(event: dict[str, Any]) -> None:
            # Tag the event with the originating vehicle without mutating the
            # caller's intent; adding a field is safe for the existing frontend.
            tagged = {**event, "vehicle": vehicle_id}
            on_event(tagged)

        return _emit


# Module-level singleton — the new owner of the MAVLink links.
registry = VehicleRegistry()
