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
                                                                     ┌────┴───────┐
                                                                     │  pending/  │
                                                                     │  replies/  │ ←── Agent reads/writes via
                                                                     └────────────┘     agent-poller.py
```

### Components

- **core_gateway** (Docker): FastAPI WebSocket server. Receives encrypted binary frames from MPX robot hardware, decrypts them with AES-256-GCM, and forwards the decoded JSON to the cognitive agent via HTTP POST.

- **openclaw-bridge** (Docker): Stateless HTTP proxy. Receives decrypted chat messages from core_gateway and forwards them to the configured cognitive agent URL. Configurable via `COGNITIVE_AGENT_URL`.

- **host-listener** (runs on host): Lightweight HTTP server that accepts requests from the Docker bridge. Stores pending messages and lets the agent submit replies. Blocks until the agent responds.

- **agent-poller.py**: CLI tool for the agent to check pending messages and submit replies.

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
2. The message is stored in `pending/` as a JSON file
3. The host-listener blocks, waiting for a reply
4. The agent (you) polls for messages and submits a reply

```bash
# From the agent session, check for pending messages
python3 /home/mangdang/mpx-server/agent-poller.py check

# Submit a reply
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
