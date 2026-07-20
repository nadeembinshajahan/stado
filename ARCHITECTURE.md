# Architecture

![STADO×Qwen system architecture](architecture.png)

> **For-print / Devpost version:** [`architecture.png`](architecture.png) ·
> [`architecture.svg`](architecture.svg) · self-contained
> [`architecture.html`](architecture.html) (open offline in any browser).

## The demo (this repo, deployed on Alibaba Cloud ECS)

One Docker container simulates the whole fleet; Qwen Cloud (Alibaba Cloud
Model Studio) is the only external service in the loop. The teal path below
is the whole story in one trace: **① PTT → ② WebSocket → ③ Qwen Realtime →
④ tool call → ⑤ safety gates → ⑥ MAVLink → PX4 → ⑦ spoken outcome**.

```mermaid
flowchart LR
    subgraph BROWSER["🖥️ Reviewer's browser — React GCS"]
        MIC(["🎙️ ① push-to-talk<br/>mic PCM16 @16 kHz"])
        UI["live map · dual HUDs · survey previews<br/>Ready-for-Flight pill — human-armed"]
        SPK(["🔊 ⑦ spoken outcome<br/>reply PCM16 @24 kHz"])
    end

    subgraph ALI["☁️ Alibaba Cloud — ap-southeast-1 · Singapore"]
        subgraph ECS["ECS — one demo container · Caddy sidecar TLS + nginx :8080"]
            BE["② /ws/voice bridge<br/>voice_qwen.py"]
            DISP["④ dispatch — one tool surface<br/>38 tools shared by voice · REST · field"]
            GATES{{"⑤ SAFETY INTERLOCKS<br/>Ready-for-Flight · max-alt ceiling · home-alt gate"}}
            MAV["MAVLink registry<br/>2 vehicles · sysid routing"]
            P1["PX4 SITL — Overwatch hexa<br/>own Gazebo world"]
            P2["PX4 SITL — Outrider quad<br/>own Gazebo world"]
        end
        subgraph MS["Model Studio — Qwen"]
            Q["③ qwen3.5-omni-plus-realtime — VOICE<br/>native audio in/out · emits tool calls"]
            QV["qwen3.7-plus — VISION<br/>target grounding · describe_view · satellite parcels"]
            QR["qwen3.7-max — REASONING<br/>post-flight mission reports"]
        end
    end

    MIC -->|"WebSocket /ws/voice · binary PCM"| BE
    BE <-->|"wss Realtime API — audio up · tool_call + reply audio down"| Q
    BE -->|"audio deltas"| SPK
    BE -->|"function_call_arguments.done"| DISP
    DISP -->|"every command"| GATES
    GATES -->|"⑥ pass"| MAV
    MAV -->|"MAVLink UDP :14540"| P1
    MAV -->|"MAVLink UDP :14541"| P2
    DISP -.->|"frames"| QV
    DISP -.->|"flight log"| QR
    MAV -.->|"telemetry → WS hub"| UI

    classDef browserBox fill:#0d1a24,stroke:#22e3c4,color:#e6edf3
    classDef cloudBox fill:#10161f,stroke:#4a5d78,color:#e6edf3
    classDef innerBox fill:#131b28,stroke:#33465f,color:#e6edf3
    classDef node fill:#15202f,stroke:#3d5271,color:#e6edf3
    classDef model fill:#112a2b,stroke:#22e3c4,color:#e6edf3
    classDef gate fill:#201a0e,stroke:#ffb020,color:#ffd680
    classDef sim fill:#181408,stroke:#ffb020,stroke-dasharray:6 4,color:#e6edf3
    class BROWSER browserBox
    class ALI cloudBox
    class ECS,MS innerBox
    class MIC,UI,SPK,BE,DISP,MAV node
    class Q,QV,QR model
    class GATES gate
    class P1,P2 sim
    linkStyle 0,1,2,3,4,5,6,7 stroke:#22e3c4,stroke-width:2.5px
    linkStyle 8,9,10 stroke:#64748b,stroke-width:1.5px
```

The two SITL nodes are drawn dashed for a reason: that is the **sim ↔ real
boundary**. In the field the same `MAVLink registry` speaks to a Pixhawk
over a SIYI datalink and to a Jetson bridge over WiFi (diagram below) —
nothing left of that boundary changes.

