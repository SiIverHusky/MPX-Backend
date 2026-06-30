"""
openclaw-bridge — Stateless HTTP relay between core_gateway and a cognitive agent.

Accepts POST /v1/chat/process from core_gateway (decrypted chat frames) and
forwards them to the configured cognitive agent, returning the agent's JSON
reply with retries, backoff, input validation, and a safe fallback.

Portable design:
  - Runs as a Docker container alongside core_gateway
  - Cognitive agent URL is configurable via COGNITIVE_AGENT_URL env var
  - No dependency on any specific agent platform
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Logging — correlation ID via ContextVar
# ---------------------------------------------------------------------------
_cid_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")


class CorrelationIDFilter(logging.Filter):
    """Injects the current request's correlation ID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _cid_ctx.get()
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s openclaw-bridge [%(correlation_id)s] %(message)s",
)
logger = logging.getLogger("openclaw-bridge")
logger.addFilter(CorrelationIDFilter())


def _set_cid(request: Request | None = None) -> str:
    """Extract or generate a correlation ID and store it in the async context."""
    if request is not None:
        cid = request.headers.get("X-Correlation-ID", "").strip()
        if cid:
            _cid_ctx.set(cid)
            return cid
    cid = uuid4().hex[:12]
    _cid_ctx.set(cid)
    return cid


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
COGNITIVE_AGENT_URL = os.getenv(
    "COGNITIVE_AGENT_URL",
    "http://host.docker.internal:19090/v1/chat/process",
)
REQUEST_TIMEOUT = float(os.getenv("BRIDGE_TIMEOUT", "30.0"))
CONNECT_TIMEOUT = float(os.getenv("BRIDGE_CONNECT_TIMEOUT", "10.0"))
MAX_RETRIES = int(os.getenv("BRIDGE_MAX_RETRIES", "2"))
HOST = os.getenv("BRIDGE_HOST", "0.0.0.0")
PORT = int(os.getenv("BRIDGE_PORT", "9090"))
MAX_BODY_SIZE = int(os.getenv("BRIDGE_MAX_BODY_SIZE", "65536"))  # 64 KiB

FALLBACK_REPLY: dict[str, Any] = {
    "type": "chat_reply",
    "text": "🤖 Cognitive agent is offline. Please try again shortly.",
    "ts": int(time.time()),
    "actions": [{"gait": "none", "param": 0}],
    "commands": [],
}

# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------
_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT,
                read=REQUEST_TIMEOUT,
                write=REQUEST_TIMEOUT,
                pool=CONNECT_TIMEOUT,
            ),
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=10,
            ),
        )
    return _client


async def shutdown_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------

_CHAT_REPLY_SCHEMA = frozenset({"type", "text", "ts"})


