# MPX Chat Ingress Gateway

Encrypted WebSocket bridge between MPX robot hardware and a cognitive agent (OpenClaw).

## Architecture

```
┌──────────────┐   AES-256-GCM   ┌──────────────┐   HTTP POST    ┌──────────────────┐   HTTP POST    ┌───────────┐
│  Robot HW    │  WebSocket      │  core_gateway │ ─────────────→ │ openclaw-bridge  │ ─────────────→ │ Agent     │
│  (ESP32)     │ ←─────────────→ │  (:8080)      │                │  (:9090)         │                │ (OpenClaw)│
└──────────────┘   encrypted     └──────────────┘   decrypted     └────────┬─────────┘   decrypted     └───────────┘
                                   frames           JSON                   │                            (on host)
                                                                           │ HTTP POST from Docker
                                                                           │ to host.docker.internal:19090
                                                                     ┌─────┴──────┐
                                                                     │ host-      │
                                                                     │ listener   │
                                                                     │ (:19090)   │
                                                                     └────┬───────┘
                                                                          │
                                                                     ┌────┴─────────────────────────────────┐
                                                                     │  MessageStore  (in-memory + files)    │
                                                                     │  ┌──────────┐  ┌───────────────────┐  │
                                                                     │  │ pending  │  │ replies           │  │
                                                                     │  │ by_robot │  │ by_robot (queue)  │  │
                                                                     │  └──────────┘  └───────────────────┘  │
                                                                     │  - threading.Condition (no polling)    │
                                                                     │  - Background cleanup (TTL: 5 min)     │
                                                                     └────────────────────────────────────────┘
                                                                          │
                                                                     ┌────┴───────┐
                                                                     │agent-poller│ ←── Agent reads/writes
                                                                     │auto-respond│     via CLI
                                                                     └────────────┘
```

### Components

- **core_gateway** (Docker): FastAPI WebSocket server. Receives encrypted binary frames from MPX robot hardware, decrypts them with AES-256-GCM, and forwards the decoded JSON to the cognitive agent via HTTP POST. On robot reconnect, proactively checks for queued replies from the host-listener.

- **openclaw-bridge** (Docker): Stateless HTTP proxy. Receives decrypted chat messages from core_gateway and forwards them to the configured cognitive agent URL. Also provides `GET /v1/replies/<robot_uuid>` to proxy reply-queue checks from core_gateway to the host-listener. Configurable via `COGNITIVE_AGENT_URL`.

- **host-listener** (runs on host): Message queue server that accepts requests from the Docker bridge. Implements a proper queue system with:
  - **Thread-safe in-memory MessageStore** with O(1) lookups
  - **Efficient condition-based waiting** (`threading.Condition`) — no busy-polling
  - **File-backed persistence** for durability across restarts
  - **Automatic message expiry** (configurable TTL, default 5 min) with background cleanup
  - **Per-robot reply queue** — if the robot disconnects before the agent replies, the reply is queued by `robot_uuid` and delivered on reconnect

- **agent-poller.py**: CLI tool for the agent to check pending messages and submit replies (includes `X-Robot-UUID` header for queue support).

## Queue System

### Message Flow

```
Robot sends message:
  core_gateway ──POST──→ openclaw-bridge ──POST──→ host-listener
                                                      │
                                                      ├── store_pending() → memory + file
                                                      ├── wait_for_reply() → threading.Condition
                                                      │      │
                                  Agent replies ◄─────┘      │
                                                      │
                                                      ├── store_reply() → notify waiter
                                                      ├── reply returned to bridge → gateway → robot
                                                      └── if robot disconnected: queue by robot_uuid
```

### Key Improvements over Original

| Feature | Original | New |
|---------|----------|-----|
| Wait mechanism | `time.sleep(1)` busy-polling | `threading.Condition.wait()` — zero CPU while waiting |
| Message storage | File I/O only (slow) | In-memory `dict` + file persistence |
| Lookup speed | O(n) file scan | O(1) dict lookup |
| Message expiry | None — stale messages accumulated | TTL-based (5 min default) + background cleanup |
| Thread safety | Implicit (GIL only) | Explicit `threading.Lock` |
| Robot reconnect | Manual polling only | Proactive delivery via `check_queued_reply()` |
| Reply queue | File-based, consumed on demand | In-memory + file, consumed atomically |

### Message TTL

Messages expire after `MESSAGE_TTL_SEC` (default: 300 seconds / 5 minutes). A background cleanup thread runs every 60 seconds to remove expired messages and orphaned reply files. Expired messages are lazily removed from `list_pending()` results as well.

## Setup

### 1. Prerequisites

- Docker and docker-compose
- Python 3.10+
- OpenClaw running (port 18789)

### 2. Configure

```bash
cp .env.example .env
# Edit .env if needed
```

### 3. Start the host listener

```bash
# Manual (for testing)
python3 host-listener.py --port 19090 --data-dir /tmp/mpx-bridge-data

# Or install as systemd service
sudo cp mpx-host-listener.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mpx-host-listener
```

### 4. Start the Docker stack

```bash
docker compose up --build -d
```

### 5. Agent integration

When the robot sends a message through the gateway:

1. The message flows: robot → core_gateway → openclaw-bridge → host-listener
2. The message is stored in the MessageStore (memory + file)
3. The host-listener blocks efficiently using `threading.Condition`, waiting for a reply
4. The agent (you) polls for messages and submits a reply
5. The reply is stored, the waiter is notified, and the reply flows back to the robot
6. If the robot disconnected, the reply is queued by `robot_uuid` and delivered on reconnect

```bash
# From the agent session, check for pending messages
python3 /home/mangdang/mpx-server/agent-poller.py check

# Submit a reply (automatically sets X-Robot-UUID for queue support)
python3 /home/mangdang/mpx-server/agent-poller.py reply <msg_id> '{"text":"Hello!","actions":[{"gait":"wag","param":1}]}'
```

## Protocol

### Frame format (wire)

Each WebSocket frame: `[16B robot_uuid][12B IV][ciphertext][16B auth_tag]`

Encrypted with AES-256-GCM. Robot UUID is used as AAD.

### Upstream (robot → server)

```json
{"type": "user_chat_input", "text": "move forward"}
{"type": "session_reset", "ts": 1234567890}
```

### Downstream (server → robot)

```json
{
  "type": "chat_reply",
  "text": "Walking forward!",
  "actions": [{"gait": "walk", "param": 1}],
  "commands": [{"type": "lua", "script": "move(10)"}]
}
```

### REST API (host-listener)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/chat/process` | Receive message from bridge, block for reply |
| `GET` | `/v1/messages/pending` | List all pending messages |
| `POST` | `/v1/messages/<msg_id>/reply` | Submit a reply for a pending message |
| `GET` | `/v1/replies/<robot_uuid>` | Consume queued reply for a robot (pop) |
| `GET` | `/healthz` | Health check with pending count |

### REST API (openclaw-bridge)

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/v1/chat/process` | Forward message to host-listener, return reply |
| `GET` | `/v1/replies/<robot_uuid>` | Proxy reply-queue check to host-listener |
| `GET` | `/healthz` | Health check (optional agent probe) |

## Development

```bash
# Build and run everything
docker compose up --build

# Run just the gateway outside Docker (for debugging)
cd core_gateway && pip install -r requirements.txt
OPENCLAW_BASE_URL=http://localhost:9090 uvicorn main:app --port 8080

# Run just the bridge outside Docker
cd openclaw-bridge && pip install -r requirements.txt
COGNITIVE_AGENT_URL=http://localhost:19090/v1/chat/process uvicorn main:app --port 9090
```