### The voice round-trip

```mermaid
sequenceDiagram
    autonumber
    participant OP as Operator (PTT)
    participant BR as Browser
    participant GW as /ws/voice bridge
    participant Q as Qwen Realtime (omni)
    participant D as dispatch() + gates
    participant PX as PX4 (MAVLink)
    OP->>BR: hold PTT · speak
    BR->>GW: binary PCM16 @16 kHz
    GW->>Q: input_audio_buffer.append …
    OP->>BR: release PTT
    GW->>Q: commit + response.create
    Q->>GW: function_call_arguments.done
    GW->>D: tool call (1 of 38)
    D->>D: Ready-for-Flight · max-alt · home-alt
    D->>PX: MAVLink command
    PX-->>GW: ack + telemetry
    GW->>Q: function_call_output + response.create
    Q-->>BR: response.audio.delta @24 kHz → speaker
```

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

### The safety interlocks (why the amber box exists)

Every model-initiated action passes deterministic gates *before* a single
MAVLink byte leaves the dispatcher — the model is powerful, the dispatcher
stays paranoid:

- **Ready-for-Flight** — a per-vehicle, human-armed software interlock.
  Voice cannot launch a drone until the operator arms the pill in the UI.
- **Max-altitude ceiling** — one fleet-wide ceiling enforced on every
  altitude-bearing command. It **refuses, never silently clamps**; raising
  it requires an explicit spoken override, which is audit-logged
  ([`backend/app/safety.py`](backend/app/safety.py)).
- **Home-alt gate** — takeoff is blocked when PX4's home/EKF altitude
  reference is visibly wrong (a real field incident: a ~2.3 m home offset
  produced a "stuck armed, no climb" hexacopter — the gate now catches it
  on the ground).

## The real system (what the demo is a simulation of)

The same GCS + agent layer flies physical drones. This is the edge↔cloud
split the EdgeAgent track is about: reflexes live at the edge, language
and orchestration live in the cloud.

```mermaid
flowchart TB
    subgraph Cloud["☁️ Qwen Cloud"]
        QW["qwen3.5-omni-plus-realtime<br/>intent · tool calls · speech"]
        QV2["qwen3.7-plus / qwen3.7-max<br/>vision grounding · reports"]
    end

    subgraph GCSs["🎧 Ground control — laptop at the flight line"]
        GCS["STRATO·GCS — FastAPI + React<br/>dispatch() + safety interlocks"]
    end

    subgraph OW["🛸 Overwatch — hexacopter"]
        FC1["PX4 flight controller"]
        CAM["SIYI gimbal camera<br/>RTSP video"]
    end

    subgraph OR["🛸 Outrider — quadcopter · Jetson Orin edge compute"]
        J["Jetson Orin"]
        VIO["VIO — OAK-D<br/>GPS-denied state estimation"]
        TRK["Onboard tracker<br/>follows targets without the GCS"]
        DDS["uXRCE-DDS ↔ MAVLink bridge"]
        FC2["Pixhawk — PX4"]
        J --- VIO
        J --- TRK
        J --- DDS --- FC2
    end

    OP(["🎙️ Operator voice — PTT"]) --> GCS
    GCS <-->|"wss Realtime API"| QW
    GCS <-->|"chat completions"| QV2
    GCS -->|"MAVLink — SIYI datalink"| FC1
    GCS -->|"MAVLink over WiFi · UDP"| DDS
    CAM -.->|"RTSP → go2rtc → WebRTC"| GCS

    classDef cloudBox fill:#10161f,stroke:#4a5d78,color:#e6edf3
    classDef groundBox fill:#0d1a24,stroke:#22e3c4,color:#e6edf3
    classDef edgeBox fill:#131b28,stroke:#33465f,color:#e6edf3
    classDef node fill:#15202f,stroke:#3d5271,color:#e6edf3
    classDef model fill:#112a2b,stroke:#22e3c4,color:#e6edf3
    class Cloud cloudBox
    class GCSs groundBox
    class OW,OR edgeBox
    class GCS,FC1,CAM,J,VIO,TRK,DDS,FC2,OP node
    class QW,QV2 model
    linkStyle 4,5,6,7,8 stroke:#22e3c4,stroke-width:2.5px
    linkStyle 9 stroke:#64748b,stroke-width:1.5px
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
