from __future__ import annotations

import logging
import math
import queue
import threading
import time
from typing import Any, Callable

from pymavlink import mavutil

from ..logbus import logbus
from .constants import decode_px4_mode

log = logging.getLogger("gcs.mavlink")

# MAV_RESULT names for surfacing COMMAND_ACK to the UI.
_ACK_RESULT = {
    0: "ACCEPTED",
    1: "TEMPORARILY_REJECTED",
    2: "DENIED",
    3: "UNSUPPORTED",
    4: "FAILED",
    5: "IN_PROGRESS",
}

# STATUSTEXT phrases PX4 uses when it refuses to arm. Used to pick the most
# relevant line out of the burst of status text around a command attempt.
_REJECT_HINTS = ("arm", "preflight", "denied", "reject", "fail", "disarm")

# MAV_RESULT_IN_PROGRESS — PX4 emits this one or more times for long-running
# commands (takeoff, calibration) BEFORE the terminal ACCEPTED/FAILED. It is not
# a final result and must not satisfy a command_long_ack waiter (H2).
_RESULT_IN_PROGRESS = 5

# Component ids that belong to an autopilot (so its HEARTBEAT may latch the
# command target). 0 = MAV_COMP_ID_ALL (some stacks), 1 = MAV_COMP_ID_AUTOPILOT1.
_AUTOPILOT_COMPONENTS = (0, 1)

# How long telemetry may be silent before we tear the socket down and re-open it
# (re-resolving a moved UDP peer). Shorter than this just flips `connected` off.
_RECONNECT_TIMEOUT_S = 15.0
# Shorter window after which we report the link disconnected (but keep the socket).
_DISCONNECT_TIMEOUT_S = 5.0
# Reassert SET_MESSAGE_INTERVAL until each requested message is observed, for at
# least this long after a vehicle is first detected (a thin link can drop the
# first burst, so a one-shot request is not enough — H4).
_STREAM_REASSERT_WINDOW_S = 60.0
_STREAM_REASSERT_INTERVAL_S = 10.0


def _pick_reason(statustexts: list[tuple[int, str]]) -> str | None:
    """Choose the human-readable rejection cause from STATUSTEXT seen around a
    command. Prefer a line that mentions arming/preflight/denied; otherwise fall
    back to the most severe (lowest MAV_SEVERITY number) line."""
    if not statustexts:
        return None
    for _sev, text in statustexts:
        low = (text or "").lower()
        if any(h in low for h in _REJECT_HINTS):
            return text.strip()
    # No obvious arming phrase — surface the most severe message instead.
    sev, text = min(statustexts, key=lambda t: t[0])
    return text.strip() if text else None


