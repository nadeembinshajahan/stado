"""Runtime fleet safety limits — currently a single global MAX-ALTITUDE CEILING.

A single process-global value (metres AGL) that BOTH the voice command path
(`app/voice.py`) and the REST command path (`app/api.py`) read+write, plus the
command layer (`app/mavlink/commands.py`) and the survey/coordination layers
ENFORCE on every altitude-bearing command. There is exactly ONE ceiling for the
whole fleet (not per-vehicle).

Default = None => NO ceiling (unlimited), unless `settings.max_altitude_m` is
configured, in which case that is the startup default.

ENFORCEMENT CONTRACT (no silent clamping — the operator MUST be told):
  * `check_altitude(alt)` raises `CeilingExceeded` when a requested OR DERIVED
    altitude exceeds the ceiling and `override` is not set. Callers either let it
    propagate (REST → 422 via the handler that maps it; voice → caught by
    dispatch's try and returned as {ok:false}) or call `refusal(alt)` to get a
    structured operator-facing dict.
  * When `override=True` the check is BYPASSED and an `altitude_override` audit
    event is logged so the override is on the record.

GCS-SIDE ONLY: this is enforced by the GCS. It should ALSO be pushed to PX4's
geofence (GF_MAX_VER_DIST / GF_ACTION) so the autopilot enforces it independently
— that needs a param subsystem the backend does not have yet. See
reviews/max-altitude-ceiling.md.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger("gcs.safety")

# Process-global ceiling in metres AGL. None = no ceiling. Guarded by a lock so a
# voice set racing a REST set can't tear a half-written value (both write a single
# float, but the lock also makes get/set ordering well-defined).
_lock = threading.Lock()
_max_altitude_m: float | None = None


class CeilingExceeded(ValueError):
    """Raised when a requested/derived altitude exceeds the active ceiling and no
    override was given. `requested`/`ceiling` carry the numbers so a handler can
    surface them; str(exc) is the operator-facing reason."""

    def __init__(self, requested: float, ceiling: float) -> None:
        self.requested = float(requested)
        self.ceiling = float(ceiling)
        super().__init__(reason(requested, ceiling))


def reason(requested: float, ceiling: float) -> str:
    """The single canonical refusal phrasing used everywhere (voice + REST)."""
    return (
        f"requested {round(float(requested), 1)} m exceeds the "
        f"{round(float(ceiling), 1)} m ceiling — override required"
    )


def init_default(default: float | None) -> None:
    """Seed the ceiling from config at startup (only if not already set)."""
    global _max_altitude_m
    with _lock:
        if _max_altitude_m is None and default is not None:
            _max_altitude_m = float(default)
            log.info("max-altitude ceiling seeded from config: %.1f m", _max_altitude_m)


def get_max_altitude() -> float | None:
    """The active fleet ceiling (metres AGL), or None for no ceiling."""
    with _lock:
        return _max_altitude_m


def set_max_altitude(altitude_m: float) -> float:
    """Set the fleet ceiling (metres AGL). Returns the stored value. Rejects a
    non-positive ceiling (a 0 m / negative ceiling would ground the fleet — almost
    always a mistake; clear it instead)."""
    alt = float(altitude_m)
    if alt <= 0:
        raise ValueError("max altitude must be a positive number of metres (use clear to remove the ceiling)")
    global _max_altitude_m
    with _lock:
        _max_altitude_m = alt
    log.info("max-altitude ceiling set to %.1f m AGL", alt)
    return alt


def clear_max_altitude() -> None:
    """Remove the ceiling entirely (unlimited altitude)."""
    global _max_altitude_m
    with _lock:
        _max_altitude_m = None
    log.info("max-altitude ceiling cleared (unlimited)")


def exceeds(altitude_m: float) -> bool:
    """True if `altitude_m` (AGL) is above the active ceiling. False if there is
    no ceiling."""
    ceiling = get_max_altitude()
    return ceiling is not None and float(altitude_m) > ceiling


def _audit_override(altitude_m: float, ceiling: float, context: str | None) -> None:
    """Record an `altitude_override` audit event when the ceiling is bypassed."""
    try:
        from .logbus import logbus

        logbus.log(
            "safety", None, None,
            event="altitude_override",
            requested_alt_m=round(float(altitude_m), 1),
            ceiling_m=round(float(ceiling), 1),
            context=context,
        )
    except Exception:  # noqa: BLE001 — auditing must never break a command
        log.exception("failed to log altitude_override audit event")


def check_altitude(
    altitude_m: float, override: bool = False, context: str | None = None
) -> None:
    """Enforce the ceiling for a requested OR DERIVED altitude (metres AGL).

    Raises `CeilingExceeded` when `altitude_m` is above the active ceiling and
    `override` is False — the command must NOT proceed (no silent clamp). When
    `override` is True the check is bypassed; if it WOULD have been refused, an
    `altitude_override` audit event is logged so the bypass is on the record.
    No ceiling set => always allowed.
    """
    ceiling = get_max_altitude()
    if ceiling is None:
        return
    if float(altitude_m) > ceiling:
        if override:
            _audit_override(altitude_m, ceiling, context)
            return
        raise CeilingExceeded(altitude_m, ceiling)


def refusal(altitude_m: float, vehicle: str | None = None) -> dict:
    """Structured operator-facing refusal dict for the voice layer (returned, not
    raised). Mirrors the CeilingExceeded reason."""
    ceiling = get_max_altitude()
    out: dict = {
        "ok": False,
        "error": reason(altitude_m, ceiling if ceiling is not None else altitude_m),
        "ceiling_m": ceiling,
        "requested_m": round(float(altitude_m), 1),
        "override_required": True,
    }
    if vehicle is not None:
        out["vehicle"] = vehicle
    return out


def fit_stack_under_ceiling(
    base_alt: float, sep_m: float, min_alt_m: float, override: bool = False,
    context: str | None = None,
) -> tuple[float, float]:
    """Fit a 2-tier staggered stack (top = Overwatch, bottom >= sep_m below) UNDER
    the ceiling for a fleet takeoff/coordination.

    Given the operator's requested base altitude and the required vertical
    separation, return `(top_alt, bottom_alt)` such that:
      * the stack starts from `base_alt` as the TOP (Overwatch) altitude, with the
        bottom drone `sep_m` below it (the existing staggered-takeoff convention:
        the top drone is the requested base, others below);
      * the TOP altitude does NOT exceed the ceiling;
      * the bottom altitude stays >= `min_alt_m` (the safe floor).

    If the ceiling is too low to fit BOTH with `sep_m` of separation above the
    floor (i.e. ceiling < min_alt_m + sep_m), raise `CeilingExceeded` (unless
    `override`, in which case the unconstrained stack is returned and the override
    is audited) — there is no safe way to stagger under such a low ceiling, so the
    operator must be told why rather than silently collapsing the separation.

    No ceiling => the unconstrained stack (top=base_alt, bottom=base_alt-sep_m,
    floored) is returned unchanged.
    """
    top = float(base_alt)
    ceiling = get_max_altitude()

    # No ceiling — behave as before (top is the base, bottom sep_m below the floor).
    if ceiling is None:
        return top, max(top - sep_m, float(min_alt_m))

    # The ceiling is too low to fit the separated stack above the floor at all.
    if ceiling < float(min_alt_m) + sep_m:
        if override:
            _audit_override(top, ceiling, context or "staggered_takeoff")
            return top, max(top - sep_m, float(min_alt_m))
        raise CeilingExceeded(min_alt_m + sep_m, ceiling)

    # If the requested top is above the ceiling, lower the WHOLE stack to fit
    # (top = ceiling) UNLESS overriding. Lowering the top below the requested base
    # is allowed because we keep separation + floor intact and the operator's
    # intent ("as high as it can go") is preserved — but only down to the ceiling,
    # never above it.
    if top > ceiling:
        if override:
            _audit_override(top, ceiling, context or "staggered_takeoff")
        else:
            top = ceiling
    bottom = max(top - sep_m, float(min_alt_m))
    return top, bottom


# ── Ready-for-Flight gate ────────────────────────────────────────────────────
# Per-vehicle boolean. OFF at startup for every vehicle. Every flight-authorizing
# command from every source (HTTP, voice, coordination) is refused while OFF.
# Recovery commands (LAND / RTL / HOLD / BRAKE / disarm) are EXEMPT so a backend
# restart mid-flight cannot strand a drone. Gate auto-locks ON when the vehicle
# is armed AND alt_rel > AIRBORNE_M — the setter refuses to turn it OFF then.

# "Airborne" threshold in metres above launch. > this + armed = auto-lock ON.
# Chosen at 1.0 m to be above ground-bump noise but well below normal flight alt.
AIRBORNE_M = 1.0

_gate_lock = threading.Lock()
_ready_for_flight: dict[str, bool] = {}
_seeded_seen: set[str] = set()  # vehicles whose first-frame seed has been considered
_unwind_hooks: list[Callable[[str], None]] = []


class NotReady(RuntimeError):
    """Raised when a flight-authorizing command runs while the vehicle's gate is OFF."""

    def __init__(self, vehicle: str | None = None) -> None:
        self.vehicle = vehicle
        super().__init__(
            f"ready-for-flight gate is OFF for {vehicle or 'this vehicle'}"
        )