def _validate_agent_reply(raw: str) -> dict[str, Any] | None:
    """Validate the agent's response is a parseable ``chat_reply``.

    Returns the parsed dict on success, ``None`` if validation fails.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None
    if data.get("type") != "chat_reply":
        return None
    if not isinstance(data.get("text"), str):
        return None
    # Basic structural check — must have at least the required fields
    if not _CHAT_REPLY_SCHEMA.issubset(data.keys()):
        return None
    return data


def _validate_incoming(body: Any) -> tuple[str, dict[str, Any], str | None]:
    """Validate the incoming request body from core_gateway.

    Returns ``(robot_uuid, message_dict, error_or_None)``.
    """
    if not isinstance(body, dict):
        return "unknown", {}, "request body must be a JSON object"

    robot_uuid = body.get("robot_uuid", "")
    if not isinstance(robot_uuid, str) or not robot_uuid.strip():
        return "unknown", {}, "missing or invalid 'robot_uuid'"

    msg = body.get("message")
    if not isinstance(msg, dict):
        return robot_uuid, {}, "missing or invalid 'message' (must be an object)"

    return robot_uuid, msg, None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "openclaw-bridge starting — forwarding to %s",
        COGNITIVE_AGENT_URL,
    )
    yield
    await shutdown_client()


app = FastAPI(title="OpenClaw Bridge", version="1.0.0", lifespan=lifespan)

# CORS — permissive for internal use; tighten if exposed externally
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz(request: Request):
    """Lightweight health check. Optionally probes the agent.

    If the query parameter ``probe=1`` is set, performs a lightweight GET
    against the agent's base URL to verify reachability.
    """
    probe = request.query_params.get("probe", "0") == "1"
    agent_reachable: bool | None = None

    if probe:
        agent_reachable = False
        try:
            client = await get_client()
            resp = await client.get(COGNITIVE_AGENT_URL, timeout=5.0)
            agent_reachable = resp.status_code < 500
        except Exception as exc:
            logger.warning("Health probe to agent failed: %s", exc)

    return JSONResponse(content={
        "status": "ok",
        "service": "openclaw-bridge",
        "version": "1.0.0",
        "agent_url": COGNITIVE_AGENT_URL,
        "agent_reachable": agent_reachable,
    })


# ---------------------------------------------------------------------------
# Main process endpoint
# ---------------------------------------------------------------------------


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
    cid = _set_cid(request)

    # ── Body-size guard ─────────────────────────────────────────
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_BODY_SIZE:
                logger.warning("Request body too large: %s bytes", content_length)
                return JSONResponse(
                    status_code=413,
                    content={"error": "request body too large"},
                )
        except ValueError:
            pass

    # ── Parse body ──────────────────────────────────────────────
    try:
        body = await request.json()
    except Exception:
        logger.warning("Invalid JSON from core_gateway")
        return JSONResponse(
            status_code=400,
            content={"error": "invalid JSON"},
        )

    # ── Validate incoming payload ───────────────────────────────
    robot_uuid, msg, err = _validate_incoming(body)
    if err:
        logger.warning("Validation error: %s", err)
        return JSONResponse(status_code=422, content={"error": err})

    logger.info(
        "Processing from %s: type=%s text=%.80s",
        robot_uuid,
        msg.get("type", "?"),
        (msg.get("text") or "")[:80],
    )

    # ── Forward to cognitive agent ──────────────────────────────
    reply = await _forward_to_agent(body, cid)

    return JSONResponse(content=reply)


# ---------------------------------------------------------------------------
# Forwarding with retry + exponential backoff
# ---------------------------------------------------------------------------


async def _forward_to_agent(payload: dict, cid: str) -> dict[str, Any]:
    """POST *payload* to the cognitive agent URL with retries + backoff.

    Returns a parsed ``chat_reply`` dict (fallback if all retries fail).
    """
    client = await get_client()
    headers = {"X-Correlation-ID": cid}

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = await client.post(
                COGNITIVE_AGENT_URL,
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            raw = resp.text

            # Validate the response is a proper chat_reply
            validated = _validate_agent_reply(raw)
            if validated is not None:
                logger.debug(
                    "Agent replied (attempt %d): status=%d size=%d",
                    attempt + 1,
                    resp.status_code,
                    len(raw),
                )
                return validated

            # Response was valid JSON but not a valid chat_reply
            logger.warning(
                "Agent returned invalid chat_reply schema (attempt %d/%d): %.200s",
                attempt + 1,
                MAX_RETRIES + 1,
                raw[:200],
            )

        except httpx.TimeoutException as exc:
            logger.warning(
                "Agent timeout (attempt %d/%d): %s",
                attempt + 1,
                MAX_RETRIES + 1,
                exc,
            )
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Agent HTTP %d (attempt %d/%d): %.200s",
                exc.response.status_code,
                attempt + 1,
                MAX_RETRIES + 1,
                exc.response.text[:200],
            )
        except httpx.RequestError as exc:
            logger.warning(
                "Agent connection error (attempt %d/%d): %s",
                attempt + 1,
                MAX_RETRIES + 1,
                exc,
            )
        except json.JSONDecodeError as exc:
            logger.warning(
                "Agent returned non-JSON (attempt %d/%d): %s",
                attempt + 1,
                MAX_RETRIES + 1,
                exc,
            )

        # ── Exponential backoff before next attempt ──────────
        if attempt < MAX_RETRIES:
            backoff = 0.5 * (2 ** attempt)  # 0.5s, 1s, 2s, …
            logger.debug("Retrying in %.1fs …", backoff)
            await asyncio.sleep(backoff)

    logger.error(
        "Cognitive agent unreachable after %d attempts — using fallback",
        MAX_RETRIES + 1,
    )
    return dict(FALLBACK_REPLY, ts=int(time.time()))



