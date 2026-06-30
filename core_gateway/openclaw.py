from __future__ import annotations

import json
import logging
import time
from typing import Any

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


async def openclaw_process(msg: dict[str, Any], robot_uuid: str) -> str:
    """Send a decrypted message to OpenClaw and get a reply JSON string.

    Args:
        msg: Decrypted message dict. Expected keys:
            - ``type``: ``"user_chat_input"`` or ``"session_reset"``
            - ``text``: user's message text (for ``user_chat_input``)
            - ``ts``: Unix timestamp
        robot_uuid: Robot identifier string (e.g. ``"MPX-DOG-01"``).

    Returns:
        JSON string to encrypt and send back downstream.  Must be a valid
        ``chat_reply`` per CLOUD_INGRESS.md §4.2.

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
