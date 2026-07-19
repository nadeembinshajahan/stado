#!/usr/bin/env bash
# Download (first run) and launch go2rtc with the drone RTSP from backend/.env.
set -euo pipefail
cd "$(dirname "$0")"

# Read RTSP_URL from backend/.env (extract, don't source — values may have spaces).
if [[ -z "${RTSP_URL:-}" && -f ../backend/.env ]]; then
  RTSP_URL="$(grep -E '^RTSP_URL=' ../backend/.env | head -1 | cut -d= -f2- | tr -d '"')"
  export RTSP_URL
fi

if [[ -z "${RTSP_URL:-}" ]]; then
  echo "RTSP_URL is not set. Add it to backend/.env (copy from .env.example)." >&2
  exit 1
fi

BIN="./go2rtc"
if ! command -v go2rtc >/dev/null 2>&1 && [[ ! -x "$BIN" ]]; then
  echo "Downloading go2rtc…"
  case "$(uname -s)" in
    Darwin) OS=mac ;;          # go2rtc names macOS assets "mac", not "darwin"
    Linux)  OS=linux ;;
    *) echo "unsupported OS $(uname -s)" >&2; exit 1 ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) ARCH=arm64 ;;
    x86_64|amd64)  ARCH=amd64 ;;
    *) echo "unsupported arch $(uname -m)" >&2; exit 1 ;;
  esac
  ASSET="go2rtc_${OS}_${ARCH}"
  BASE="https://github.com/AlexxIT/go2rtc/releases/latest/download"
  if [[ "$OS" == "mac" || "$OS" == "win" ]]; then
    curl -fL "$BASE/${ASSET}.zip" -o /tmp/go2rtc.zip
    unzip -o /tmp/go2rtc.zip -d . >/dev/null
    [[ -f "$ASSET" ]] && mv "$ASSET" "$BIN"
    rm -f /tmp/go2rtc.zip
  else
    curl -fL "$BASE/${ASSET}" -o "$BIN"
  fi
  chmod +x "$BIN"
fi

GO2RTC=$(command -v go2rtc || echo "$BIN")
echo "Starting go2rtc on http://127.0.0.1:1984  (stream: drone <- $RTSP_URL)"
exec "$GO2RTC" -config go2rtc.yaml
