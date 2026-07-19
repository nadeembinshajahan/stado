#!/usr/bin/env bash
# Diagnose the SIYI RTSP feed and find an ffmpeg transcode that yields frames.
# Run this when the CAMERA IS ON. Tests are SEQUENTIAL on purpose — multiple
# concurrent pulls (QGC + go2rtc + this) starve the camera and corrupt HEVC,
# which looks like a decode bug but is really contention.
#
#   ./relay/test-rtsp.sh                 # uses RTSP_URL from backend/.env
#   ./relay/test-rtsp.sh rtsp://host/...  # explicit URL
#
# TIP: close QGC's video and stop go2rtc (pkill -f 'go2rtc -config') first.
set -uo pipefail
cd "$(dirname "$0")"

URL="${1:-}"
if [[ -z "$URL" && -f ../backend/.env ]]; then
  URL="$(grep -E '^RTSP_URL=' ../backend/.env | head -1 | cut -d= -f2- | tr -d '"')"
fi
[[ -z "$URL" ]] && { echo "no RTSP_URL (pass as arg or set in backend/.env)"; exit 1; }

DUR=6
tmp() { mktemp /tmp/rtsptest.XXXX.mp4; }
# mac has no `timeout`; emulate with perl alarm.
run() { perl -e 'alarm shift; exec @ARGV' "$@"; }
# Read the frame count from ffmpeg's own progress line — robust even if the MP4
# trailer wasn't written (ffprobe -count_frames returns 0 on an unfinalized file,
# which falsely reads as "no frames" while data is clearly flowing).
frames_from_log() { grep -oE 'frame= *[0-9]+' "$1" | tail -1 | grep -oE '[0-9]+'; }

echo "RTSP: $URL"
echo "=== probe ==="
run 15 ffprobe -v error -rtsp_transport tcp \
  -show_entries stream=codec_name,profile,width,height,avg_frame_rate \
  -of default=nw=1 "$URL" 2>&1 | sed 's/^/  /'

declare -a NAME CMD
add() { NAME+=("$1"); CMD+=("$2"); }

add "A: TCP · sw-decode · h264_videotoolbox (config primary)" \
   "ffmpeg -hide_banner -nostdin -stats -loglevel error -fflags +genpts+discardcorrupt -rtsp_transport tcp -i $URL -an -c:v h264_videotoolbox -realtime 1 -g 30 -bf 0 -pix_fmt yuv420p -t $DUR -y OUT"
add "B: TCP · sw-decode · libx264 ultrafast (config fallback)" \
   "ffmpeg -hide_banner -nostdin -stats -loglevel error -fflags +genpts+discardcorrupt -rtsp_transport tcp -i $URL -an -c:v libx264 -preset ultrafast -tune zerolatency -g 30 -bf 0 -pix_fmt yuv420p -t $DUR -y OUT"
add "C: UDP · sw-decode · h264_videotoolbox" \
   "ffmpeg -hide_banner -nostdin -stats -loglevel error -fflags +genpts+discardcorrupt -rtsp_transport udp -i $URL -an -c:v h264_videotoolbox -realtime 1 -g 30 -bf 0 -t $DUR -y OUT"
add "D: TCP · stream-copy (no decode — does clean data arrive?)" \
   "ffmpeg -hide_banner -nostdin -stats -loglevel error -rtsp_transport tcp -i $URL -an -c copy -t $DUR -y OUT"

echo ""; echo "=== transcode trials (${DUR}s each, sequential) ==="
WIN=""
for i in "${!NAME[@]}"; do
  out=$(tmp); errlog="${out}.log"
  cmd="${CMD[$i]/OUT/$out}"
  # -stats prints the frame= progress line to stderr even at -loglevel error.
  run $((DUR + 8)) $cmd >"$errlog" 2>&1
  n=$(frames_from_log "$errlog"); n=${n:-0}
  sz=$(stat -f%z "$out" 2>/dev/null || echo 0)
  if [[ "$n" =~ ^[0-9]+$ && "$n" -gt 0 ]]; then
    echo "  ✅ ${NAME[$i]} → $n frames, ${sz}B"
    [[ -z "$WIN" ]] && WIN="${NAME[$i]}"
  else
    echo "  ❌ ${NAME[$i]} → no frames (${sz}B)"
  fi
  rm -f "$out" "$errlog"
done

echo ""
if [[ -n "$WIN" ]]; then
  echo "WINNER → $WIN"
  echo "If A passed, the current go2rtc.yaml is correct. If only B passed, switch"
  echo "go2rtc.yaml to the libx264 fallback line. Then: pkill -f 'go2rtc -config' && ./relay/run.sh"
else
  echo "Nothing produced frames. Likely the camera was being pulled by QGC/go2rtc at"
  echo "the same time (contention). Close those and re-run. If D (copy) also fails,"
  echo "the Mac isn't reaching the camera (check it's on the 192.168.144.x subnet)."
fi
