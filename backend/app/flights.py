from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("gcs.flights")

# Keep at most this many finalized flights in memory / on disk.
MAX_FLIGHTS = 50
# Pre-arm agent actions newer than this many seconds before the arm are folded
# into the flight on takeoff (so the opening "takeoff" command is on the
# timeline). Older buffered actions are discarded as unrelated.
PREARM_ACTION_WINDOW_S = 30.0
# Cap the pre-arm action ring so it can't grow unbounded between flights.
MAX_PREARM_ACTIONS = 16
# Downsample the recorded path to roughly this many points before storing.
MAX_PATH_POINTS = 300
# Persist finalized flights here so they survive a backend restart.
STORE_PATH = Path(__file__).resolve().parent.parent / "flights.json"

EARTH_RADIUS_M = 6_371_000.0


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two lat/lon points."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _downsample(points: list[list[float]], target: int) -> list[list[float]]:
    """Evenly thin a path to ~`target` points, always keeping first + last."""
    n = len(points)
    if n <= target:
        return points
    step = n / target
    out: list[list[float]] = []
    i = 0.0
    while int(i) < n:
        out.append(points[int(i)])
        i += step
    last = points[-1]
    if not out or out[-1] is not last:
        out.append(last)
    return out


def _num(v: Any, nd: int = 0) -> str | None:
    """Format a numeric arg compactly, or None if it's missing/not a number."""
    try:
        if v is None:
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    if nd <= 0:
        return f"{round(f)}"
    return f"{f:.{nd}f}".rstrip("0").rstrip(".")


def action_label(name: str, args: dict[str, Any], result: dict[str, Any]) -> str:
    """Turn a voice tool call into a clean, operator-grade one-line label.

    Examples: "Takeoff → 30 m", "Orbit target r=25 m", "Set speed 8 m/s",
    "Go to LZ-1", "Switch mode GUIDED". Falls back to a title-cased tool name."""
    a = args or {}
    r = result or {}
    n = (name or "").lower()
    if n == "arm":
        return "Arm"
    if n == "disarm":
        return "Disarm"
    if n == "takeoff":
        alt = _num(a.get("altitude_m"))
        return f"Takeoff → {alt} m" if alt else "Takeoff"
    if n == "land":
        return "Land"
    if n in ("return_to_launch", "rtl"):
        return "Return to launch"
    if n == "hold":
        return "Hold position"
    if n == "set_mode":
        mode = a.get("mode")
        return f"Switch mode {str(mode).upper()}" if mode else "Switch mode"
    if n == "track_target":
        desc = a.get("description")
        return f"Track target: {desc}" if desc else "Track target"
    if n == "follow":
        enable = a.get("enable", True)
        return "Follow target" if bool(enable) else "Stop follow"
    if n == "orbit_target":
        rad = _num(r.get("radius_m") or a.get("radius_m"))
        return f"Orbit target r={rad} m" if rad else "Orbit target"
    if n in ("survey_area", "survey", "execute_survey"):
        size = _num(a.get("size_m"))
        return f"Survey area {size} m" if size else "Survey area"
    if n == "survey_area_with_fleet":
        size = _num(a.get("size_m"))
        return f"Fleet survey {size} m" if size else "Fleet survey"
    if n == "move":
        parts = []
        for key, lbl in (("forward_m", "fwd"), ("right_m", "right"), ("up_m", "up")):
            val = _num(a.get(key), 1)
            if val and float(a.get(key) or 0) != 0:
                parts.append(f"{lbl} {val} m")
        return "Move " + ", ".join(parts) if parts else "Move"
    if n == "turn":
        deg = _num(a.get("degrees"))
        direction = str(a.get("direction", "left"))
        return f"Turn {direction} {deg}°" if deg else f"Turn {direction}"
    if n == "set_speed":
        sp = _num(a.get("speed_ms"), 1)
        return f"Set speed {sp} m/s" if sp else "Set speed"
    if n == "goto_point":
        return f"Go to {a.get('name')}" if a.get("name") else "Go to point"
    if n == "orbit_point":
        rad = _num(a.get("radius_m"))
        base = f"Orbit {a.get('name')}" if a.get("name") else "Orbit point"
        return f"{base} r={rad} m" if rad else base
    if n == "coordinated_orbit":
        rad = _num(a.get("radius_m"))
        return f"Coordinated orbit r={rad} m" if rad else "Coordinated orbit"
    if n == "formation_flight":
        enable = a.get("enable", True)
        return "Formation flight" if bool(enable) else "Stop formation"
    if n == "pair_overwatch_scout":
        desc = a.get("target_description")
        return f"Overwatch + scout: {desc}" if desc else "Overwatch + scout"
    if n == "search_area":
        size = _num(a.get("size_m"))
        return f"Search area {size} m" if size else "Search area"
    if n == "stop_coordination":
        return "Stop coordination"
    if n == "select_vehicle":
        return f"Select {a.get('name')}" if a.get("name") else "Select vehicle"
    # Generic fallback: humanize the tool name.
    return name.replace("_", " ").strip().capitalize()


