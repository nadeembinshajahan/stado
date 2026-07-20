#!/usr/bin/env bash
# STADO×Qwen — build + (re)deploy on the Alibaba Cloud ECS box itself.
#
#   ./alibaba/deploy.sh                     # HTTP on :80
#   DOMAIN=demo.example.com ./alibaba/deploy.sh   # HTTPS via Caddy sidecar
#
# Reads secrets from ./.env (git-ignored; created from .env.example).
# Pattern: build → stop → rm → run → wait for /api/ready.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="${ENV_FILE:-.env}"
[ -f "$ENV_FILE" ] || { echo "!! $ENV_FILE missing — cp .env.example .env and fill it in"; exit 1; }

# Pull single values out of .env without sourcing it (values may contain '=').
envval() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' || true; }

DASHSCOPE_API_KEY="$(envval DASHSCOPE_API_KEY)"
[ -n "$DASHSCOPE_API_KEY" ] || { echo "!! DASHSCOPE_API_KEY not set in $ENV_FILE"; exit 1; }

IMAGE="${IMAGE:-stado-qwen:latest}"
NAME="${NAME:-stado-qwen}"
NET="${NET:-stado-net}"

echo "── build ────────────────────────────────────────────────────────────────"
docker build -t "$IMAGE" \
    --build-arg VITE_GOOGLE_MAPS_API_KEY="$(envval VITE_GOOGLE_MAPS_API_KEY)" \
    --build-arg VITE_GOOGLE_MAPS_MAP_ID="$(envval VITE_GOOGLE_MAPS_MAP_ID)" \
    .

echo "── replace container ────────────────────────────────────────────────────"
docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET"
docker stop "$NAME" 2>/dev/null || true
docker rm   "$NAME" 2>/dev/null || true

# Publish :8080 on loopback only when Caddy fronts it; directly on :80 otherwise.
if [ -n "${DOMAIN:-}" ]; then
    PUBLISH=(-p 127.0.0.1:8080:8080)
else
    PUBLISH=(-p 80:8080)
fi

docker run -d --name "$NAME" \
    --network "$NET" \
    --restart unless-stopped \
    "${PUBLISH[@]}" \
    --env-file "$ENV_FILE" \
    "$IMAGE"

if [ -n "${DOMAIN:-}" ]; then
    echo "── caddy (TLS for $DOMAIN) ──────────────────────────────────────────"
    mkdir -p /var/stado/caddy

    # Optional public-demo basic auth. When DEMO_USER and DEMO_PASSWORD are set
    # (typically in .env), Caddy 401s any unauthenticated request — protects
    # the running demo from bot scans + accidental Qwen-credit burn. Judges
    # get the credentials via the Devpost "Testing Instructions" field.
    DEMO_USER="${DEMO_USER:-$(envval DEMO_USER)}"
    DEMO_PASSWORD="${DEMO_PASSWORD:-$(envval DEMO_PASSWORD)}"
    AUTH_BLOCK=""
    if [ -n "$DEMO_USER" ] && [ -n "$DEMO_PASSWORD" ]; then
        # Bootstrap: start Caddy without auth first so we can call its
        # hash-password binary to bcrypt the password. Then rewrite + reload.
        docker rm -f caddy 2>/dev/null || true
        docker run -d --name caddy --network "$NET" --restart unless-stopped \
            -p 80:80 -p 443:443 \
            -v /var/stado/caddy/Caddyfile:/etc/caddy/Caddyfile:ro \
            -v caddy_data:/data caddy:2-alpine >/dev/null
        # Wait for caddy container to be responsive to `exec`.
        for _ in $(seq 1 10); do
            docker exec caddy caddy version >/dev/null 2>&1 && break
            sleep 1
        done
        HASH=$(docker exec caddy caddy hash-password --plaintext "$DEMO_PASSWORD" 2>/dev/null)
        if [ -n "$HASH" ]; then
            AUTH_BLOCK=$'    basic_auth {\n        '"$DEMO_USER $HASH"$'\n    }\n'
            echo "── basic auth enabled (user: $DEMO_USER) ──"
        else
            echo "!! Warning: could not hash DEMO_PASSWORD — deploying WITHOUT auth"
        fi
    fi

    cat > /var/stado/caddy/Caddyfile <<EOF
$DOMAIN {
$AUTH_BLOCK    reverse_proxy $NAME:8080
}
EOF
    docker stop caddy 2>/dev/null || true
    docker rm   caddy 2>/dev/null || true
    docker run -d --name caddy \
        --network "$NET" \
        --restart unless-stopped \
        -p 80:80 -p 443:443 \
        -v /var/stado/caddy/Caddyfile:/etc/caddy/Caddyfile:ro \
        -v caddy_data:/data \
        caddy:2-alpine
fi

echo "── waiting for SITL + backend (can take ~60 s on first boot) ───────────"
for i in $(seq 1 120); do
    if docker exec "$NAME" curl -fsS http://127.0.0.1:8080/api/ready >/dev/null 2>&1; then
        echo "ready after ${i}s"
        break
    fi
    sleep 1
done

echo
echo "deployed: ${DOMAIN:+https://$DOMAIN}${DOMAIN:-http://$(curl -fsS ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')}"
echo "logs:     docker logs -f $NAME"