class GateLocked(RuntimeError):
    """Raised when the caller tries to turn a gate OFF while the vehicle is armed+airborne."""

    def __init__(self, vehicle: str, reason: str = "armed and airborne") -> None:
        self.vehicle = vehicle
        self.gate_reason = reason
        super().__init__(f"cannot disable ready-for-flight for {vehicle}: {reason}")


def is_ready(vehicle_id: str) -> bool:
    """True if commands are currently allowed for this vehicle."""
    with _gate_lock:
        return bool(_ready_for_flight.get(vehicle_id, False))


def all_ready() -> dict[str, bool]:
    """Snapshot of every known vehicle's gate state."""
    with _gate_lock:
        return dict(_ready_for_flight)


def is_locked(armed: bool | None, alt_rel: float | None) -> bool:
    """True if the gate must be considered locked ON given this telemetry snapshot
    (armed AND alt_rel > AIRBORNE_M). Unknown altitude while armed is treated as
    ground — the operator can still toggle the gate off in that state, which
    matches the case where telemetry is stale but the drone is sitting."""
    if not armed:
        return False
    if alt_rel is None:
        return False
    try:
        return float(alt_rel) > AIRBORNE_M
    except (TypeError, ValueError):
        return False


def register_unwind_hook(fn: Callable[[str], None]) -> None:
    """Register a callback invoked whenever a vehicle's gate transitions to OFF.
    The hook receives the vehicle_id and must be safe to call from any thread.
    Failures are logged and swallowed so one bad hook can't block others.
    Register at startup from main.py — e.g. stop coordination, kill follow
    setpoint stream, tell the Jetson to stop tracking, cancel autotune."""
    _unwind_hooks.append(fn)


