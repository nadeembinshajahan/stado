#!/usr/bin/env bash
# STADO×Qwen demo entrypoint (Alibaba Cloud ECS).
#
# Adapted from the GCP stado-demo entrypoint with the cloud-specific bits
# removed: no Secret Manager fetch (env vars come from `docker run`), no
# GCS log uploader, no admin panel, no auth layer.
#
# Starts, in order (nginx last, in the foreground, as the container's
# lifecycle process):
#   1. PX4 SITL instance 0 (Overwatch hexacopter, sysid 1 → 127.0.0.1:14540)
#   2. PX4 SITL instance 1 (Outrider quadcopter,  sysid 2 → 127.0.0.1:14541)
#   3. FastAPI backend (uvicorn app.main:app on 127.0.0.1:8000) —
#      voice runs on Qwen3.5-Omni Realtime
#   4. px4-relax-preflight (background, one-shot SITL param relax)
#   5. nginx on :8080 (foreground)
set -euo pipefail

# ── 1. Required config ───────────────────────────────────────────────────────
# The Qwen Realtime key is the ONLY hard requirement — the whole demo is the
# voice agent. Everything else has workable defaults.
: "${DASHSCOPE_API_KEY:?DASHSCOPE_API_KEY must be set (Qwen Realtime voice agent is the demo)}"
export QWEN_REALTIME_MODEL="${QWEN_REALTIME_MODEL:-qwen3.5-omni-plus-realtime}"
# International accounts: set QWEN_WS_URL to your workspace-scoped endpoint,
#   wss://<WorkspaceId>.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime
export QWEN_WS_URL="${QWEN_WS_URL:-wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime}"
export QWEN_VOICE="${QWEN_VOICE:-Ethan}"
echo "[entrypoint] voice: Qwen Realtime model=$QWEN_REALTIME_MODEL voice=$QWEN_VOICE"

HOST_IP="${HOST_IP:-127.0.0.1}"

# ── 2. Dual PX4 SITL ─────────────────────────────────────────────────────────
# Each PX4 instance gets its OWN Gazebo world (default.sdf / default2.sdf)
# and its own GZ_PARTITION, so each gets a dedicated gz-sim process. With a
# SHARED world, one gz server advances physics for both vehicles and falls
# behind real time → PX4's sensors_status_imu watchdog fires "Accel #0 fail:
# TIMEOUT!" → arming refused. (Hard-won on the GCP deploy; kept verbatim.)
WORLD_OVERWATCH="${WORLD_OVERWATCH:-default}"
WORLD_OUTRIDER="${WORLD_OUTRIDER:-default2}"

launch_px4() {
    local instance="$1" home_lat="$2" home_lon="$3" home_alt="$4" logname="$5" world="$6"

    (
        # Bake the spherical-coordinates origin into the per-instance world
        # BEFORE px4 boots Gazebo, so EKF latches the right home altitude
        # (setting it via service call post-boot loses a race with EKF init).
        WORLD_SDF="/root/px4/Tools/simulation/gz/worlds/${world}.sdf"
        if [ -w "$WORLD_SDF" ] && [ -n "$home_lat" ] && [ -n "$home_lon" ] && [ -n "$home_alt" ]; then
            sed -i \
                -e "s#<latitude_deg>[^<]*</latitude_deg>#<latitude_deg>${home_lat}</latitude_deg>#" \
                -e "s#<longitude_deg>[^<]*</longitude_deg>#<longitude_deg>${home_lon}</longitude_deg>#" \
                -e "s#<elevation>[^<]*</elevation>#<elevation>${home_alt}</elevation>#" \
                "$WORLD_SDF" 2>/dev/null || true
        fi

        # Xvfb + rtsp_proxy are global; only spawn once.
        if [ "$instance" = "0" ]; then
            Xvfb :99 -screen 0 1600x1200x24+32 &
            ${SITL_RTSP_PROXY:-/root/sitl_rtsp_proxy}/build/sitl_rtsp_proxy 2>/dev/null &
        fi

        # edit_rcS.bash <HOST_API> <HOST_QGC> points PX4's MAVLink targets at
        # loopback; API remote port = 14540 + instance.
        # shellcheck disable=SC1091
        source "${WORKSPACE_DIR}/edit_rcS.bash" "$HOST_IP" "$HOST_IP"

        export HEADLESS=1
        export PX4_SIM_MODEL="${PX4_SIM_MODEL:-gz_x500}"
        export PX4_GZ_WORLD="$world"
        export PX4_HOME_LAT="$home_lat"
        export PX4_HOME_LON="$home_lon"
        export PX4_HOME_ALT="$home_alt"
        # Unique partition per instance → isolated gz topic graphs → each PX4
        # spawns its own gz-sim server instead of joining the first one.
        export GZ_PARTITION="stado-qwen-i${instance}"
        export PX4_GZ_MODEL_POSE="0,0,0,0,0,0"

        echo "[entrypoint] starting PX4 SITL ${logname} (instance ${instance}, world ${world}) → ${HOST_IP}:$((14540 + instance))"
        # -d (daemon): no interactive pxh shell. Without it PX4 gets stdin EOF
        # every cycle and spews `pxh> ` in a tight loop (~3 GB/hour of log).
        exec "${FIRMWARE_DIR}/build/bin/px4" -d -i "$instance"
    ) > "/tmp/px4-${logname}.log" 2>&1 < /dev/null &

    PX4_PIDS+=($!)
    echo "[entrypoint] ${logname} pid=$!"
}

