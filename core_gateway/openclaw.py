from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncGenerator

import httpx

from config import openclaw_settings

logger = logging.getLogger("core_gateway.openclaw")

# ---------------------------------------------------------------------------
# HTTP client singleton
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    """Return the shared ``httpx.AsyncClient``, creating it lazily."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=openclaw_settings.base_url,
            timeout=openclaw_settings.request_timeout,
            limits=httpx.Limits(max_keepalive_connections=10),
        )
        logger.info(
            "OpenClaw client created: base_url=%s timeout=%.1fs max_retries=%d",
            openclaw_settings.base_url,
            openclaw_settings.request_timeout,
            openclaw_settings.max_retries,
        )
    return _client


async def shutdown_client() -> None:
    """Gracefully close the shared HTTP client."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("OpenClaw client shut down")


# ---------------------------------------------------------------------------
# OpenClaw process call
# ---------------------------------------------------------------------------

FALLBACK_REPLY = json.dumps({
    "type": "chat_reply",
    "text": "I'm sorry, I'm having trouble connecting to my brain. Please try again.",
    "ts": int(time.time()),
    "commands": [],
})

FALLBACK_REPLY_DICT = json.loads(FALLBACK_REPLY)


async def openclaw_process(msg: dict[str, Any], robot_uuid: str) -> str:
    """Send a decrypted message to OpenClaw (non-streaming) and get a reply.

    Args:
        msg: Decrypted message dict. Expected keys:
            - ``type``: ``"user_chat_input"`` or ``"session_reset"``
            - ``text``: user's message text (for ``user_chat_input``)
            - ``session_id``: conversation UUID (optional)
            - ``ts``: Unix timestamp
        robot_uuid: Robot identifier string (e.g. ``"MPX-DOG-01"``).

    Returns:
        JSON string to encrypt and send back downstream.

    If OpenClaw is unreachable or returns an error after all retries, a
    safe fallback reply is returned so the robot never hangs.
    """
    client = await get_client()

    headers: dict[str, str] = {}
    if openclaw_settings.api_key:
        headers["Authorization"] = f"Bearer {openclaw_settings.api_key}"

    payload = {
        "robot_uuid": robot_uuid,
        "message": msg,
    }

    last_exc: Exception | None = None

    for attempt in range(openclaw_settings.max_retries + 1):
        try:
            resp = await client.post("/v1/chat/process", json=payload, headers=headers)
            resp.raise_for_status()
            logger.debug(
                "OpenClaw replied (attempt %d): status=%d size=%d",
                attempt + 1, resp.status_code, len(resp.content),
            )
            return resp.text
        except httpx.TimeoutException as exc:
            last_exc = exc
            logger.warning(
                "OpenClaw timeout (attempt %d/%d): %s",
                attempt + 1, openclaw_settings.max_retries + 1, exc,
            )
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            logger.warning(
                "OpenClaw HTTP %d (attempt %d/%d): %.200s",
                exc.response.status_code,
                attempt + 1, openclaw_settings.max_retries + 1,
                exc.response.text[:200],
            )
        except httpx.RequestError as exc:
            last_exc = exc
            logger.warning(
                "OpenClaw connection error (attempt %d/%d): %s",
                attempt + 1, openclaw_settings.max_retries + 1, exc,
            )

        if attempt < openclaw_settings.max_retries:
            continue

    # All retries exhausted
    logger.error(
        "OpenClaw unreachable after %d attempts — using fallback reply",
        openclaw_settings.max_retries + 1,
    )
    return FALLBACK_REPLY


# ---------------------------------------------------------------------------
# Streaming OpenClaw process call
# ---------------------------------------------------------------------------

async def openclaw_process_stream(
    msg: dict[str, Any],
    robot_uuid: str,
) -> AsyncGenerator[dict[str, Any], None]:
    """Send a message to OpenClaw and yield each downstream message as it arrives.

    OpenClaw returns a stream of JSON objects (one per line via SSE or
    JSON-lines format):
      1. Zero or more ``step`` messages (intermediate progress)
      2. One ``chat_reply`` message (final response with Lua commands)

    Args:
        msg: Decrypted message dict with ``type``, ``text``, ``session_id``.
        robot_uuid: Robot identifier string.

    Yields:
        Dicts representing downstream messages (``step`` or ``chat_reply``).

    If streaming is not supported by OpenClaw (falls back to non-streaming),
    a single ``chat_reply`` dict is yielded.
    """
    client = await get_client()

    headers: dict[str, str] = {
        "Accept": "text/event-stream",
    }
    if openclaw_settings.api_key:
        headers["Authorization"] = f"Bearer {openclaw_settings.api_key}"

    payload = {
        "robot_uuid": robot_uuid,
        "message": msg,
    }

    try:
        async with client.stream(
            "POST",
            "/v1/chat/process-stream",
            json=payload,
            headers=headers,
            timeout=openclaw_settings.request_timeout,
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                # SSE format: "data: <json>" or raw JSON-lines
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
            # Streaming endpoint not available — fall back to non-streaming
            logger.info(
                "Streaming endpoint unavailable for %s — using non-streaming",
                robot_uuid,
            )
            reply_str = await openclaw_process(msg, robot_uuid)
            yield json.loads(reply_str)
            return
        logger.warning(
            "OpenClaw streaming HTTP %d for %s: %.200s",
            exc.response.status_code, robot_uuid, exc.response.text[:200],
        )
        yield FALLBACK_REPLY_DICT
    except Exception as exc:
        logger.warning(
            "OpenClaw streaming error for %s: %s",
            robot_uuid, exc,
        )
        yield FALLBACK_REPLY_DICT


# ---------------------------------------------------------------------------
# Queued reply check (for robot reconnect)
# ---------------------------------------------------------------------------
# When a robot reconnects, we check if there's a queued reply waiting
# from a previous disconnect.  The bridge proxies this to the host-listener.


async def check_queued_reply(robot_uuid: str) -> dict[str, Any] | None:
    """Check if there's a queued reply for *robot_uuid* on the host-listener.

    Returns the reply dict if one is available, or ``None`` if no reply
    is queued or the upstream is unreachable.

    This is called by ``_handle_ingress`` on robot (re)connect so that
    any reply that was generated while the robot was offline is delivered
    immediately without requiring a new chat message.
    """
    client = await get_client()
    try:
        resp = await client.get(
            f"/v1/replies/{robot_uuid}",
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            logger.info("Found queued reply for %s — will deliver on reconnect", robot_uuid)
            return data
        elif resp.status_code == 404:
            return None
        else:
            logger.debug(
                "Reply check for %s returned HTTP %d",
                robot_uuid,
                resp.status_code,
            )
            return None
    except httpx.TimeoutException:
        logger.debug("Reply check timeout for %s (upstream may be down)", robot_uuid)
        return None
    except httpx.RequestError as exc:
        logger.debug("Reply check error for %s: %s", robot_uuid, exc)
        return None
