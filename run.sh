#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  STADO×Qwen — local dev runner. Starts backend + frontend, tears down on ^C.
#
#    backend (FastAPI, voice → Qwen Realtime) ─► http 8000
#    frontend (Vite dev server)               ─► http 5180
#
#  Drones: point MAVLINK_CONNECTION / OUTRIDER_CONNECTION at a PX4 SITL
#  (defaults match the Docker image: udpin 14540 / 14541). The full sim runs
#  in the container (`docker build … && docker run …`, see README) — this
#  script is for iterating on backend/frontend against an already-running SITL.
#
#  Requires backend/.env (copy .env.example) with DASHSCOPE_API_KEY set.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail
cd "$(dirname "$0")"

export MAVLINK_CONNECTION="${MAVLINK_CONNECTION:-udpin:0.0.0.0:14540}"
export OUTRIDER_CONNECTION="${OUTRIDER_CONNECTION:-udpin:0.0.0.0:14541}"

c_cyan=$'\e[36m'; c_dim=$'\e[2m'; c_grn=$'\e[32m'; c_yel=$'\e[33m'; c_off=$'\e[0m'
log() { printf "%s▸%s %s\n" "$c_cyan" "$c_off" "$*"; }

pids=()
cleanup() {
  trap - INT TERM EXIT
  printf "\n%s▸ shutting down…%s\n" "$c_yel" "$c_off"
  kill "${pids[@]}" 2>/dev/null
  pkill -P $$ 2>/dev/null
  pkill -f "uvicorn app.main:app" 2>/dev/null
  pkill -f "vite" 2>/dev/null
  wait 2>/dev/null
  printf "%s▸ all stopped.%s\n" "$c_grn" "$c_off"
}
trap cleanup INT TERM EXIT

# Clear stale instances so ports are free (idempotent restart).
pkill -f "uvicorn app.main:app" 2>/dev/null
pkill -f "vite" 2>/dev/null
sleep 0.4

echo
log "voice          : Qwen Realtime (needs DASHSCOPE_API_KEY in backend/.env)"
log "backend        : http://127.0.0.1:8000   (MAVLink ${MAVLINK_CONNECTION}, Outrider ${OUTRIDER_CONNECTION})"
( cd backend && exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 ) &
pids+=($!)

log "frontend       : http://localhost:5180"
( cd frontend && exec npm run dev ) &
pids+=($!)

printf "\n%s  STADO×Qwen up.  Open %shttp://localhost:5180%s\n" "$c_grn" "$c_cyan" "$c_off"
printf "%s  Ctrl-C to stop everything.%s\n\n" "$c_dim" "$c_off"

wait
