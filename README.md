# MPX Backend — Chat Ingress & Marketplace Gateway

Encrypted WebSocket bridge between MPX robot hardware (ESP32) and a cognitive agent (OpenClaw), plus a WASM skill marketplace with robot deployment.

## Architecture (v2.0 — Streaming + Sessions + Lua)

```
┌──────────────┐   AES-256-GCM    ┌─────────────────┐   HTTP SSE      ┌───────────┐
│  PWA / App   │                  │                 │  (streaming)    │           │
│  (Browser)   │──ws:/v1/chat/ui──▶  ESP32 Robot    │                 │           │
└──────────────┘                  │  (firmware)     │                 │  OpenClaw │
                                  │                 │◀────────────────│  Agent    │
                                  │  persistent WS  │                 │           │
                                  │  /v1/chat/      │                 │           │
                                  │  ingress        │                 │           │
                                  └────────┬────────┘                 └───────────┘
                                           │
                                   AES-256-GCM
                                   binary frames
                                           │
                                  ┌────────▼────────┐   HTTP POST    ┌──────────────────┐
                                  │  core_gateway    │ ─────────────▶│ openclaw-bridge   │
                                  │  (:8080)         │                │  (:9090)          │
                                  │                  │◀──────────────│  (stateless proxy) │
                                  │  FastAPI +       │   reply JSON   │                   │
                                  │  PostgreSQL +    │                └────────┬──────────┘
                                  │  GCS emulation   │                         │
                                  └────────┬─────────┘                  HTTP POST to
                                           │                         host.docker.internal
                                           │                              :19090
                                  ┌────────▼─────────┐
                                  │  host-listener    │
                                  │  (:19090)         │
                                  │  MessageStore     │
                                  │  (in-mem + file)  │
                                  └────────┬──────────┘
                                           │
                                  ┌────────▼─────────┐
                                  │  robot-responder  │  ←── Polls pending messages
                                  │  (auto-responder) │      Sends to OpenClaw Gateway
                                  └───────────────────┘      for LLM processing
                                           │
                                  ┌────────▼─────────┐
                                  │  agent-poller.py  │  ←── Manual CLI for debugging
                                  └───────────────────┘
```

### Components

| Component | Where | Role |
|-----------|-------|------|
| **core_gateway** | Docker | FastAPI server — WebSocket ingress, REST API, marketplace, WASM deploy, JWT auth, PostgreSQL + GCS |
| **openclaw-bridge** | Docker | Stateless HTTP relay between core_gateway and host-listener |
| **host-listener** | Host | Message queue server with `threading.Condition`, file persistence, per-robot reply queuing |
| **robot-responder** | Host | Auto-responder: polls host-listener, sends to OpenClaw Gateway, posts replies back |
| **agent-poller.py** | Host | CLI tool for manually checking pending messages and submitting replies |
| **mpx-wasm-deploy.py** | Host | CLI tool to deploy WASM skills to robots (list, download, encrypt, push) |

## Protocol (v2.0)

### Frame format (wire)

Each WebSocket frame: `[16B robot_uuid][12B IV][ciphertext][16B auth_tag]`

Encrypted with AES-256-GCM. Robot UUID is used as AAD.

### Upstream — robot → server

```json
{"type": "user_chat_input", "text": "move forward", "session_id": "<uuid>", "ts": 1700000000}
{"type": "session_reset", "session_id": "<uuid>", "ts": 1700000000}
```

### Downstream — server → robot (streaming)

```json
{"type": "step", "seq": 1, "total": 3, "text": "Thinking…", "ts": 1700000001}
{"type": "step", "seq": 2, "total": 3, "text": "Planning…", "ts": 1700000002}
{"type": "chat_reply", "text": "Walking forward!", "commands": [{"type": "lua", "script": "robot.gait('advance')"}], "ts": 1700000003}
```

The gateway streams intermediate `step` frames followed by one final `chat_reply`. Downstream messages only use **Lua commands** (no legacy `actions` array).

## REST API

### core_gateway (:8080)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/healthz` | Health check |
| `POST` | `/v1/auth/signup` | Register developer account |
| `POST` | `/v1/auth/login` | Login → JWT token |
| `POST` | `/v1/publish` | Publish a skill (WASM/AWA) to marketplace |
| `GET` | `/v1/skills` | List all marketplace skills |
| `GET` | `/v1/skills/{id}` | Get skill details |
| `GET` | `/v1/skills/{id}/versions` | List versions of a skill |
| `GET` | `/v1/skills/{id}/manifest` | Get skill manifest JSON |
| `GET` | `/v1/skills/check?slug=` | Check if slug is available |
| `POST` | `/v1/robots` | Register a new robot |
| `GET` | `/v1/robots` | List all robots |
| `GET` | `/v1/robots/{uuid}` | Get robot info |
| `POST` | `/v1/robots/{uuid}/deploy` | Deploy WASM skills to robot (REST) |
| `WS` | `/v1/chat/ingress` | Encrypted robot WebSocket |
| `WS` | `/` | Same as above (default ESP32 path) |

### openclaw-bridge (:9090)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/chat/process` | Forward decrypted message to host-listener |
| `GET` | `/v1/replies/{robot_uuid}` | Proxy reply-queue check |
| `GET` | `/v1/pending/{robot_uuid}` | Check for queued reply on reconnect |
| `GET` | `/healthz` | Health check |

### host-listener (:19090)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/chat/process` | Store message, wait for reply (Condition-based) |
| `GET` | `/v1/messages/pending` | List all pending messages |
| `POST` | `/v1/messages/{msg_id}/reply` | Submit a reply |
| `GET` | `/v1/replies/{robot_uuid}` | Consume queued reply (pop) |
| `GET` | `/healthz` | Health check with pending count |