PX4_PIDS=()

launch_px4 0 "$PX4_HOME_LAT"      "$PX4_HOME_LON"      "$PX4_HOME_ALT"      overwatch "$WORLD_OVERWATCH"
# Stagger: give instance 0's gz server time to finish EKF init first.
sleep 8
launch_px4 1 "$OUTRIDER_HOME_LAT" "$OUTRIDER_HOME_LON" "$OUTRIDER_HOME_ALT" outrider "$WORLD_OUTRIDER"

# Surface the PX4 lines that matter (arm rejections, EKF, sensor timeouts)
# in `docker logs` without drowning it in heartbeat noise.
(
    tail -F /tmp/px4-overwatch.log 2>/dev/null \
      | grep --line-buffered -E "INFO|WARN|ERROR|FAIL|denied|Armed|Disarmed|TAKEOFF|TIMEOUT|EKF|preflight|sensor" \
      | sed -u 's/^/[px4-overwatch] /'
) &
(
    tail -F /tmp/px4-outrider.log 2>/dev/null \
      | grep --line-buffered -E "INFO|WARN|ERROR|FAIL|denied|Armed|Disarmed|TAKEOFF|TIMEOUT|EKF|preflight|sensor" \
      | sed -u 's/^/[px4-outrider]  /'
) &

sleep 4

# ── 3. Backend ───────────────────────────────────────────────────────────────
export MAVLINK_CONNECTION OUTRIDER_CONNECTION CAMERA_HFOV_DEG \
       DASHSCOPE_API_KEY QWEN_WS_URL QWEN_REALTIME_MODEL QWEN_VOICE \
       STATIC_MAPS_API_KEY GROUNDING_BACKEND

echo "[entrypoint] starting backend on 127.0.0.1:8000 (voice → Qwen Realtime)"
(
    cd /app/backend
    exec uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level info
) > /tmp/backend.log 2>&1 &
BACKEND_PID=$!
echo "[entrypoint] backend pid=$BACKEND_PID"
( tail -F /tmp/backend.log 2>/dev/null | sed -u 's/^/[backend] /' ) &

for i in $(seq 1 30); do
    if curl -fsS http://127.0.0.1:8000/health > /dev/null 2>&1; then
        echo "[entrypoint] backend healthy after ${i}s"
        break
    fi
    sleep 1
done

# ── 4. Relax PX4 SITL preflight gates (background one-shot) ──────────────────
# Waits for both PX4s to heartbeat, then pushes params that disable RC-loss /
# battery-sim / auto-disarm failsafes that would otherwise block arming in
# a headless sim. Idempotent; logs and continues on failure.
rm -f /tmp/relax-preflight.done
(
    cd /app/backend
    sleep 25
    uv run python3 /usr/local/bin/px4-relax-preflight.py || true
    touch /tmp/relax-preflight.done
    echo "[preflight-relax] done marker written"
) 2>&1 | sed -u 's/^/[preflight-relax] /' &
echo "[entrypoint] preflight-relax scheduled"

# ── 5. nginx (foreground) ────────────────────────────────────────────────────
trap 'echo "[entrypoint] SIGTERM — stopping children"; \
      kill "$BACKEND_PID" "${PX4_PIDS[@]}" 2>/dev/null || true; \
      nginx -s quit 2>/dev/null || true; \
      wait 2>/dev/null; \
      exit 0' TERM INT

echo "[entrypoint] starting nginx on :8080"
exec nginx -g 'daemon off;'
