from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, loaded from environment / .env."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Routed by relay/mavrouter.py so QGC (14550→14552) and this GCS (14551)
    # can share one vehicle link. Point straight at the vehicle if not routing.
    # This is the primary/active vehicle's connection (Overwatch).
    mavlink_connection: str = "udpin:0.0.0.0:14551"
    # Secondary vehicle (Outrider). Distinct safe placeholder by default — it
    # simply stays disconnected (MavlinkLink retries) until a real link exists.
    #
    # Outrider is a PX4 quadcopter with a Jetson onboard (uXRCE-DDS + the
    # stado_jetson stack). The GCS reaches it over WiFi. The CHOSEN integration
    # is plain MAVLink-over-WiFi (PX4 streams MAVLink to the Jetson, which
    # forwards over WiFi — or PX4 streams direct), so this stays an ordinary
    # pymavlink connection string. Point it at the Jetson once the link is real:
    #   OUTRIDER_CONNECTION=udpout:<jetson-ip>:14550
    # No DDS/ROS2 dependency is added to this backend. The (optional, separate)
    # onboard-tracking control channel is configured by `outrider_jetson_*`.
    outrider_connection: str = "udpin:0.0.0.0:14541"

    # ── Outrider Jetson onboard-tracking control channel (OPTIONAL) ──────────
    # The Jetson runs target tracking ONBOARD (low-latency follow loop). The GCS
    # seeds the initial track region (via Qwen vision grounding) and the Jetson holds
    # + follows it locally. This is a SEPARATE channel from MAVLink telemetry —
    # a small HTTP/WebSocket endpoint on the Jetson. Empty host => feature OFF
    # (the stub reports "not configured" and changes nothing).
    # FLAG: transport (HTTP vs WS vs ROS2 topic) is a user decision — see the
    # JetsonTrackClient stub in app/mavlink/jetson.py.
    outrider_jetson_host: str = ""          # Jetson WiFi IP, e.g. "192.168.x.y"
    outrider_jetson_track_port: int = 8770  # control channel (track seed/status)
    outrider_jetson_video_port: int = 8080  # stado_vision MJPEG server port
    # Onboard VIO tracker (oakd_tracker) UDP control port — SEED/CLEAR/BOX. The
    # reticle is burned into Outrider's RGB stream. See app/onboard_track.py.
    outrider_onboard_track_port: int = 8771
    # Default follow speed/behaviour envelope ("profile") the onboard controller
    # flies when the GCS can't infer the target class (operator said only "follow
    # it"). The GCS AUTO-selects per target: "follow the car" → car, "the person"
    # → person; this is the fallback. Per-class numbers (max fwd/lat speed, lead,
    # yaw-rate, hold-range) live in the onboard controller — see
    # reviews/outrider-follow-readiness.md. "person" is the conservative default
    # (slower, no lateral, short hold-range).
    outrider_follow_profile: str = "person"
    # ── GCS-IP beacon (makes Outrider's video transport IP-INDEPENDENT) ───────
    # Outrider PUSHES its OAK-D RGB (H.264/MPEG-TS/UDP) to THIS host. The Mac's
    # DHCP IP changes, so the backend periodically sends a tiny UDP "beacon" to
    # the Jetson; the Jetson's outrider-discovery daemon reads the beacon's source
    # IP (= our current IP) and re-points the push at it. Gated on
    # outrider_jetson_host being set; 0 interval disables. See app/gcs_beacon.py.
    outrider_beacon_port: int = 5599
    outrider_beacon_interval_s: float = 5.0
    # MJPEG/RTSP the GCS grabs to SEED the tracker box. CRITICAL: this MUST be the
    # SAME stream the onboard CSRT tracker runs on (stado_vision tracks `rgb_frame`
    # = the `/rgb` video, 640x480, 4:3) — a normalized box is only valid if the
    # seed image and the tracker image share the same aspect/crop. Do NOT point it
    # at `/preview` (416x416 square) or a differently-cropped feed. Left empty, it
    # derives to `http://<host>:<video_port>/rgb` so it's the tracker's stream by
    # construction (see voice.grab_outrider_frame).
    outrider_jetson_video_url: str = ""

    gcs_host: str = "0.0.0.0"
    gcs_port: int = 8000
    # OPTIONAL shared-secret for the flight-command surface. When set, the command
    # router, the voice WS, the telemetry WS, and the log-stream all require it via
    # an `X-API-Token` header (or `?token=` query param for WebSockets, which can't
    # set headers from the browser). Empty (the default) = NO auth: every endpoint
    # is reachable by anyone who can hit the host. We bind 0.0.0.0, so an empty
    # token is logged as a prominent startup WARNING (see main.py) — this surface
    # can arm + fly real aircraft. Set GCS_API_TOKEN to lock it down.
    api_token: str = ""
    rtsp_url: str = ""
    # Optional vision input override (file path or RTSP); falls back to rtsp_url.
    video_source: str = ""
    camera_hfov_deg: float = 69.0
    # Camera mount tilt below the horizontal, in degrees ("downward pitch"). The
    # camera's optical axis points this many degrees BELOW the drone's forward
    # horizon: 0 = level with the horizon, 90 = straight down (nadir). We have no
    # live gimbal-angle feedback on the SIYI link, so this is the assumed FIXED
    # mount/gimbal depression used to project image pixels onto the ground (see
    # vision/follow.geolocate_box and ally_overlay.project_world_to_pixel). The
    # drone's own pitch (from ATTITUDE) is folded in on top of this.
    #
    # BOTH vehicles' cameras are mounted at the SAME 15 deg below horizontal —
    # Overwatch's SIYI and Outrider's OAK-D Pro (RGB). The GCS vision pipeline
    # processes the Overwatch feed, so `camera_pitch_deg` drives the on-map
    # geolocation + the ally-overlay reverse projection.
    camera_pitch_deg: float = 15.0
    # Outrider's OAK-D Pro (RGB) tilt — kept as a separate knob in case the mounts
    # ever diverge, but currently MATCHES Overwatch (15 deg). Used when Outrider's
    # onboard tracker reports geolocations (Jetson side / future).
    outrider_camera_pitch_deg: float = 15.0
    # How often (Hz) the map-objects geolocation task runs off CACHED tracks.
    # Kept low (3–4 Hz) so it never competes with the 30 fps capture thread.
    map_objects_hz: float = 3.5

    # ── Qwen models (Alibaba Cloud Model Studio) ─────────────────────────────
    # One key drives everything: the realtime voice session (native WebSocket
    # protocol — see voice_qwen.py) and the chat-completions calls below.
    dashscope_api_key: str = ""
    # OpenAI-compatible chat-completions endpoint for the non-realtime models.
    qwen_openai_base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    # Vision: single-frame target boxes, plate reads, scene description,
    # satellite survey-perimeter detection.
    qwen_vision_model: str = "qwen3.7-plus"
    # Reasoning/text: mission-report summary generation.
    qwen_reasoning_model: str = "qwen3.7-max"
    # Target-acquisition backend for "track the <description>": "qwen" | "moondream"
    grounding_backend: str = "qwen"
    yolo_model: str = "yolo11m.pt"  # 'm' needed for small/aerial targets
    # Periodically re-anchor the CSRT lock with the VLM to correct drift (seconds).
    # 0 = only re-acquire when the lock is lost (cheaper, less accurate on aerial).
    reanchor_s: float = 1.0

    # ── Vehicle identification (anonymized) ──────────────────────────────────
    # Pluggable provider for make/model/owner. Empty url => deterministic MOCK.
    # When set, it's GET with the plate; the key is sent as header + query param.
    # Owner names are ALWAYS masked regardless of source.
    vehicle_api_url: str = ""
    vehicle_api_key: str = ""
    # How often (seconds) to attempt a plate read on the locked target.
    plate_read_interval_s: float = 4.0
    # Master switch for the proactive plate-read / vehicle-ID loop.
    plate_id_enabled: bool = True

    # ── Smart RTL / low-battery failsafe ─────────────────────────────────────
    # When an ARMED vehicle's battery drops to/below this %, the GCS notifies the
    # operator and STADO asks to confirm a return-to-launch (it does NOT auto-RTL
    # without confirmation). 0/disabled via smart_rtl_enabled.
    low_battery_pct: float = 10.0
    smart_rtl_enabled: bool = True

    # ── Max-altitude CEILING (metres AGL) ────────────────────────────────────
    # OPTIONAL startup default for the runtime fleet altitude ceiling: no command
    # may fly a drone above it without an explicit operator override. Empty/None
    # (the default) = NO ceiling at startup; the operator sets one at runtime by
    # voice (set_max_altitude) or REST (POST /api/safety/max_altitude). When set
    # here it seeds the runtime value at startup (see app/safety.init_default).
    # GCS-SIDE enforcement only — STRONGLY recommend also pushing it to PX4's
    # geofence (GF_MAX_VER_DIST/GF_ACTION); see reviews/max-altitude-ceiling.md.
    max_altitude_m: float | None = None

    # Server-side Static Maps key for satellite-imagery survey planning.
    static_maps_api_key: str = ""

    # ── Structured audit log ─────────────────────────────────────────────────
    # Captures EVERYTHING relevant flowing in/out of the GCS as timestamped JSON
    # lines (see app/logbus.py). Writes to `<backend>/<audit_log_dir>` and keeps
    # an in-memory ring buffer for the /api/logs query endpoints. High-rate
    # telemetry is sampled at `audit_log_telemetry_hz` per vehicle so the record
    # is complete without flooding.
    audit_log_enabled: bool = True
    audit_log_dir: str = "logs"
    audit_log_telemetry_hz: float = 1.0

    # CORS origins for the Vite dev server.
    cors_origins: list[str] = [
        "http://localhost:5180",
        "http://127.0.0.1:5180",
    ]