class MavlinkLink:
    """Owns the pymavlink connection.

    A dedicated reader thread pumps incoming messages into `self.state`.
    Commands are sent under a lock from any thread (UDP sends are non-blocking).
    Notable async events (ACKs, status text, mode changes) are forwarded to a
    callback so the WebSocket hub can relay them to the UI.
    """

    def __init__(self, connection_string: str) -> None:
        self.connection_string = connection_string
        self.master: mavutil.mavfile | None = None
        self.target_system = 1
        self.target_component = 1

        # Audit-log vehicle tag. Defaults to the connection string; resolved to
        # the registry's vehicle id lazily (by link identity) so TX/RX entries
        # are vehicle-tagged without the registry having to set it explicitly.
        self._audit_vid: str | None = None

        self._send_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self.on_event: Callable[[dict[str, Any]], None] | None = None

        # COMMAND_ACK correlation. A caller (e.g. arm) registers a waiter for a
        # specific command id *before* sending; the reader thread fills it in when
        # the matching COMMAND_ACK arrives and also tees STATUSTEXT into every open
        # waiter so the human-readable reject reason ("Arming denied: …") is
        # captured alongside the ACK. Guarded by its own lock so the reader thread
        # and command callers never touch the dict concurrently.
        self._ack_lock = threading.Lock()
        self._ack_waiters: list[dict[str, Any]] = []
        # Monotonic token so the OLDEST matching waiter for a command id is
        # resolved first when several overlap (H1).
        self._ack_seq = 0

        # Progress-aware ACK waiters (used by the autotune 1 Hz poll). Unlike the
        # H2 waiters above, these accept MAV_RESULT_IN_PROGRESS and capture the
        # ACK's `progress` byte (0-100) — PX4's non-standard way of reporting
        # autotune progress on a re-sent MAV_CMD_DO_AUTOTUNE_ENABLE. The reader
        # resolves the OLDEST matching waiter on the FIRST ack of that command id
        # from our autopilot (terminal OR in-progress). Same lock as above.
        self._ack_progress_waiters: list[dict[str, Any]] = []

        # STATUSTEXT subscribers. The reader thread tees every decoded STATUSTEXT
        # (severity, text) into each registered callback so a feature like autotune
        # can observe PX4's "Autotune: …" progress lines WITHOUT spinning a second
        # socket reader. Subscribers are best-effort/supplementary (Outrider's DDS
        # transport never delivers STATUSTEXT, so nothing may ever fire) and must
        # never raise into the reader. Guarded by its own lock so register/remove
        # from a command thread can't race the reader's iteration.
        self._statustext_lock = threading.Lock()
        self._statustext_subs: list[Callable[[int, str], None]] = []

        # Stream-request reassertion (H4): a short-lived worker reasserts
        # SET_MESSAGE_INTERVAL until each message type is observed (or the window
        # elapses), off the reader thread so RX never stalls. `_observed_msgs`
        # records which requested types have actually arrived.
        self._observed_msgs: set[str] = set()
        self._stream_thread: threading.Thread | None = None

        # Mission-protocol messages are funnelled here for the uploader to drive
        # the request/ack handshake (the reader thread is the only socket reader).
        self.mission_q: "queue.Queue[tuple[str, Any]]" = queue.Queue()

        # PX4 sends MISSION_CURRENT at ~1 Hz regardless of whether the waypoint
        # changed; only surface transitions so the console isn't flooded.
        self._last_mission_seq: int | None = None

        self.state: dict[str, Any] = {
            "connected": False,
            "armed": False,
            "mode": None,
            "lat": None,
            "lon": None,
            "alt_msl": None,
            "alt_rel": None,
            "heading": None,
            "roll": None,
            "pitch": None,
            "yaw": None,
            "groundspeed": None,
            "airspeed": None,
            "climb": None,
            "throttle": None,
            "battery_pct": None,
            "battery_voltage": None,
            "battery_current": None,
            "gps_fix": None,
            "satellites": None,
            "vx": None,
            "vy": None,
            "vz": None,
            "home_lat": None,
            "home_lon": None,
            "last_heartbeat": 0.0,
        }

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._run, name="mavlink-reader", daemon=True)
        self._thread.start()
        # A GCS must announce itself: many links (e.g. SIYI datalinks) only start
        # streaming telemetry once they hear a GCS heartbeat.
        threading.Thread(target=self._heartbeat_loop, name="mavlink-heartbeat", daemon=True).start()

    def _heartbeat_loop(self) -> None:
        while self._running:
            try:
                with self._send_lock:
                    # Snapshot master under the lock so a concurrent reconnect
                    # swap (which sets it to None under the same lock) can't be
                    # used mid-send (M1).
                    m = self.master
                    if m is not None:
                        m.mav.heartbeat_send(
                            mavutil.mavlink.MAV_TYPE_GCS,
                            mavutil.mavlink.MAV_AUTOPILOT_INVALID,
                            0, 0, 0,
                        )
            except Exception:
                pass
            time.sleep(1.0)

    def stop(self) -> None:
        self._running = False
        if self.master is not None:
            try:
                self.master.close()
            except Exception:
                pass

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                log.exception("event callback failed")

    # ── audit logging (purely additive; never raises into the caller) ───────
    def _audit_vehicle(self) -> str:
        """Resolve this link's vehicle id for the audit log. Best-effort: match
        the registry by link identity, else fall back to the connection string.
        Cached after the first successful resolution."""
        if self._audit_vid is not None:
            return self._audit_vid
        try:
            from .registry import registry

            for v in registry.list():
                if v.link is self:
                    self._audit_vid = v.id
                    return self._audit_vid
        except Exception:
            pass
        return self.connection_string

    def _audit_tx(self, kind: str, command: Any, params: Any) -> None:
        """Log one OUTBOUND command/setpoint. Never raises, never blocks."""
        try:
            if not logbus.enabled:
                return
            logbus.log(
                "mavlink",
                "out",
                self._audit_vehicle(),
                kind=kind,
                command=command,
                params=params,
            )
        except Exception:
            pass

    def _audit_in(self, kind: str, **fields: Any) -> None:
        """Log one INBOUND decoded MAVLink event. Never raises, never blocks."""
        try:
            if not logbus.enabled:
                return
            logbus.log("mavlink", "in", self._audit_vehicle(), kind=kind, **fields)
        except Exception:
            pass

    def _install_tx_taps(self) -> None:
        """Wrap raw `self.master.mav` send functions so TX that bypasses the
        link's own methods (velocity setpoints in commands.py, mission uploads
        in missions.py) is also audited — without editing those modules. The
        high-rate offboard velocity stream is throttled so it doesn't flood."""
        try:
            if not logbus.enabled or self.master is None:
                return
            mav = self.master.mav
            if getattr(mav, "_gcs_audit_tapped", False):
                return

            def _wrap(name: str, kind: str, throttle_key: str | None = None):
                orig = getattr(mav, name, None)
                if orig is None or not callable(orig):
                    return

                def _tapped(*args, **kwargs):
                    result = orig(*args, **kwargs)
                    try:
                        if throttle_key is None or logbus.allow(
                            self._audit_vehicle(), throttle_key
                        ):
                            self._audit_tx(kind, name, {"args": list(args)})
                    except Exception:
                        pass
                    return result

                setattr(mav, name, _tapped)

            # Offboard velocity / position setpoints — high-rate, so throttle.
            _wrap("set_position_target_local_ned_send", "setpoint", "setpoint_ned")
            _wrap("set_position_target_global_int_send", "setpoint", "setpoint_global")
            _wrap("set_attitude_target_send", "setpoint", "setpoint_attitude")
            # Mission upload protocol (full fidelity — low rate).
            _wrap("mission_count_send", "mission")
            _wrap("mission_item_int_send", "mission")
            _wrap("mission_clear_all_send", "mission")
            mav._gcs_audit_tapped = True
        except Exception:
            pass

    def _run(self) -> None:
        while self._running:
            try:
                log.info("connecting MAVLink: %s", self.connection_string)
                with self._send_lock:
                    self.master = mavutil.mavlink_connection(
                        self.connection_string, autoreconnect=True, source_system=255
                    )
                self._install_tx_taps()
                log.info("link opened — waiting for vehicle telemetry…")
                # _read_loop returns (rather than raising) on a long telemetry
                # timeout so we can close + reopen the socket below to re-resolve a
                # moved UDP peer (H5).
                self._read_loop()
            except Exception as exc:  # noqa: BLE001
                log.warning("MAVLink link error: %s — retrying in 2s", exc)
            # Reaching here means the link is being (re)established: tear the old
            # socket down and reset detection so a moved/replaced vehicle is
            # re-detected from scratch (H5).
            self._reset_link()
            if self._running:
                time.sleep(2)

    def _reset_link(self) -> None:
        """Close the current socket and reset all per-connection detection state
        so the next _run iteration re-detects the vehicle cleanly (H5)."""
        old = self.master
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        with self._send_lock:
            self.master = None
        with self._state_lock:
            self.state["connected"] = False
            # Reset the command target so a moved/replaced vehicle on the same
            # connection string re-latches from its autopilot HEARTBEAT (C2/H5).
            self.target_system = 1
            self.target_component = 1
        self._observed_msgs = set()
        self._emit({"type": "link", "connected": False})
        self._audit_in("link", connected=False, reason="reconnect")

    # Requested telemetry: (get_type() name, Hz). The name is both the
    # MAVLINK_MSG_ID_<name> suffix used to build the SET_MESSAGE_INTERVAL request
    # AND what arrives as get_type(), so the reassertion worker (H4) can stop
    # re-requesting a stream once it's actually observed. SYS_STATUS/BATTERY_STATUS
    # carry battery; GLOBAL_POSITION_INT carries fused alt + lat/lon (after GPS lock).
    _STREAM_REQUESTS = (
        ("SYS_STATUS", 2),
        ("BATTERY_STATUS", 2),
        ("GLOBAL_POSITION_INT", 5),
        ("GPS_RAW_INT", 2),
        ("VFR_HUD", 5),
        ("ATTITUDE", 10),
    )

    def _request_streams(self) -> None:
        """Send ONE burst of telemetry-rate requests.

        PX4 ignores the legacy MAV_DATA_STREAM_ALL and only streams whatever its
        MAVLink instance is configured for — so over a thin link (e.g. SIYI's
        serial bridge) battery (SYS_STATUS/BATTERY_STATUS) can simply never arrive
        even though HEARTBEAT/VFR_HUD/ATTITUDE do. We therefore ALSO request each
        message we need explicitly via SET_MESSAGE_INTERVAL, which both PX4 and
        ArduPilot honour per-message regardless of stream-group config.

        Called by the short-lived reassertion worker (NOT the reader thread) and
        repeated until each type is observed, so a dropped first burst doesn't
        permanently starve telemetry (H4).
        """
        try:
            self.master.mav.request_data_stream_send(
                self.target_system,
                self.target_component,
                mavutil.mavlink.MAV_DATA_STREAM_ALL,
                10,  # Hz
                1,
            )
        except Exception:
            log.debug("request_data_stream not supported, relying on defaults")
        mav = mavutil.mavlink
        for name, hz in self._STREAM_REQUESTS:
            if name in self._observed_msgs:
                continue  # already arriving — don't re-request
            msg_id = getattr(mav, f"MAVLINK_MSG_ID_{name}")
            try:
                self.command_long(
                    mav.MAV_CMD_SET_MESSAGE_INTERVAL, float(msg_id), float(int(1_000_000 / hz))
                )
            except Exception:
                log.debug("SET_MESSAGE_INTERVAL %d not supported", msg_id)
        # Explicitly request HOME_POSITION (id 242) — PX4 only emits it on change.
        try:
            self.command_long(mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE, 242.0)
        except Exception:
            log.debug("HOME_POSITION request not supported")

    def _start_stream_reassert(self) -> None:
        """Spawn a short-lived worker that (re)asserts SET_MESSAGE_INTERVAL off the
        reader thread until every requested type is observed or the window elapses.
        Idempotent — a still-running worker is reused (H4)."""
        t = self._stream_thread
        if t is not None and t.is_alive():
            return
        self._stream_thread = threading.Thread(
            target=self._stream_reassert_loop, name="mavlink-streams", daemon=True
        )
        self._stream_thread.start()

    def _stream_reassert_loop(self) -> None:
        deadline = time.time() + _STREAM_REASSERT_WINDOW_S
        while self._running and self.master is not None and time.time() < deadline:
            self._request_streams()
            # Stop early once every requested stream has been seen.
            if all(name in self._observed_msgs for name, _ in self._STREAM_REQUESTS):
                break
            slept = 0.0
            while (
                self._running and slept < _STREAM_REASSERT_INTERVAL_S
                and self.master is not None
            ):
                time.sleep(0.25)
                slept += 0.25

    def _read_loop(self) -> None:
        last_msg = time.time()
        while self._running:
            msg = self.master.recv_match(blocking=True, timeout=1)
            if msg is None:
                silent = time.time() - last_msg
                # Beyond the long threshold: give up on this socket and return so
                # _run closes + reopens it (re-resolving a moved UDP peer — H5). A
                # dead udpout socket never raises out of recv_match on its own.
                if silent > _RECONNECT_TIMEOUT_S:
                    log.warning(
                        "telemetry silent %.0fs — reopening link to re-resolve peer",
                        silent,
                    )
                    return
                if silent > _DISCONNECT_TIMEOUT_S and self.state["connected"]:
                    with self._state_lock:
                        self.state["connected"] = False
                    self._emit({"type": "link", "connected": False})
                    self._audit_in("link", connected=False, reason="telemetry timeout")
                continue
            mtype = msg.get_type()
            if mtype == "BAD_DATA":
                continue
            last_msg = time.time()
            # NOTE: the command target is latched ONLY from an autopilot HEARTBEAT
            # in _handle (C2) — never from the first arbitrary source — so a SIYI
            # gimbal/companion (compid 154) can't win the target.
            self._handle(msg, mtype)

    # ── message handling ──────────────────────────────────────────────────
    def _maybe_latch_target(self, msg: Any) -> bool:
        """C2: latch the command target ONLY from an autopilot-bearing HEARTBEAT,
        never from the first arbitrary source. Returns True when this call newly
        latched the target (so the caller can kick off stream requests once).

        An autopilot heartbeat is one whose srcComponent is 0/1 AND whose
        `autopilot` field isn't MAV_AUTOPILOT_INVALID (so a SIYI gimbal compid 154,
        or a GCS heartbeat with autopilot=INVALID, can never win the target)."""
        if self.state["connected"]:
            return False
        if msg.get_srcComponent() not in _AUTOPILOT_COMPONENTS:
            return False
        autopilot = getattr(msg, "autopilot", None)
        if autopilot is not None and autopilot == mavutil.mavlink.MAV_AUTOPILOT_INVALID:
            return False
        sysid = msg.get_srcSystem()
        self.target_system = int(sysid)
        self.target_component = int(msg.get_srcComponent() or 1)
        return True

    def _handle(self, msg: Any, mtype: str) -> None:  # noqa: C901
        s = self.state
        # Events to emit / audit are collected under the lock and dispatched
        # AFTER it is released — _emit invokes arbitrary user callbacks and must
        # never run while holding _state_lock (M6).
        events: list[dict[str, Any]] = []
        audits: list[tuple[str, dict[str, Any]]] = []
        newly_latched = False
        with self._state_lock:
            if mtype == "HEARTBEAT":
                # The GCS's own heartbeat is sourced from sysid 255; a 0/255 source
                # (looped-back GCS heartbeat, or broadcast) must NOT mark the link
                # connected or drive arm/mode — that's a phantom vehicle (H6).
                if msg.get_srcSystem() in (0, 255):
                    return
                # Only the autopilot's heartbeat carries flight mode / arm state;
                # ignore gimbal/companion/other component heartbeats.
                if msg.get_srcComponent() not in _AUTOPILOT_COMPONENTS:
                    return
                # Latch the command target from this autopilot heartbeat the first
                # time we see one (C2) — never from an earlier arbitrary source.
                newly_latched = self._maybe_latch_target(msg)
                if newly_latched:
                    audits.append(
                        ("link", {"connected": True, "system": int(self.target_system),
                                  "component": int(self.target_component)})
                    )
                    log.info(
                        "vehicle detected: system %d component %d",
                        self.target_system, self.target_component,
                    )
                if not s["connected"]:
                    events.append({"type": "link", "connected": True})
                s["connected"] = True
                s["last_heartbeat"] = time.time()
                armed = bool(msg.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
                mode = decode_px4_mode(msg.custom_mode)
                if mode != s["mode"]:
                    events.append({"type": "mode", "mode": mode})
                    audits.append(("mode", {"mode": mode, "prev": s["mode"]}))
                if armed != s["armed"]:
                    events.append({"type": "armed", "armed": armed})
                    audits.append(("armed", {"armed": armed}))
                s["armed"] = armed
                s["mode"] = mode
            elif mtype == "GLOBAL_POSITION_INT":
                s["lat"] = msg.lat / 1e7
                s["lon"] = msg.lon / 1e7
                s["alt_msl"] = msg.alt / 1000.0
                s["alt_rel"] = msg.relative_alt / 1000.0
                s["heading"] = msg.hdg / 100.0 if msg.hdg != 65535 else s["heading"]
                s["vx"] = msg.vx / 100.0
                s["vy"] = msg.vy / 100.0
                s["vz"] = msg.vz / 100.0
            elif mtype == "ATTITUDE":
                s["roll"] = msg.roll
                s["pitch"] = msg.pitch
                s["yaw"] = msg.yaw
            elif mtype == "VFR_HUD":
                s["groundspeed"] = msg.groundspeed
                s["airspeed"] = msg.airspeed
                s["climb"] = msg.climb
                s["throttle"] = msg.throttle
                if s["heading"] is None:
                    s["heading"] = float(msg.heading)
            elif mtype in ("SYS_STATUS", "BATTERY_STATUS"):
                if mtype == "SYS_STATUS":
                    if msg.battery_remaining != -1:
                        s["battery_pct"] = msg.battery_remaining
                    if msg.voltage_battery not in (0, 65535):
                        s["battery_voltage"] = msg.voltage_battery / 1000.0
                    if msg.current_battery != -1:
                        s["battery_current"] = msg.current_battery / 100.0
                else:
                    if msg.battery_remaining != -1:
                        s["battery_pct"] = msg.battery_remaining
            elif mtype == "GPS_RAW_INT":
                if msg.fix_type != s["gps_fix"]:
                    audits.append((
                        "gps_fix",
                        {"gps_fix": int(msg.fix_type), "prev": s["gps_fix"],
                         "satellites": int(msg.satellites_visible)},
                    ))
                s["gps_fix"] = msg.fix_type
                s["satellites"] = msg.satellites_visible
            elif mtype == "HOME_POSITION":
                s["home_lat"] = msg.latitude / 1e7
                s["home_lon"] = msg.longitude / 1e7
            elif mtype == "COMMAND_ACK":
                # H1: only accept ACKs from OUR autopilot — filter by source so a
                # looped-back GCS ACK or a second vehicle/gimbal can't satisfy an
                # arm/takeoff waiter with a false result.
                if self._ack_from_autopilot(msg):
                    _result = _ACK_RESULT.get(int(msg.result), str(msg.result))
                    self._notify_ack(int(msg.command), int(msg.result))
                    # Feed progress-aware waiters (autotune poll) the FULL ack —
                    # including MAV_RESULT_IN_PROGRESS and the `progress` byte, which
                    # _notify_ack deliberately drops (H2).
                    self._notify_ack_progress(
                        int(msg.command), int(msg.result),
                        int(getattr(msg, "progress", 0) or 0),
                    )
                    events.append(
                        {"type": "ack", "command": int(msg.command), "result": _result}
                    )
                    audits.append(("ack", {"command": int(msg.command), "result": _result}))
            elif mtype == "STATUSTEXT":
                text = msg.text.decode() if isinstance(msg.text, bytes) else msg.text
                self._notify_statustext(int(msg.severity), text)
                events.append({"type": "statustext", "severity": int(msg.severity), "text": text})
                audits.append(("statustext", {"severity": int(msg.severity), "text": text}))
            elif mtype in (
                "MISSION_REQUEST", "MISSION_REQUEST_INT", "MISSION_ACK",
                "MISSION_COUNT", "MISSION_ITEM_INT",
            ):
                # MISSION_COUNT / MISSION_ITEM_INT (incoming) feed the download
                # path; the request/ack messages drive the upload handshake (H3).
                self.mission_q.put((mtype, msg))
            elif mtype == "MISSION_CURRENT":
                seq = int(msg.seq)
                if seq != self._last_mission_seq:
                    self._last_mission_seq = seq
                    events.append({"type": "mission_current", "seq": seq})
                    audits.append(("mission_current", {"seq": seq}))
            elif mtype == "MISSION_ITEM_REACHED":
                events.append({"type": "waypoint_reached", "seq": int(msg.seq)})
                audits.append(("waypoint_reached", {"seq": int(msg.seq)}))

        # H4: note that this telemetry type was actually observed so the stream
        # reassertion worker stops re-requesting it (cheap, lock-free set add).
        if mtype in self._observed_msgs or any(mtype == n for n, _ in self._STREAM_REQUESTS):
            self._observed_msgs.add(mtype)

        # M6: dispatch emits/audit OUTSIDE the state lock.
        for ev in events:
            self._emit(ev)
        for kind, fields in audits:
            self._audit_in(kind, **fields)
        # First autopilot lock → kick off stream requests off the reader thread (H4).
        if newly_latched:
            self._start_stream_reassert()

    def _ack_from_autopilot(self, msg: Any) -> bool:
        """H1: a COMMAND_ACK is for us only if it came from our latched autopilot —
        matching system id and an autopilot component (0/1 or the latched one)."""
        try:
            if int(msg.get_srcSystem()) != int(self.target_system):
                return False
            comp = int(msg.get_srcComponent())
        except Exception:
            return True  # be permissive if the fake/msg lacks source accessors
        return comp in (0, 1, int(self.target_component))

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            # PX4 sends NaN/Inf for unavailable fields; those aren't JSON-safe.
            return {
                k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                for k, v in self.state.items()
            }

    # ── COMMAND_ACK correlation (used by arm/disarm to report the real outcome) ─
    def _notify_ack(self, command: int, result: int) -> None:
        """Reader-thread side: hand a COMMAND_ACK to the OLDEST open waiter for that
        command id (H1). MAV_RESULT_IN_PROGRESS (5) is NOT a final result — PX4
        emits it before the terminal ACCEPTED/FAILED for long-running commands, so
        we ignore it and keep waiting; the timeout is the backstop (H2)."""
        if result == _RESULT_IN_PROGRESS:
            return
        with self._ack_lock:
            # Oldest-first: waiters are appended in order with a monotonic token,
            # so the earliest unresolved waiter for this id is answered first.
            candidates = [
                w for w in self._ack_waiters
                if w["command"] == command and w["result"] is None
            ]
            if not candidates:
                return
            w = min(candidates, key=lambda x: x["token"])
            w["result"] = result
            w["event"].set()

    def _notify_ack_progress(self, command: int, result: int, progress: int) -> None:
        """Reader-thread side: hand the FULL ack (result + progress) to the OLDEST
        open progress-waiter for that command id. Unlike _notify_ack this ACCEPTS
        MAV_RESULT_IN_PROGRESS — that's exactly the case autotune polling needs."""
        with self._ack_lock:
            candidates = [
                w for w in self._ack_progress_waiters
                if w["command"] == command and w["result"] is None
            ]
            if not candidates:
                return
            w = min(candidates, key=lambda x: x["token"])
            w["result"] = result
            w["progress"] = progress
            w["event"].set()

    def command_long_ack_progress(
        self, command: int, *params: float, timeout: float = 1.5, confirmation: int = 0
    ) -> dict[str, Any]:
        """Send a COMMAND_LONG and BLOCK (off the socket reader) for the FIRST
        matching COMMAND_ACK from our autopilot, capturing both the MAV_RESULT and
        the `progress` byte. Used by the autotune 1 Hz poll: PX4 returns
        IN_PROGRESS(5)+progress while a tune runs and ACCEPTED(0) when done.

        Returns {result, result_name, progress, timed_out}. `result` is the
        MAV_RESULT int (None on timeout). Run from a worker thread
        (asyncio.to_thread) so the event loop / reader thread are never blocked."""
        waiter: dict[str, Any] = {
            "command": int(command),
            "result": None,
            "progress": 0,
            "event": threading.Event(),
            "token": 0,
        }
        with self._ack_lock:
            self._ack_seq += 1
            waiter["token"] = self._ack_seq
            self._ack_progress_waiters.append(waiter)
        try:
            self.command_long(command, *params, confirmation=confirmation)
            got = waiter["event"].wait(timeout)
            result = waiter["result"]
            progress = waiter["progress"]
        finally:
            with self._ack_lock:
                try:
                    self._ack_progress_waiters.remove(waiter)
                except ValueError:
                    pass
        return {
            "result": result,
            "result_name": _ACK_RESULT.get(result, None) if result is not None else None,
            "progress": int(progress or 0),
            "timed_out": not got,
        }

    def _notify_statustext(self, severity: int, text: str) -> None:
        """Reader-thread side: tee STATUSTEXT into every open waiter so the reject
        reason emitted around the command (PX4: 'Arming denied: …') is captured,
        and fan it out to every registered STATUSTEXT subscriber (e.g. autotune's
        progress lines). Subscriber callbacks must never raise into the reader."""
        with self._ack_lock:
            for w in self._ack_waiters:
                w["statustexts"].append((severity, text))
        # Snapshot the subscriber list under its lock, then call OUTSIDE the lock so
        # a slow/blocking callback can't stall the reader or deadlock on re-entry.
        with self._statustext_lock:
            subs = list(self._statustext_subs)
        for cb in subs:
            try:
                cb(severity, text)
            except Exception:  # noqa: BLE001 — a subscriber must never break RX
                log.exception("statustext subscriber failed")

    def subscribe_statustext(self, cb: Callable[[int, str], None]) -> Callable[[], None]:
        """Register a STATUSTEXT subscriber `cb(severity, text)`; returns a
        zero-arg unsubscribe. Supplementary only — on a transport that never
        delivers STATUSTEXT (Outrider/DDS) the callback simply never fires."""
        with self._statustext_lock:
            self._statustext_subs.append(cb)

        def _unsub() -> None:
            with self._statustext_lock:
                try:
                    self._statustext_subs.remove(cb)
                except ValueError:
                    pass

        return _unsub

    def command_long_ack(
        self, command: int, *params: float, timeout: float = 3.0, confirmation: int = 0
    ) -> dict[str, Any]:
        """Send a COMMAND_LONG and BLOCK (off the socket reader) for its
        COMMAND_ACK. Returns {accepted, result, result_name, reason, statustexts}.

        `result` is the MAV_RESULT int (None on timeout). `reason` is the most
        relevant PX4 STATUSTEXT seen in the window (the human-readable rejection
        cause), or None. Run from a worker thread (asyncio.to_thread) so the event
        loop isn't blocked — the actual socket reading stays on the reader thread."""
        waiter: dict[str, Any] = {
            "command": int(command),
            "result": None,
            "event": threading.Event(),
            "statustexts": [],
            "token": 0,
        }
        with self._ack_lock:
            self._ack_seq += 1
            waiter["token"] = self._ack_seq
            self._ack_waiters.append(waiter)
        try:
            self.command_long(command, *params, confirmation=confirmation)
            got = waiter["event"].wait(timeout)
            # Give PX4 a beat to emit a trailing STATUSTEXT explaining a rejection
            # (the ACK and the reason text don't always arrive in the same frame).
            if got and waiter["result"] not in (None, 0):
                time.sleep(0.4)
            result = waiter["result"]
            texts = list(waiter["statustexts"])
        finally:
            with self._ack_lock:
                try:
                    self._ack_waiters.remove(waiter)
                except ValueError:
                    pass

        result_name = _ACK_RESULT.get(result, None) if result is not None else None
        reason = _pick_reason(texts)
        return {
            "accepted": result == 0,
            "result": result,
            "result_name": result_name,
            "reason": reason,
            "statustexts": [t for _, t in texts],
        }

    # ── send helpers (thread-safe) ──────────────────────────────────────────
    def _target(self) -> tuple[int, int]:
        """Snapshot (target_system, target_component) atomically under the state
        lock so a command is never built from a half-updated pair (M1)."""
        with self._state_lock:
            return self.target_system, self.target_component

    def command_long(self, command: int, *params: float, confirmation: int = 0) -> None:
        p = list(params) + [0.0] * (7 - len(params))
        tsys, tcomp = self._target()
        self._audit_tx("command", int(command), {"params": list(params), "confirmation": confirmation})
        with self._send_lock:
            # Snapshot master under the send lock so a concurrent reconnect swap
            # (which also takes the send lock) can't hand us a closed object (M1).
            m = self.master
            if m is None:
                return
            m.mav.command_long_send(
                tsys, tcomp,
                command, confirmation, p[0], p[1], p[2], p[3], p[4], p[5], p[6],
            )

    def command_int(
        self, command: int, x: int, y: int, z: float,
        p1: float = 0, p2: float = 0, p3: float = 0, p4: float = 0,
        frame: int = mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
    ) -> None:
        tsys, tcomp = self._target()
        self._audit_tx(
            "command_int",
            int(command),
            {"x": x, "y": y, "z": z, "p1": p1, "p2": p2, "p3": p3, "p4": p4, "frame": int(frame)},
        )
        with self._send_lock:
            m = self.master
            if m is None:
                return
            m.mav.command_int_send(
                tsys, tcomp, frame, command,
                0, 0, p1, p2, p3, p4, x, y, z,
            )

    def set_mode(self, base_mode: int, main: int, sub: int) -> None:
        # Log the high-level intent; the underlying command_long is also logged.
        self._audit_tx(
            "set_mode", int(mavutil.mavlink.MAV_CMD_DO_SET_MODE),
            {"base_mode": int(base_mode), "main": int(main), "sub": int(sub)},
        )
        self.command_long(
            mavutil.mavlink.MAV_CMD_DO_SET_MODE, float(base_mode), float(main), float(sub)
        )


# Legacy single-link slot. Kept for backward compatibility, but the registry's
# active vehicle takes precedence once vehicles are registered. `init_link()`
# still works (it sets this slot) for any standalone / test usage.
link: MavlinkLink | None = None


def get_link() -> MavlinkLink:
    """Return the ACTIVE vehicle's link.

    Backward-compatible: callers pass no args and get the active vehicle's
    `MavlinkLink`. Resolution order:
      1. the registry's active vehicle (the multi-vehicle path), if registered;
      2. the legacy `init_link()` singleton, for standalone use.
    """
    # Imported lazily to avoid a circular import (registry imports this module).
    from .registry import registry

    if registry.active_id() is not None:
        return registry.active_vehicle().link
    if link is None:
        raise RuntimeError("MAVLink link not initialised")
    return link


def init_link(connection_string: str) -> MavlinkLink:
    global link
    link = MavlinkLink(connection_string)
    return link