## Queue System

### Message Flow

```
Robot → core_gateway (WS) → openclaw-bridge (HTTP) → host-listener
  ├─ store_pending() → memory + file
  ├─ wait_for_reply() → threading.Condition (no polling)
  ├─ robot-responder polls → sends to OpenClaw Gateway → gets LLM reply
  ├─ store_reply() → notify waiter → reply flows back through bridge → gateway → robot
  └─ if robot disconnected: reply queued by robot_uuid for reconnect delivery
```

### Key Features

| Feature | Implementation |
|---------|---------------|
| Wait mechanism | `threading.Condition.wait()` — zero CPU while waiting |
| Message storage | In-memory `dict` + file persistence |
| Lookup speed | O(1) dict lookup |
| Message expiry | TTL-based (default 5 min) + background cleanup every 60s |
| Thread safety | Explicit `threading.Lock` |
| Robot reconnect | Proactive queued-reply delivery via `_check_pending_reply()` |
| Per-session context | `session_id` scopes conversation history in OpenClaw |

## Marketplace

The core gateway includes a developer marketplace for WASM and AWA (Agentic Web Action) skills:

- **Publish**: `POST /v1/publish` with JWT auth → uploads to GCS + records in PostgreSQL
- **Discover**: `GET /v1/skills` — browse all published skills
- **Deploy**: `POST /v1/robots/{uuid}/deploy` — sends encrypted WASM + Lua commands to robot
- **CLI**: `mpx-wasm-deploy.py` for listing, downloading, encrypting, and pushing skills

### WASM Encryption

Deployed WASM binaries are encrypted per-robot using AES-256-GCM key wrapping:

```
robot_root_key ──(wrap)──→ per_skill_key ──(encrypt)──→ WASM binary
```

The encrypted blob uses the **MPXE** container format (magic `MPXE`, version `0x01`) for LittleFS storage on the robot.

## Setup

### 1. Prerequisites

- Docker and docker-compose
- Python 3.12+
- PostgreSQL (Cloud SQL emulation on port 5432)
- GCS emulator (on port 4443)
- OpenClaw running (port 18789)

### 2. Configure

```bash
cp .env.example .env
# Edit .env if needed (keys, DB creds, URLs)
```

### 3. Start the host listener

```bash
python3 host-listener.py --port 19090 --data-dir /tmp/mpx-bridge-data
```

### 4. Start the robot auto-responder

```bash
python3 robot-responder.py
```

Or install as systemd services:

```bash
sudo cp mpx-host-listener.service /etc/systemd/system/
sudo cp mpx-robot-responder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mpx-host-listener mpx-robot-responder
```

### 5. Start the Docker stack

```bash
docker compose up --build -d
```

### 6. Agent integration (manual)

```bash
# Check pending messages
python3 agent-poller.py check

# Reply with Lua commands
python3 agent-poller.py reply <msg_id> '{"text":"Hello!","commands":[{"type":"lua","script":"robot.gait(\"twerk\")"}]}'
```

### 7. WASM skill deployment

```bash
# List WASM skills assigned to a robot
python3 mpx-wasm-deploy.py list MPX-DOG-01

# Download artifacts locally
python3 mpx-wasm-deploy.py download MPX-DOG-01

# Push to robot (encrypted, via host-listener bridge)
python3 mpx-wasm-deploy.py push MPX-DOG-01

# Push unencrypted (development)
python3 mpx-wasm-deploy.py push MPX-DOG-01 --plain
```

## Development

```bash
# Build and run everything
docker compose up --build

# Run gateway outside Docker
cd core_gateway && pip install -r requirements.txt
OPENCLAW_BASE_URL=http://localhost:9090 uvicorn main:app --port 8080

# Run bridge outside Docker
cd openclaw-bridge && pip install -r requirements.txt
COGNITIVE_AGENT_URL=http://localhost:19090/v1/chat/process uvicorn main:app --port 9090
```

## Project Structure

```
.
├── docker-compose.yml              # Docker stack orchestration
├── core_gateway/                   # Main ingress gateway (Docker)
│   ├── main.py                     # FastAPI app: WS ingress, REST API, marketplace, auth
│   ├── config.py                   # Configuration dataclasses
│   ├── crypto.py                   # AES-256-GCM encrypt/decrypt + key store
│   ├── openclaw.py                 # OpenClaw HTTP client (streaming SSE)
│   ├── robots.py                   # Robot registration & management endpoints
│   ├── protocol/packets.py         # Pydantic models for wire protocol
│   ├── entrypoint.sh               # Container entrypoint
│   ├── requirements.txt
│   └── Dockerfile
├── openclaw-bridge/                # Stateless HTTP relay (Docker)
│   ├── main.py                     # FastAPI proxy with correlation IDs
│   ├── requirements.txt
│   └── Dockerfile
├── host-listener.py                # Message queue server (runs on host)
├── robot-responder.py              # Auto-responder: polls queue → OpenClaw Gateway
├── agent-poller.py                 # CLI for manual message check/reply
├── mpx-wasm-deploy.py              # WASM skill deployer CLI
├── mpx-host-listener.service       # systemd unit
├── mpx-robot-responder.service     # systemd unit
├── chat-ingress-spec.md            # Wire protocol specification (v2.0)
├── lua-bindings.md                 # Lua robot API reference
└── README.md
```

## Dependencies

- **FastAPI** + **Uvicorn** — async web framework
- **Cryptography** — AES-256-GCM
- **HTTPX** — async HTTP client with SSE streaming
- **asyncpg** — PostgreSQL driver
- **google-cloud-storage** — GCS emulation client
- **PyJWT** — JWT authentication
- **Pydantic** — data validation