class FlightRecorder:
    """Records a single vehicle's flights.

    Fed a per-vehicle telemetry snapshot at the telemetry rate plus discrete
    mode/statustext/armed events. Detects a flight on the disarmed→armed
    transition and finalizes it on armed→disarmed. Each finalized flight is
    handed to the module-level store, which keeps the most recent ones.
    """

    def __init__(self, vehicle_id: str, vehicle_name: str) -> None:
        self.vehicle_id = vehicle_id
        self.vehicle_name = vehicle_name
        self._lock = threading.Lock()
        self._armed = False
        self._cur: dict[str, Any] | None = None
        # A small ring of agent actions issued BEFORE arm (e.g. the takeoff
        # command that starts the flight). Attached to the flight on `_begin` so
        # the report's action timeline isn't missing the very command that
        # opened the mission. Only actions within PREARM_ACTION_WINDOW_S of the
        # arm survive, so a stale command from minutes ago isn't mis-attributed.
        self._prearm_actions: list[dict[str, Any]] = []
        # Callback invoked with the finalized flight summary (wired by main.py).
        self.on_complete: Callable[[dict[str, Any]], None] | None = None

    # ── flight lifecycle ─────────────────────────────────────────────────────
    def _begin(self, snap: dict[str, Any]) -> None:
        now = time.time()
        lat, lon = snap.get("lat"), snap.get("lon")
        self._cur = {
            "id": uuid.uuid4().hex[:12],
            "vehicle_id": self.vehicle_id,
            "vehicle_name": self.vehicle_name,
            "start_ts": now,
            "end_ts": None,
            "max_alt_m": 0.0,
            "max_speed_ms": 0.0,
            "distance_m": 0.0,
            "takeoff": {"lat": lat, "lon": lon} if lat is not None and lon is not None else None,
            "landing": None,
            "battery_start_pct": snap.get("battery_pct"),
            "battery_min_pct": snap.get("battery_pct"),
            "battery_start_v": snap.get("battery_voltage"),
            "path": [],  # [lat, lon, alt_rel]
            "mode_timeline": [],  # {mode, ts}
            "events": [],  # {ts, severity?, text, kind}
            "actions": [],  # {ts, name, label, ok}  — agent (STADO) tool calls
            "summary": None,  # cached model-written mission summary (generated on demand)
            "_last_fix": None,  # (lat, lon) for incremental distance
            "_last_mode": None,
        }
        log.info("flight started: vehicle=%s id=%s", self.vehicle_id, self._cur["id"])
        # Fold in any agent action issued just BEFORE arm (e.g. the takeoff
        # command itself) so the report's timeline includes the opening command.
        # Keep only those within the pre-arm window; drop the rest.
        cutoff = now - PREARM_ACTION_WINDOW_S
        recent = [a for a in self._prearm_actions if a["ts"] >= cutoff]
        if recent:
            self._cur["actions"].extend(recent)
        self._prearm_actions = []
        # Record the mode at takeoff if known.
        mode = snap.get("mode")
        if mode:
            self._cur["mode_timeline"].append({"mode": mode, "ts": now})
            self._cur["_last_mode"] = mode

    def _finalize(self, snap: dict[str, Any]) -> None:
        cur = self._cur
        if cur is None:
            return
        now = time.time()
        cur["end_ts"] = now
        lat, lon = snap.get("lat"), snap.get("lon")
        if lat is not None and lon is not None:
            cur["landing"] = {"lat": lat, "lon": lon}
        elif cur["path"]:
            last = cur["path"][-1]
            cur["landing"] = {"lat": last[0], "lon": last[1]}
        cur["duration_s"] = max(0.0, now - cur["start_ts"])
        cur["path"] = _downsample(cur["path"], MAX_PATH_POINTS)
        cur.setdefault("actions", [])
        cur.setdefault("summary", None)
        # Drop internal scratch fields before publishing.
        cur.pop("_last_fix", None)
        cur.pop("_last_mode", None)
        battery_used = None
        if cur["battery_start_pct"] is not None and cur["battery_min_pct"] is not None:
            battery_used = cur["battery_start_pct"] - cur["battery_min_pct"]
        cur["battery_used_pct"] = battery_used
        self._cur = None
        log.info(
            "flight finalized: vehicle=%s id=%s duration=%.1fs dist=%.0fm",
            self.vehicle_id, cur["id"], cur["duration_s"], cur["distance_m"],
        )
        store.add(cur)
        if self.on_complete is not None:
            try:
                self.on_complete(summarize(cur))
            except Exception:
                log.exception("flight on_complete callback failed")

    # ── feed methods (called from the telemetry loop / event path) ───────────
    def feed_telemetry(self, snap: dict[str, Any]) -> None:
        """Process one telemetry snapshot. Drives arm detection + accumulation."""
        with self._lock:
            armed = bool(snap.get("armed"))
            if armed and not self._armed:
                self._begin(snap)
            elif not armed and self._armed:
                self._finalize(snap)
            self._armed = armed

            cur = self._cur
            if cur is None:
                return

            now = time.time()
            alt = snap.get("alt_rel")
            if alt is not None and alt > cur["max_alt_m"]:
                cur["max_alt_m"] = float(alt)
            gs = snap.get("groundspeed")
            if gs is not None and gs > cur["max_speed_ms"]:
                cur["max_speed_ms"] = float(gs)
            bp = snap.get("battery_pct")
            if bp is not None:
                if cur["battery_start_pct"] is None:
                    cur["battery_start_pct"] = bp
                if cur["battery_min_pct"] is None or bp < cur["battery_min_pct"]:
                    cur["battery_min_pct"] = bp

            lat, lon = snap.get("lat"), snap.get("lon")
            if lat is not None and lon is not None:
                if cur["takeoff"] is None:
                    cur["takeoff"] = {"lat": lat, "lon": lon}
                last = cur["_last_fix"]
                if last is not None:
                    d = _haversine(last[0], last[1], lat, lon)
                    # Ignore sub-metre GPS jitter so a stationary craft doesn't
                    # accumulate phantom distance.
                    if d >= 1.0:
                        cur["distance_m"] += d
                        cur["_last_fix"] = (lat, lon)
                        cur["path"].append([lat, lon, alt if alt is not None else 0.0])
                else:
                    cur["_last_fix"] = (lat, lon)
                    cur["path"].append([lat, lon, alt if alt is not None else 0.0])

    def feed_mode(self, mode: str | None) -> None:
        """Record a mode change on the active flight's timeline."""
        if not mode:
            return
        with self._lock:
            cur = self._cur
            if cur is None:
                return
            if cur["_last_mode"] != mode:
                cur["_last_mode"] = mode
                cur["mode_timeline"].append({"mode": mode, "ts": time.time()})

    def feed_event(self, text: str, severity: int | None = None, kind: str = "statustext") -> None:
        """Record a notable statustext / event on the active flight."""
        if not text:
            return
        with self._lock:
            cur = self._cur
            if cur is None:
                return
            cur["events"].append(
                {"ts": time.time(), "severity": severity, "text": text, "kind": kind}
            )

    def feed_action(self, name: str, args: dict[str, Any] | None, result: dict[str, Any] | None) -> None:
        """Record an agent (STADO) action — a voice tool call — with a
        human-readable label and ok/failed status.

        On the active flight's timeline when armed; otherwise buffered in a small
        pre-arm ring so the opening command (e.g. takeoff) that STARTS the flight
        is folded into the timeline on arm (see `_begin`) instead of being lost."""
        if not name:
            return
        with self._lock:
            ok = True
            if isinstance(result, dict) and "ok" in result:
                ok = bool(result.get("ok"))
            entry = {
                "ts": time.time(),
                "name": name,
                "label": action_label(name, args or {}, result or {}),
                "ok": ok,
            }
            cur = self._cur
            if cur is None:
                # Buffer pre-arm actions (capped) for the next flight's _begin.
                self._prearm_actions.append(entry)
                if len(self._prearm_actions) > MAX_PREARM_ACTIONS:
                    self._prearm_actions = self._prearm_actions[-MAX_PREARM_ACTIONS:]
                return
            cur.setdefault("actions", []).append(entry)


