# STADO×Qwen — voice-commanded drone fleet on Qwen Cloud

**Talk to a drone fleet.** STADO is the voice agent inside the STRATO·GCS
ground-control station: hold push-to-talk, say *"Overwatch, take off to 30
meters and survey a 200 meter area"*, and watch two PX4 drones split the
survey between them on a live map.

Built on **Qwen Cloud** for the Global AI Hackathon Series (EdgeAgent
track): realtime voice runs on **Qwen3.5-Omni-Plus-Realtime**, vision
grounding on **Qwen3.7-Plus**, mission-report reasoning on **Qwen3.7-Max**
(all Alibaba Cloud Model Studio), and the demo deploys on **Alibaba Cloud
ECS**, simulating two PX4 SITL drones (Overwatch hexacopter + Outrider
quadcopter) so anyone can fly the fleet from a browser — no hardware, no
login.

> The underlying GCS is a real, field-flown system (it flies actual
> hexa/quadcopters over SIYI datalinks and a Jetson DDS bridge). Everything
> here is the production code, wrapped with SITL glue for a public demo.

## What Qwen does here

**Voice (Qwen3.5-Omni-Plus-Realtime).** The browser streams raw mic PCM
(16 kHz) over a WebSocket to the GCS backend, which bridges it to a Qwen
Realtime session (native WebSocket API, `wss://…/api-ws/v1/realtime`). Qwen:

- **understands speech directly** (native audio in — no separate ASR),
- **calls tools**: the full 38-function drone command surface (takeoff, land,
  orbit, multi-drone survey planning, formation flight, target tracking,
  autotune, geofenced max-altitude overrides…),
- **speaks back** (native audio out, 24 kHz PCM streamed to the browser),
- honors **push-to-talk** via manual turn detection (`turn_detection: null`
  + buffer commit on PTT release) with barge-in (`response.cancel`).

**Vision (Qwen3.7-Plus).** Open-vocabulary target acquisition — "track the
red truck" grounds a bounding box on the live feed and seeds the CSRT/onboard
tracker — plus scene description for the `describe_view` voice tool, license
plate reads for vehicle ID, and satellite-tile parcel detection for the
survey planner. All via Model Studio's OpenAI-compatible endpoint.

**Reasoning (Qwen3.7-Max).** End-of-flight mission reports: the flight
recorder's stats, mode timeline, and agent-action timeline are distilled into
an operator-grade summary for the report/PDF.

Every tool call goes through the GCS's safety machinery: the Ready-for-Flight
gate (a human-armed software interlock — voice takeoff is refused until the
operator arms the pill in the UI), capability guards per vehicle, and a
max-altitude ceiling that requires an explicit spoken override. The model is
powerful; the dispatcher stays paranoid.

**Alibaba Cloud proof:** the Qwen Realtime client is
[`backend/app/voice_qwen.py`](backend/app/voice_qwen.py) and the
chat-completions client is [`backend/app/qwen.py`](backend/app/qwen.py) —
both authenticate against Alibaba Cloud Model Studio (`DASHSCOPE_API_KEY`).
The demo itself runs on an Alibaba Cloud ECS instance
([`alibaba/`](alibaba/)).

## Try it

Live demo: **https://stado.sfautonomy.com/** (Alibaba Cloud ECS, Singapore).
Open it, hold the mic button (or spacebar), and try:

- "STADO, what's the fleet status?"
- "Arm check on both drones."
- *(arm the Ready-for-Flight pill in the UI, then)* "Take off to 20 meters."
- "Survey a 300 meter area at 40 meters altitude." *(watch it split between
  both drones)*
- "Outrider, orbit the survey area." / "Everyone return to launch."

## Run it yourself

```bash
cp .env.example .env       # set DASHSCOPE_API_KEY (Model Studio) + VITE_GOOGLE_MAPS_API_KEY
docker build -t stado-qwen --build-arg VITE_GOOGLE_MAPS_API_KEY=<key> .
docker run -p 8080:8080 --env-file .env stado-qwen
# open http://localhost:8080 — wait ~60s for both SITL drones to link up
```

The container boots two PX4 SITL instances in separate Gazebo worlds
(dedicated physics per drone), the FastAPI backend, and nginx. `/api/ready`
turns 200 when both drones are MAVLink-connected.

Quick API-only sanity check (no Docker):

```bash
export DASHSCOPE_API_KEY=sk-...
python3 experiments/qwen_hello.py     # audio in → tool call → audio out
```

Local dev against a running SITL: `./run.sh` (backend :8000 + Vite :5180).

## Repo map

| Path | What |
|---|---|
| `backend/app/voice_qwen.py` | **The Qwen Realtime bridge** — session, PTT, tool calls, alerts |
| `backend/app/voice.py` | Voice agent core: 38 tool schemas, dispatcher, Ready-for-Flight gate |
| `backend/app/qwen.py` | Qwen chat-completions client (vision + reasoning models) |
| `backend/app/vision/grounding.py` | Open-vocabulary target grounding on Qwen vision |
| `backend/app/survey_vision.py` | Satellite parcel detection for the survey planner |
| `backend/app/mavlink/` | MAVLink links, registry, commands, missions |
| `backend/app/survey/` | Multi-drone survey planner (zone splitting, corridors) |
| `frontend/` | React GCS: map, HUD, PTT voice UI, survey previews |
| `experiments/qwen_hello.py` | Standalone Qwen Realtime viability probe |
| `alibaba/` | ECS provisioning guide + one-command deploy (Caddy TLS) |
| `Dockerfile`, `entrypoint.sh` | Dual-PX4-SITL demo container |

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full diagram — including how
the same agent layer drives real hardware in the field (Jetson edge compute
with onboard VIO + tracking, cloud model for intent), which is the
edge-agent split this demo showcases.

## Tests

```bash
cd backend && uv sync --extra test && uv run pytest   # 384 tests
```

## License

[MIT](LICENSE).
