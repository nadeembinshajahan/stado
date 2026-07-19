# Architecture

## The demo (this repo, deployed on Alibaba Cloud ECS)

One Docker container simulates the whole fleet; Qwen Cloud (Alibaba Cloud
Model Studio) is the only external service in the loop.

```mermaid
flowchart LR
    subgraph Browser["Reviewer's browser"]
        MIC["Push-to-talk mic<br/>PCM16 @16 kHz"]
        UI["React GCS<br/>map · HUD · survey previews"]
        SPK["Speaker<br/>PCM16 @24 kHz"]
    end

    subgraph ECS["Alibaba Cloud ECS (Singapore) — one container"]
        NGINX["nginx :8080<br/>SPA + /api + /ws proxy"]
        BE["FastAPI backend<br/>/ws/voice bridge"]
        VQ["voice_qwen.py<br/>Qwen Realtime client"]
        QC["qwen.py<br/>chat-completions client"]
        DISP["dispatch()<br/>Ready-for-Flight gate<br/>capability guards<br/>max-alt ceiling"]
        MAV["MAVLink links<br/>(registry: 2 vehicles)"]
        P1["PX4 SITL #1<br/>Overwatch hexa<br/>Gazebo world A"]
        P2["PX4 SITL #2<br/>Outrider quad<br/>Gazebo world B"]
    end

    subgraph QWEN["Alibaba Cloud Model Studio"]
        Q["qwen3.5-omni-plus-realtime<br/>voice: audio in · audio out<br/>38 registered tools"]
        QV["qwen3.7-plus<br/>vision: target grounding<br/>scene description · survey parcels"]
        QR["qwen3.7-max<br/>reasoning: mission-report summaries"]
    end

    MIC -- "WebSocket (binary PCM)" --> NGINX --> BE
    BE <--> VQ
    VQ <-- "wss://…/api-ws/v1/realtime<br/>session.update · audio buffers<br/>function_call_arguments.done" --> Q
    QC <-- "OpenAI-compatible<br/>chat completions" --> QV
    QC <--> QR
    VQ -- "tool call" --> DISP --> MAV
    DISP -- "describe_view · track_target<br/>survey perimeters · reports" --> QC
    MAV -- "MAVLink UDP" --> P1
    MAV -- "MAVLink UDP" --> P2
    MAV -- "telemetry" --> BE -- "WS hub" --> UI
    VQ -- "reply audio" --> BE --> SPK
```

### The voice round-trip

1. **PTT press** → browser starts streaming raw mic PCM frames over
   `/ws/voice`; the bridge forwards each as `input_audio_buffer.append`
   (manual turn detection — `turn_detection: null`).
2. **PTT release** → `input_audio_buffer.commit` + `response.create`.
3. Qwen transcribes + reasons **natively on audio** (no ASR stage) and either
   answers or emits a tool call (`response.function_call_arguments.done`).
4. The bridge runs the tool through `dispatch()` — the same dispatcher the
   REST API and the field system use, including the **Ready-for-Flight
   interlock** (voice can't launch a drone until a human arms the gate in
   the UI) — then returns `function_call_output` + `response.create`.
5. Qwen speaks the outcome; `response.audio.delta` chunks stream back to the
   browser as binary PCM. Transcripts flow alongside (`heard`/`said` events)
   for the on-screen conversation log.
6. Backend-initiated events (takeoff completed, low battery) are injected
   into the session as `[SYSTEM]` turns, so STADO *proactively speaks*
   ("Overwatch has reached 30 meters").

### One tool surface, three models

- The 38 tool declarations are defined **once**, in `voice.py`, as plain
  OpenAI-style JSON-Schema function dicts — the realtime session registers
  them in `session.update`, and the REST surface dispatches through the same
  schema. Zero drift between what the model can call and what the GCS can do.
- Vision-backed tools (`describe_view`, `track_target`, survey perimeter
  detection, plate reads) call **qwen3.7-plus** through
  [`backend/app/qwen.py`](backend/app/qwen.py), the shared chat-completions
  client on Model Studio's OpenAI-compatible endpoint.
- Mission-report summaries call **qwen3.7-max** on the same endpoint. Every
  non-realtime call degrades gracefully — no key or a network blip falls
  back to deterministic behavior, never a crash.

Qwen Realtime's audio contract (PCM16 16 kHz up / 24 kHz down) is exactly
what the browser pipeline streams — the frontend does no transcoding.

## The real system (what the demo is a simulation of)

The same GCS + agent layer flies physical drones. This is the edge↔cloud
split the EdgeAgent track is about: reflexes live at the edge, language
and orchestration live in the cloud.

```mermaid
flowchart TB
    subgraph Cloud["Qwen Cloud"]
        QW["Qwen3.5-Omni Realtime<br/>intent · tool calls · speech"]
        QV2["Qwen3.7-Plus / 3.7-Max<br/>vision grounding · reports"]
    end

    subgraph GCSs["Ground control (laptop, field)"]
        GCS["STRATO·GCS<br/>FastAPI + React<br/>dispatch() + safety gates"]
    end

    subgraph OW["Overwatch (hexacopter)"]
        FC1["PX4 flight controller"]
        CAM["SIYI gimbal camera<br/>RTSP video"]
    end

    subgraph OR["Outrider (quadcopter) — edge compute"]
        J["Jetson Orin"]
        VIO["VIO (OAK-D)<br/>GPS-denied state est."]
        TRK["Onboard tracker<br/>follows targets w/o GCS"]
        DDS["uXRCE-DDS ↔ MAVLink bridge"]
        FC2["Pixhawk (PX4)"]
        J --- VIO
        J --- TRK
        J --- DDS --- FC2
    end

    OP["Operator voice (PTT)"] --> GCS
    GCS <--> QW
    GCS <--> QV2
    GCS -- "MAVLink / SIYI datalink" --> FC1
    GCS -- "MAVLink over WiFi" --> DDS
    CAM -- "RTSP → go2rtc → WebRTC" --> GCS
```

- **Edge**: the Jetson runs visual-inertial odometry and target tracking
  onboard — the drone keeps flying and tracking through GCS/datalink
  dropouts. Tracking/yaw commands never round-trip through the cloud.
- **Cloud**: Qwen handles what edge silicon can't — open-vocabulary speech
  understanding, multi-step tasking ("survey the area and split it between
  both drones"), open-vocabulary target grounding to *seed* the onboard
  tracker, and spoken situation reports.
- **Ground**: the GCS is the trust boundary. Every model-initiated action
  passes deterministic safety gates *before* a single MAVLink byte goes out.

## Demo container internals

Two PX4 SITL instances would starve each other sharing one Gazebo physics
server (sensor watchdog → arming refused — learned on a shared-scheduler
cloud platform). So each PX4 gets its **own Gazebo world** in its own
`GZ_PARTITION`, i.e. a dedicated gz-sim process per drone:

```
entrypoint.sh
 ├─ PX4 SITL i0 (Overwatch, sysid 1) ── gz world "default"   → MAVLink :14540
 ├─ (8s stagger)
 ├─ PX4 SITL i1 (Outrider,  sysid 2) ── gz world "default2"  → MAVLink :14541
 ├─ uvicorn app.main:app (127.0.0.1:8000)
 ├─ px4-relax-preflight.py (one-shot: disable RC-loss/battery-sim failsafes)
 └─ nginx :8080 (foreground)
```

SITL-only patches (all idempotent, all in `scripts/`): TAKEOFF-mode-first
arming (`patch_commands_takeoff.py` — also field-proven under baro drift),
preflight relaxation, camera panels hidden, map centered on the SITL spawn.