# Two flights belong to the same MISSION if their active windows overlap, OR if
# one starts within this many seconds of an open mission window's end — drones in
# a coordinated sortie rarely arm at the exact same instant.
MISSION_GAP_S = 120.0


def _flight_window(flight: dict[str, Any]) -> tuple[float, float]:
    """The [start, end] wall-clock window of a flight, end falling back to start
    + duration (or just start) so an unfinalized/partial record still groups."""
    start = float(flight.get("start_ts") or 0.0)
    end = flight.get("end_ts")
    if end is None:
        dur = flight.get("duration_s")
        end = start + float(dur) if dur is not None else start
    return start, float(end)


def group_missions(flights: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group per-vehicle flights into MISSIONS — the set of flights whose active
    windows overlap in time (or start within MISSION_GAP_S of an open mission
    window). Returns mission dicts, newest first, each:
        {mission_id, t0, t1, duration_s, vehicles:[ids], names:[...],
         flight_ids:[...], flights:[<full flight records, sorted>]}
    The `mission_id` is derived deterministically from the earliest member flight
    (its id), so the same set of flights always yields the same mission id."""
    # Sort by start so we can sweep and extend open windows left→right.
    ordered = sorted(flights, key=lambda f: _flight_window(f)[0])
    groups: list[dict[str, Any]] = []
    for f in ordered:
        s, e = _flight_window(f)
        placed = False
        for g in groups:
            # Overlap, or this flight starts within the gap of the group window.
            if s <= g["t1"] + MISSION_GAP_S and e >= g["t0"]:
                g["members"].append(f)
                g["t0"] = min(g["t0"], s)
                g["t1"] = max(g["t1"], e)
                placed = True
                break
        if not placed:
            groups.append({"t0": s, "t1": e, "members": [f]})

    missions: list[dict[str, Any]] = []
    for g in groups:
        members = sorted(g["members"], key=lambda f: _flight_window(f)[0])
        earliest = members[0]
        mid = f"m_{earliest['id']}"
        # De-dup vehicle ids/names while preserving first-seen order.
        vehicles: list[str] = []
        names: list[str] = []
        for f in members:
            vid = f.get("vehicle_id")
            if vid and vid not in vehicles:
                vehicles.append(vid)
            nm = f.get("vehicle_name")
            if nm and nm not in names:
                names.append(nm)
        missions.append({
            "mission_id": mid,
            "t0": g["t0"],
            "t1": g["t1"],
            "duration_s": max(0.0, g["t1"] - g["t0"]),
            "vehicles": vehicles,
            "names": names,
            "flight_ids": [f["id"] for f in members],
            "flights": members,
        })
    # Newest first (by mission start).
    missions.sort(key=lambda m: m["t0"], reverse=True)
    return missions


def mission_summary(mission: dict[str, Any]) -> dict[str, Any]:
    """A compact mission summary for the list endpoint (no full flight detail)."""
    return {
        "mission_id": mission["mission_id"],
        "t0": mission["t0"],
        "t1": mission["t1"],
        "duration_s": round(mission["duration_s"], 1),
        "vehicles": mission["vehicles"],
        "names": mission["names"],
        "flight_ids": mission["flight_ids"],
        "flight_count": len(mission["flight_ids"]),
    }


def summarize(flight: dict[str, Any]) -> dict[str, Any]:
    """A compact summary suitable for the list endpoint / flight_complete event."""
    return {
        "id": flight["id"],
        "vehicle_id": flight["vehicle_id"],
        "vehicle_name": flight["vehicle_name"],
        "start_ts": flight["start_ts"],
        "end_ts": flight.get("end_ts"),
        "duration_s": flight.get("duration_s"),
        "max_alt_m": round(flight.get("max_alt_m", 0.0), 1),
        "distance_m": round(flight.get("distance_m", 0.0), 1),
        "max_speed_ms": round(flight.get("max_speed_ms", 0.0), 1),
        "battery_start_pct": flight.get("battery_start_pct"),
        "battery_min_pct": flight.get("battery_min_pct"),
        "battery_used_pct": flight.get("battery_used_pct"),
        "takeoff": flight.get("takeoff"),
        "landing": flight.get("landing"),
        "event_count": len(flight.get("events", [])),
        "action_count": len(flight.get("actions", [])),
    }


class FlightStore:
    """Module-level store of finalized flights (most recent last in `_flights`),
    capped at MAX_FLIGHTS, optionally persisted to a JSON file."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._flights: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            if STORE_PATH.exists():
                data = json.loads(STORE_PATH.read_text())
                if isinstance(data, list):
                    self._flights = data[-MAX_FLIGHTS:]
                    log.info("loaded %d persisted flight(s)", len(self._flights))
        except Exception:
            log.exception("failed to load persisted flights — starting empty")
            self._flights = []

    def _save(self) -> None:
        try:
            STORE_PATH.write_text(json.dumps(self._flights))
        except Exception:
            log.exception("failed to persist flights")

    def add(self, flight: dict[str, Any]) -> None:
        with self._lock:
            self._flights.append(flight)
            self._flights = self._flights[-MAX_FLIGHTS:]
            self._save()

    def list_summaries(self) -> list[dict[str, Any]]:
        """Most recent first."""
        with self._lock:
            return [summarize(f) for f in reversed(self._flights)]

    def get(self, flight_id: str) -> dict[str, Any] | None:
        with self._lock:
            for f in self._flights:
                if f["id"] == flight_id:
                    return f
        return None

    def list_missions(self) -> list[dict[str, Any]]:
        """Mission summaries (flights grouped by overlapping time windows),
        newest first."""
        with self._lock:
            flights = list(self._flights)
        return [mission_summary(m) for m in group_missions(flights)]

    def get_mission(self, mission_id: str) -> dict[str, Any] | None:
        """A mission with the FULL flight record of every member flight, so the
        frontend can render side-by-side and replay all drones together."""
        with self._lock:
            flights = list(self._flights)
        for m in group_missions(flights):
            if m["mission_id"] == mission_id:
                return {
                    "mission_id": m["mission_id"],
                    "t0": m["t0"],
                    "t1": m["t1"],
                    "duration_s": round(m["duration_s"], 1),
                    "vehicles": m["vehicles"],
                    "names": m["names"],
                    "flight_ids": m["flight_ids"],
                    "flights": m["flights"],  # full FlightDetail records
                }
        return None

    def set_summary(self, flight_id: str, summary: str) -> None:
        """Cache a generated mission summary on the flight and persist it, so a
        (paid) model call only happens once per flight."""
        with self._lock:
            for f in self._flights:
                if f["id"] == flight_id:
                    f["summary"] = summary
                    self._save()
                    return


# ── module-level singletons ──────────────────────────────────────────────────
store = FlightStore()

# One recorder per vehicle id, created lazily as telemetry arrives.
_recorders: dict[str, FlightRecorder] = {}
_recorders_lock = threading.Lock()
# Callback set by main.py to emit flight_complete events to the hub.
_on_complete: Callable[[dict[str, Any]], None] | None = None


def set_on_complete(cb: Callable[[dict[str, Any]], None] | None) -> None:
    """Wire the hub publisher so finalized flights emit a flight_complete event."""
    global _on_complete
    _on_complete = cb
    with _recorders_lock:
        for rec in _recorders.values():
            rec.on_complete = cb


def get_recorder(vehicle_id: str, vehicle_name: str) -> FlightRecorder:
    with _recorders_lock:
        rec = _recorders.get(vehicle_id)
        if rec is None:
            rec = FlightRecorder(vehicle_id, vehicle_name)
            rec.on_complete = _on_complete
            _recorders[vehicle_id] = rec
        return rec


def feed_telemetry(vehicle_id: str, vehicle_name: str, snap: dict[str, Any]) -> None:
    get_recorder(vehicle_id, vehicle_name).feed_telemetry(snap)


def feed_event(vehicle_id: str, event: dict[str, Any]) -> None:
    """Route a hub event (mode / statustext) to the right recorder.

    Only acts on flows that have a recorder already (i.e. telemetry seen).
    """
    with _recorders_lock:
        rec = _recorders.get(vehicle_id)
    if rec is None:
        return
    etype = event.get("type")
    if etype == "mode":
        rec.feed_mode(event.get("mode"))
    elif etype == "statustext":
        sev = event.get("severity")
        # Only notable status text (warnings and above) to avoid noise.
        if sev is None or sev <= 6:
            rec.feed_event(event.get("text", ""), sev, "statustext")
    elif etype == "voice_command":
        # An agent (STADO) tool call — record it as a timestamped action on the
        # active flight so the report shows what the agent did, and when.
        rec.feed_action(event.get("name", ""), event.get("args"), event.get("result"))
