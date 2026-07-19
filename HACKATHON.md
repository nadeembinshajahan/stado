# Hackathon submission — Global AI Hackathon Series with Qwen Cloud

**Track:** EdgeAgent
**Project:** STADO×Qwen — voice-commanded drone fleet on Qwen Cloud
**Repo:** this one (public, MIT)
**Live demo:** https://stado.sfautonomy.com/ (Alibaba Cloud ECS, Singapore)

## What it is

A ground-control station where the primary interface to a two-drone fleet is
**conversation**. Hold push-to-talk and speak: Qwen3.5-Omni-Plus-Realtime
hears the raw audio, decides among **38 flight/mission tools**, the GCS
executes them over MAVLink against two PX4 SITL drones, and Qwen speaks the
outcome — including *proactive* reports when actions complete or a battery
runs low. Qwen3.7-Plus grounds vision requests ("track the red truck",
"describe what you see", satellite parcel detection for survey planning) and
Qwen3.7-Max writes the end-of-flight mission report. The GCS is a real,
field-flown system (hexa + quad over SIYI datalink and a Jetson DDS bridge);
the demo wraps the production code with a simulator so judges can fly it
from a browser.

## EdgeAgent fit

This is an agent whose *body* is at the edge and whose *language brain* is
in the cloud:

- **Edge**: on the real aircraft, a Jetson Orin runs VIO (GPS-denied state
  estimation) and onboard target tracking — control loops that must survive
  datalink loss and can't tolerate cloud RTT. The flight controller enforces
  its own failsafes below that.
- **Cloud (Qwen)**: open-vocabulary speech understanding, multi-drone task
  orchestration (e.g. one utterance → survey area planned, split into zones
  with separation corridors, staggered altitudes, per-drone missions
  uploaded), open-vocabulary target grounding that *seeds* the onboard
  tracker, and spoken telemetry.
- **The seam**: a deterministic dispatcher between them. Every model tool
  call passes safety interlocks — a human-armed **Ready-for-Flight gate**
  (voice takeoff is refused until the operator arms it), per-vehicle
  capability guards, and a max-altitude ceiling requiring an explicit spoken
  override. The agent proposes; the gates dispose.

## Built during the submission period (the base project predates it)

The GCS existed before May 26, 2026 — this submission is the **significant
update** on top of it, all after the period opened:

1. **The Qwen Cloud agent architecture** (`backend/app/voice_qwen.py` +
   `backend/app/qwen.py`): the realtime voice loop on Qwen Realtime's native
   WebSocket protocol — manual-VAD push-to-talk (`turn_detection: null` +
   commit), barge-in via `response.cancel`, the tool-call round-trip
   (`response.function_call_arguments.done` → `function_call_output` →
   `response.create`) — plus the vision/reasoning layer (target grounding,
   scene description, survey parcel detection, mission reports) on Model
   Studio's OpenAI-compatible endpoint. One tool schema, defined once,
   drives voice, REST, and the safety dispatcher.
2. **Alibaba Cloud deployment** (`alibaba/`, `Dockerfile`, `entrypoint.sh`):
   the public demo container — dual PX4 SITL + backend + nginx — provisioned
   and served from Alibaba Cloud ECS with one-command deploy and Caddy TLS.
3. **Ready-for-Flight gate** (built July 2, 2026; validated in a July 3
   field test): the human-in-the-loop interlock in `dispatch()` that voice
   commands cannot bypass.
4. **Field-test hardening from the July 3 flight** — including the
   TAKEOFF-mode-first arming sequence (`scripts/patch_commands_takeoff.py`),
   which fixes both a PX4 SITL edge case and a real-world barometer-drift
   failure mode observed in the field.
5. Fleet-scaled surveys, dual-HUD/dual-feed UI, and the ~384-test suite the
   system is validated against — all May 26+.

## Alibaba Cloud usage (proof)

- **Qwen Cloud / Model Studio**: the realtime client in
  [`backend/app/voice_qwen.py`](backend/app/voice_qwen.py) authenticates
  with `DASHSCOPE_API_KEY` and speaks Model Studio's realtime WebSocket
  protocol (`wss://…aliyuncs.com/api-ws/v1/realtime`,
  model `qwen3.5-omni-plus-realtime`). The chat-completions client in
  [`backend/app/qwen.py`](backend/app/qwen.py) drives `qwen3.7-plus`
  (vision) and `qwen3.7-max` (reasoning) on the OpenAI-compatible endpoint.
- **ECS**: the live demo container runs on an Alibaba Cloud ECS instance —
  provisioning + deploy scripts in [`alibaba/`](alibaba/).

## Why Qwen specifically

- **Native audio-to-audio with tools**: one realtime model does ASR +
  reasoning + tool selection + TTS in a single session — the entire voice
  stack is ~400 lines.
- **Manual turn detection** maps 1:1 onto push-to-talk, the correct UX for
  command-and-control (an open mic near a flying drone is a hazard).
- **One key, one cloud** for voice, vision, and reasoning: the same
  `DASHSCOPE_API_KEY` drives the realtime session and the chat-completions
  models, so the whole model surface is a single vendor dependency.
- **Semantic interruption** (in open-mic mode) and 100K TPM headroom cover
  long operator sessions.
- The audio contract (PCM16 16 kHz in / 24 kHz out) matched our browser
  pipeline exactly — the frontend needed **zero changes**.

## Judge quickstart

1. Open the demo URL. Wait for both drones to show **CONNECTED** (~60 s
   after a cold start).
2. Hold the mic button: *"What's the fleet status?"*
3. Arm the **Ready for Flight** pill (UI), then: *"Take off to 20 meters."*
4. *"Survey a 300 meter area at 40 meters."* — watch the area split across
   both drones on the map.
5. *"Everyone return to launch."*

Or run it locally: `docker build` + `docker run -e DASHSCOPE_API_KEY=…`
(see [README](README.md)).