settings = Settings()


# ── fleet definition ─────────────────────────────────────────────────────────
# The set of vehicles the registry instantiates at startup. Overwatch is the
# primary/active vehicle and reuses the existing `mavlink_connection` so the
# single-vehicle flow is unchanged; Outrider is the secondary vehicle.
#
# CAPABILITY FLAGS (preflight H1/H2): the two vehicles do NOT share a command
# surface. Overwatch is a normal MAVLink autopilot; Outrider reaches PX4 over a
# DDS→MAVLink bridge (jetson/dds_mavlink_bridge.py) that relays COMMAND_LONG /
# COMMAND_INT only — it does NOT carry SET_POSITION_TARGET_LOCAL_NED (GCS-side
# OFFBOARD setpoints) nor the MISSION_* upload protocol. So:
#   - supports_offboard=False for Outrider: the GCS must REFUSE turn /
#     start_offboard / any GCS-driven OFFBOARD/follow rather than send a
#     DO_SET_MODE→OFFBOARD that would strand a flying Outrider with no setpoint
#     stream (PX4 drops OFFBOARD in ~0.5 s → failsafe).
#   - supports_missions=False for Outrider: survey/mission upload can't be
#     relayed over DDS, so the GCS refuses it up front instead of letting the
#     upload silently time out (and never arms Outrider into an empty mission).
# Overwatch is a full MAVLink vehicle → both True. The guards key off these
# flags (see api.py / voice.py), NOT a hardcoded vehicle id.
def fleet() -> list[dict]:
    return [
        {
            "id": "overwatch",
            "name": "Overwatch",
            "kind": "hexacopter",
            "connection": settings.mavlink_connection,
            "active": True,
            "supports_offboard": True,
            "supports_missions": True,
            "supports_autotune": True,
        },
        {
            "id": "outrider",
            "name": "Outrider",
            "kind": "quadcopter",
            "connection": settings.outrider_connection,
            "active": False,
            # DDS-bridge vehicle: still no GCS-side OFFBOARD setpoints (its
            # tracking/yaw is closed onboard by the VIO controller).
            "supports_offboard": False,
            # MISSIONS: Outrider IS now mission-capable. The Jetson bridge
            # (jetson/dds_mavlink_bridge.py) implements the MAVLink MISSION-upload
            # protocol server-side and EXECUTES the mission ONBOARD by sequencing
            # DO_REPOSITION over DDS (PX4 itself can't take a MISSION upload over
            # DDS). So Outrider participates in surveys/missions and the 2-drone
            # coordinated split. ⚠ This requires the updated bridge deployed to the
            # Jetson (see jetson/DEPLOY.md). The env override is kept so a deploy
            # WITHOUT the new bridge can be set back to mission-incapable:
            #   OUTRIDER_SUPPORTS_MISSIONS=0  → excluded from surveys again.
            "supports_missions": os.getenv("OUTRIDER_SUPPORTS_MISSIONS", "1") == "1",
            # AUTOTUNE: cmd 212 over the DDS bridge is rejected by PX4 Commander as
            # UNSUPPORTED (reviews/autotune-over-dds-verdict.md), so autotune is OFF
            # for Outrider by default — triggering it would false-start (drone left
            # armed/hovering, nothing tuned). Tune Outrider via the temporary
            # MAVLink-on-TELEM2 path (reviews/autotune-mavlink-mode.md); set this
            # True only while that path is active:  OUTRIDER_SUPPORTS_AUTOTUNE=1
            "supports_autotune": os.getenv("OUTRIDER_SUPPORTS_AUTOTUNE", "0") == "1",
        },
    ]


# survey_vision reads the maps key from os.environ; bridge it from .env/Settings.
if settings.static_maps_api_key:
    os.environ.setdefault("STATIC_MAPS_API_KEY", settings.static_maps_api_key)
