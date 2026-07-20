# syntax=docker/dockerfile:1.6
#
# STADO×Qwen demo image — multi-stage:
#   Stage A: build the React/Vite frontend (demo patches are pre-applied
#            in-repo by scripts/patch_frontend_demo.py + patch_onboarding_banner.py).
#   Stage B: PX4 SITL base + Python backend (voice → Qwen Realtime) + nginx.
#
# Adapted from the GCP stado-demo Dockerfile with all GCP-specific pieces
# removed (Secret Manager, GCS log uploader, admin panel, cookie auth) and
# the voice provider swapped to Qwen3.5-Omni Realtime on Alibaba Cloud
# Model Studio (see backend/app/voice_qwen.py).
#
# Build:  docker build -t stado-qwen --build-arg VITE_GOOGLE_MAPS_API_KEY=... .
# Run:    docker run -p 8080:8080 -e DASHSCOPE_API_KEY=sk-... stado-qwen

# ─── Stage A: frontend build ─────────────────────────────────────────────────
FROM node:20-bookworm-slim AS frontend
WORKDIR /src

# Baked into the JS bundle (visible to users) — restrict the Maps key to the
# demo origin in the provider console.
ARG VITE_GOOGLE_MAPS_API_KEY=""
ARG VITE_GOOGLE_MAPS_MAP_ID=""
ARG VITE_GMAPS_VERSION="weekly"

ENV VITE_GOOGLE_MAPS_API_KEY=$VITE_GOOGLE_MAPS_API_KEY \
    VITE_GOOGLE_MAPS_MAP_ID=$VITE_GOOGLE_MAPS_MAP_ID \
    VITE_GMAPS_VERSION=$VITE_GMAPS_VERSION

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build && ls -la dist/

# ─── Stage B: runtime image ──────────────────────────────────────────────────
# Prebuilt PX4 SITL base: px4 binary, gz simulator, Xvfb, rtsp proxy.
FROM jonasvautherin/px4-gazebo-headless:latest AS runtime

USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip \
        nginx ca-certificates curl tini procps \
    && rm -rf /var/lib/apt/lists/*

# uv — same Python project manager the dev workflow uses.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh \
    && uv --version

# ── Backend (FastAPI + Qwen Realtime voice + MAVLink) ────────────────────────
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
# voice extra: websockets (Qwen Realtime client) + openai (Qwen chat
#   completions via the OpenAI-compatible Model Studio endpoint).
# vision extra: the app imports cv2 at startup even with no camera.
RUN uv sync --extra voice --extra vision --frozen || uv sync --extra voice --extra vision

COPY backend/ ./

# The SITL takeoff patch is already applied in-repo
# (scripts/patch_commands_takeoff.py — idempotent). Re-run it so a fresh
# re-sync from the field GCS tree also builds.
COPY scripts/patch_commands_takeoff.py /tmp/
RUN python3 /tmp/patch_commands_takeoff.py

# One-shot SITL preflight relaxer (run in background by entrypoint.sh).
COPY scripts/px4-relax-preflight.py /usr/local/bin/px4-relax-preflight.py
RUN chmod +x /usr/local/bin/px4-relax-preflight.py

# ── Second Gazebo world for the second PX4 instance ──────────────────────────
# Each PX4 gets its own world (→ its own gz-sim server); the in-file world
# name must match PX4_GZ_WORLD or PX4 waits on gz topics forever.
RUN cp /root/px4/Tools/simulation/gz/worlds/default.sdf \
        /root/px4/Tools/simulation/gz/worlds/default2.sdf \
    && sed -i 's#<world name="default">#<world name="default2">#' \
        /root/px4/Tools/simulation/gz/worlds/default2.sdf \
    && grep '<world name=' /root/px4/Tools/simulation/gz/worlds/default2.sdf

# ── Frontend static assets (from Stage A) ────────────────────────────────────
COPY --from=frontend /src/dist /usr/share/nginx/html

# ── nginx ────────────────────────────────────────────────────────────────────
COPY nginx/nginx.conf /etc/nginx/nginx.conf
RUN rm -f /etc/nginx/sites-enabled/* /etc/nginx/conf.d/default.conf || true

# ── Entrypoint ───────────────────────────────────────────────────────────────
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Runtime env (override at `docker run`). NO SECRETS HERE — DASHSCOPE_API_KEY
# must come from the environment at run time.
ENV PORT=8080 \
    PX4_HOME_LAT=25.35338 \
    PX4_HOME_LON=55.38043 \
    PX4_HOME_ALT=5 \
    OUTRIDER_HOME_LAT=25.353425 \
    OUTRIDER_HOME_LON=55.380434 \
    OUTRIDER_HOME_ALT=5 \
    PX4_SIM_MODEL=gz_x500 \
    MAVLINK_CONNECTION=udpin:0.0.0.0:14540 \
    OUTRIDER_CONNECTION=udpin:0.0.0.0:14541 \
    QWEN_REALTIME_MODEL=qwen3.5-omni-plus-realtime \
    QWEN_VOICE=Ethan \
    CAMERA_HFOV_DEG=69

EXPOSE 8080
# tini → reap zombie SITL processes cleanly on SIGTERM.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
