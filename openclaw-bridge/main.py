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
from typing import Any, AsyncGenerator
from uuid import uuid4

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Logging — correlation ID via ContextVar
# ---------------------------------------------------------------------------
# NOTE: we do NOT use logging.basicConfig() here because that sets the format
# on the root handler, which breaks other loggers (httpx, uvicorn, …) that
# don't have the ``correlation_id`` field in their records.  Instead we
# create a dedicated handler for *our* logger only.
_cid_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")


class CorrelationIDFilter(logging.Filter):
    """Injects the current request's correlation ID into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = _cid_ctx.get()
        return True


_handler = logging.StreamHandler()
_handler.setLevel(logging.INFO)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s openclaw-bridge [%(correlation_id)s] %(message)s",
))

logger = logging.getLogger("openclaw-bridge")
logger.setLevel(logging.INFO)
logger.addHandler(_handler)
logger.addFilter(CorrelationIDFilter())
logger.propagate = False  # don't duplicate to root handler

# Suppress noisy httpx request/response logging
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


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


app = FastAPI(title="OpenClaw Bridge", version="2.0.0", lifespan=lifespan)

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
        "version": "2.0.0",
        "agent_url": COGNITIVE_AGENT_URL,
        "agent_reachable": agent_reachable,
    })


# ---------------------------------------------------------------------------
# Queue — pending reply endpoint
# ---------------------------------------------------------------------------


@app.get("/v1/pending/{robot_uuid}")
async def pending_reply(request: Request, robot_uuid: str):
    """Check for a queued reply waiting for this robot.

    Polled by core_gateway when the robot (re)connects.  If the agent
    replied after the previous connection dropped, this returns the reply
    so it can be sent on the fresh socket.

    Returns the ChatReply JSON on success, or 404 with
    ``{"status": "no_reply"}`` if nothing is queued.
    """
    cid = _set_cid(request)
    client = await get_client()

    agent_url = COGNITIVE_AGENT_URL.rstrip("/v1/chat/process").rstrip("/")
    # Derive the host-listener's pending endpoint from the agent URL
    pending_url = f"{agent_url}/v1/replies/{robot_uuid}"

    try:
        resp = await client.get(pending_url, timeout=5.0)
        if resp.status_code == 200:
            logger.info(
                "Queued reply found for %s (pending check) — delivering",
                robot_uuid,
            )
            return JSONResponse(content=resp.json())
    except httpx.RequestError as exc:
        logger.debug(
            "Pending check for %s failed: %s", robot_uuid, exc,
        )

    logger.debug("No queued reply for %s", robot_uuid)
    return JSONResponse(
        status_code=404,
        content={"status": "no_reply", "robot_uuid": robot_uuid},
    )


# ---------------------------------------------------------------------------
# Lua output — proxy to host-listener
# ---------------------------------------------------------------------------


@app.post("/v1/chat/lua-output")
async def lua_output(request: Request):
    """Receive Lua output from core_gateway and proxy to the host-listener.

    The host-listener's ``/v1/lua/output`` endpoint stores the output as a
    pending ``user_chat_input`` message which robot-responder picks up.

    Expects JSON: ``{"robot_uuid": "...", "text": "...", "session_id": "..."}``
    """
    cid = _set_cid(request)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    robot_uuid = body.get("robot_uuid", "unknown")
    text = body.get("text", "")
    session_id = body.get("session_id", "")

    if not text:
        return JSONResponse(status_code=400, content={"error": "text field is required"})

    # Proxy to the host-listener's /v1/lua/output
    agent_base = COGNITIVE_AGENT_URL.rstrip("/v1/chat/process").rstrip("/")
    lua_url = f"{agent_base}/v1/lua/output"

    client = await get_client()
    try:
        resp = await client.post(
            lua_url,
            json=body,
            timeout=httpx.Timeout(connect=5.0, read=5.0, write=5.0),
        )
        if resp.status_code == 201:
            logger.info(
                "Lua output proxied for %s (session=%s): %.100s",
                robot_uuid, session_id, text[:100],
            )
        else:
            logger.warning(
                "Host-listener returned HTTP %d for Lua output: %.200s",
                resp.status_code, resp.text[:200],
            )
        return JSONResponse(
            status_code=resp.status_code,
            content=resp.json() if resp.text else {"status": "ok"},
        )
    except httpx.RequestError as exc:
        logger.warning("Failed to proxy Lua output to host-listener: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"error": "host-listener unreachable"},
        )


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
# Streaming process endpoint (SSE)
# ---------------------------------------------------------------------------


@app.post("/v1/chat/process-stream")
async def chat_process_stream(request: Request):
    """Receive a decrypted chat message and stream back step+chat_reply messages
    via Server-Sent Events (SSE).

    This endpoint is preferred over ``/v1/chat/process`` because it allows
    OpenClaw to send intermediate ``step`` messages before the final
    ``chat_reply``, enabling real-time progress in the PWA.

    The response is a SSE stream where each ``data:`` line is a JSON object
    (``step`` or ``chat_reply``), terminated by ``data: [DONE]``.
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
        "Streaming from %s: type=%s session=%s text=%.80s",
        robot_uuid,
        msg.get("type", "?"),
        msg.get("session_id", ""),
        (msg.get("text") or "")[:80],
    )

    # ── Forward to cognitive agent with streaming ───────────────
    async def event_stream():
        async for downstream_msg in _forward_to_agent_stream(body, cid):
            yield f"data: {json.dumps(downstream_msg)}\n\n"
        yield "data: [DONE]\n\n"

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Correlation-ID": cid,
        },
    )


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


# ---------------------------------------------------------------------------
# Streaming forward — yields step + chat_reply messages
# ---------------------------------------------------------------------------


async def _forward_to_agent_stream(
    payload: dict,
    cid: str,
) -> AsyncGenerator[dict[str, Any], None]:
    """POST *payload* to the cognitive agent and stream the response.

    Yields dicts representing downstream messages.  Typically zero or more
    ``step`` messages, then a final ``chat_reply`` message.

    Falls back to ``_forward_to_agent`` if the agent doesn't support streaming.
    """
    client = await get_client()
    headers = {
        "X-Correlation-ID": cid,
        "Accept": "text/event-stream",
    }

    stream_url = COGNITIVE_AGENT_URL.replace(
        "/v1/chat/process", "/v1/chat/process-stream",
    )

    try:
        async with client.stream(
            "POST",
            stream_url,
            json=payload,
            headers=headers,
            timeout=httpx.Timeout(
                connect=CONNECT_TIMEOUT,
                read=None,    # streaming — no read timeout
                write=REQUEST_TIMEOUT,
                pool=CONNECT_TIMEOUT,
            ),
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("data: "):
                    line = line[6:]
                if line == "[DONE]":
                    return
                try:
                    parsed = json.loads(line)
                    yield parsed
                except json.JSONDecodeError:
                    logger.warning("Unparseable streaming line: %.80s", line)
                    continue

    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            logger.info(
                "Streaming endpoint unavailable — falling back to non-streaming",
            )
            reply = await _forward_to_agent(payload, cid)
            yield reply
            return
        logger.warning(
            "Agent streaming HTTP %d: %.200s",
            exc.response.status_code,
            exc.response.text[:200],
        )
        yield dict(FALLBACK_REPLY, ts=int(time.time()))
    except Exception as exc:
        logger.warning("Agent streaming error: %s", exc)
        yield dict(FALLBACK_REPLY, ts=int(time.time()))