def _run_unwind_hooks(vehicle_id: str) -> None:
    for hook in list(_unwind_hooks):
        try:
            hook(vehicle_id)
        except Exception:  # noqa: BLE001 — hooks must never block each other
            log.exception(
                "ready-for-flight unwind hook %r failed for %s", hook, vehicle_id
            )


def set_ready(
    vehicle_id: str,
    ready: bool,
    *,
    armed: bool | None = None,
    alt_rel: float | None = None,
) -> bool:
    """Set gate state. Returns the new boolean.

    When turning OFF while the vehicle is armed AND alt_rel > AIRBORNE_M this
    raises `GateLocked` (the operator cannot cut recovery paths mid-flight).
    On a successful OFF-transition, runs every registered unwind hook so any
    live coordination loop, follow streamer, onboard tracker or autotune loop
    is torn down before commands stop being accepted.
    """
    ready = bool(ready)
    with _gate_lock:
        prev = _ready_for_flight.get(vehicle_id, False)
        if not ready and is_locked(armed, alt_rel):
            raise GateLocked(vehicle_id)
        _ready_for_flight[vehicle_id] = ready
    if prev != ready:
        _audit_gate_change(vehicle_id, ready, armed=armed, alt_rel=alt_rel)
        if not ready:
            _run_unwind_hooks(vehicle_id)
    return ready


def _audit_gate_change(vehicle_id: str, ready: bool, **fields) -> None:
    try:
        from .logbus import logbus

        logbus.log(
            "safety",
            None,
            vehicle_id,
            event="ready_for_flight_change",
            ready=ready,
            **{k: v for k, v in fields.items() if v is not None},
        )
    except Exception:  # noqa: BLE001 — auditing must never break a command
        log.exception("failed to log ready_for_flight_change audit event")


def refusal_not_ready(vehicle_id: str | None) -> dict:
    """Structured operator-facing refusal dict for the voice layer."""
    return {
        "ok": False,
        "error": (
            "ready-for-flight gate is OFF — enable it in the status bar before commanding"
        ),
        "vehicle": vehicle_id,
        "ready_for_flight": False,
    }


def seed_from_telemetry(
    vehicle_id: str, armed: bool | None, alt_rel: float | None
) -> None:
    """Restart safety. On the very first telemetry frame per vehicle, if the
    drone is already armed+airborne, seed the gate to ON so the backend can
    still command it — otherwise a backend restart mid-flight would strand
    the vehicle behind a closed gate. Idempotent: only fires once per vehicle_id.

    Called from the telemetry loop for every vehicle it sees. It's safe (and
    cheap) to call every tick — the `_seeded_seen` set gates the actual work.
    """
    if vehicle_id in _seeded_seen:
        return
    with _gate_lock:
        if vehicle_id in _seeded_seen:  # double-check under lock
            return
        _seeded_seen.add(vehicle_id)
        # Only actually flip the gate if the vehicle is already airborne. If it
        # is on the ground (the common case), we leave the gate OFF as designed.
        if not is_locked(armed, alt_rel):
            return
        if _ready_for_flight.get(vehicle_id, False):
            return
        _ready_for_flight[vehicle_id] = True
    log.warning(
        "ready-for-flight seeded ON for %s (armed+airborne at %s m — "
        "backend restart mid-flight)",
        vehicle_id,
        alt_rel,
    )
    _audit_gate_change(vehicle_id, True, armed=armed, alt_rel=alt_rel, seeded=True)


def reset_for_tests() -> None:
    """Wipe all gate state. Test-only helper."""
    with _gate_lock:
        _ready_for_flight.clear()
        _seeded_seen.clear()
        _unwind_hooks.clear()
