"""
openclaw-bridge — Stateless HTTP relay between core_gateway and a cognitive agent.

Accepts POST /v1/chat/process from core_gateway (encrypted chat frames that
have already been decrypted by core_gateway) and forwards them to the
configured cognitive agent URL, returning the agent's JSON reply.

Portable design:
  - Runs as a Docker container alongside core_gateway
  - Cognitive agent URL is configurable via COGNITIVE_AGENT_URL env var
  - No dependency on any specific agent platform
  - Retry + fallback logic mirrors core_gateway's openclaw.py
"""

from __future__ import annotations

import json
import logging
import os
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s openclaw-bridge %(message)s",
)
logger = logging.getLogger("openclaw-bridge")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
COGNITIVE_AGENT_URL = os.getenv(
    "COGNITIVE_AGENT_URL",
    "http://host.docker.internal:19090/v1/chat/process",
)
REQUEST_TIMEOUT = float(os.getenv("BRIDGE_TIMEOUT", "30.0"))
MAX_RETRIES = int(os.getenv("BRIDGE_MAX_RETRIES", "2"))
HOST = os.getenv("BRIDGE_HOST", "0.0.0.0")
PORT = int(os.getenv("BRIDGE_PORT", "9090"))

FALLBACK_REPLY = json.dumps({
    "type": "chat_reply",
    "text": "🤖 Cognitive agent is offline. Please try again shortly.",
    "ts": int(time.time()),
    "actions": [{"gait": "none", "param": 0}],
    "commands": [],
})

# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            limits=httpx.Limits(max_keepalive_connections=10),
        )
    return _client


async def shutdown_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "openclaw-bridge starting — forwarding to %s",
        COGNITIVE_AGENT_URL,
    )
    yield
    await shutdown_client()


app = FastAPI(title="OpenClaw Bridge", version="1.0.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "openclaw-bridge", "version": "1.0.0"}


@app.post("/v1/chat/process")
async def chat_process(request: Request):
    """Receive a decrypted chat message from core_gateway and relay it to the
    cognitive agent.

    Request body (from core_gateway):
      {
        "robot_uuid": "MPX-DOG-01",
        "message": {
          "type": "user_chat_input",
          "text": "hello robot"
        }
      }

    Response body (expected from cognitive agent):
      A ChatReply JSON object:
      {
        "type": "chat_reply",
        "text": "Hello!",
        "actions": [{"gait": "wag", "param": 1}],
        "commands": []
      }
    """
    try:
        body = await request.json()
    except Exception:
        logger.warning("Invalid JSON from core_gateway")
        return JSONResponse(
            status_code=400,
            content={"error": "invalid JSON"},
        )

    robot_uuid = body.get("robot_uuid", "unknown")
    msg = body.get("message", {})
    logger.info(
        "Processing from %s: type=%s text=%.80s",
        robot_uuid,
        msg.get("type"),
        msg.get("text", ""),
    )

    # Forward to cognitive agent
    reply = await _forward_to_agent(robot_uuid, body)

    # Must return raw JSON text — core_gateway encrypts it
    return JSONResponse(content=json.loads(reply))


async def _forward_to_agent(robot_uuid: str, payload: dict) -> str:
    """POST the payload to the cognitive agent URL with retries."""
    client = await get_client()
    last_exc: Exception | None = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await client.post(
                COGNITIVE_AGENT_URL,
                json=payload,
            )
            resp.raise_for_status()
            text = resp.text
            logger.debug(
                "Agent replied (attempt %d): status=%d size=%d",
                attempt + 1,
                resp.status_code,
                len(text),
            )
            # Validate it's parseable JSON
            json.loads(text)
            return text
        except httpx.TimeoutException as exc:
            last_exc = exc
            logger.warning(
                "Agent timeout (attempt %d/%d): %s",
                attempt + 1,
                MAX_RETRIES + 1,
                exc,
            )
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            logger.warning(
                "Agent HTTP %d (attempt %d/%d): %.200s",
                exc.response.status_code,
                attempt + 1,
                MAX_RETRIES + 1,
                exc.response.text[:200],
            )
        except httpx.RequestError as exc:
            last_exc = exc
            logger.warning(
                "Agent connection error (attempt %d/%d): %s",
                attempt + 1,
                MAX_RETRIES + 1,
                exc,
            )
        except json.JSONDecodeError as exc:
            last_exc = exc
            logger.warning(
                "Agent returned non-JSON (attempt %d/%d): %s",
                attempt + 1,
                MAX_RETRIES + 1,
                exc,
            )

        if attempt < MAX_RETRIES:
            continue

    logger.error(
        "Cognitive agent unreachable after %d attempts — using fallback",
        MAX_RETRIES + 1,
    )
    return FALLBACK_REPLY
