#!/usr/bin/env python3
"""
robot-responder — Bridges the MPX message queue to the OpenClaw agent.

Uses the Gateway's /v1/chat/completions endpoint with session-aware routing.

Architecture:
  host-listener ← (poll) ← robot-responder
                              ↓
                  POST /v1/chat/completions (with user={robot}:{session})
                              ↓
                  reply POSTed directly to host-listener /v1/messages/<id>/reply

Per-robot, per-session context is maintained by the Gateway — the `user` field
derives a stable session key from (robot_uuid, session_id). New conversations
(getting a fresh session_id from the PWA) automatically start clean sessions.

Usage:
  # Test run
  python3 robot-responder.py

  # Install as systemd service
  sudo cp mpx-robot-responder.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now mpx-robot-responder
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST_LISTENER_URL = os.getenv(
    "HOST_LISTENER_URL", "http://127.0.0.1:19090"
)
GATEWAY_CHAT_URL = os.getenv(
    "GATEWAY_CHAT_URL", "http://127.0.0.1:18789/v1/chat/completions"
)
GATEWAY_TOKEN = (
    os.getenv("GATEWAY_TOKEN")
    or os.getenv("GATEWAY_HOOK_TOKEN")
    or ""
)
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.0"))
CHAT_TIMEOUT = float(os.getenv("CHAT_TIMEOUT", "600.0"))

# If not set via env, try to read from the OpenClaw config file
if not GATEWAY_TOKEN:
    _config_paths = [
        os.path.expanduser("~/.openclaw/openclaw.json"),
    ]
    for _p in _config_paths:
        try:
            with open(_p) as _f:
                _cfg = json.load(_f)
            # Try the gateway auth token first (correct for /v1/chat/completions)
            _gw_auth = _cfg.get("gateway", {}).get("auth", {})
            if _gw_auth.get("mode") in ("token", "password") and _gw_auth.get("token"):
                GATEWAY_TOKEN = _gw_auth["token"]
                break
            # Fallback: hooks token (legacy)
            _hooks = _cfg.get("hooks", {})
            if _hooks.get("enabled") and _hooks.get("token"):
                GATEWAY_TOKEN = _hooks["token"]
                break
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            continue

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s robot-responder %(message)s",
)
logger = logging.getLogger("robot-responder")

# ---------------------------------------------------------------------------
# Track seen message IDs (in-memory set — resets on restart, which is fine)
# ---------------------------------------------------------------------------

_seen: set[str] = set()
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    logger.info("Received signal %d — shutting down...", signum)


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def fetch_pending() -> list[dict]:
    """Fetch the list of pending messages from the host-listener."""
    req = urllib.request.Request(f"{HOST_LISTENER_URL}/v1/messages/pending")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        logger.debug("Error fetching pending: %s", e)
        return []


# ---------------------------------------------------------------------------
# System prompt — describes robot capabilities and reply format
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are MPX, a friendly robot dog. Respond conversationally and naturally.

Your response MUST be valid JSON with these fields:
- "text": what you say to the user (string)
- "commands": array of Lua command objects (can be empty)

Each command: {"type": "lua", "script": "<lua code>"}

Available Lua commands:
| Intent            | Lua command                                    |
|-------------------|------------------------------------------------|
| Walk forward      | robot.gait('advance')                          |
| Stop / sit        | robot.gait('none')                             |
| Walk backward     | robot.gait('back')                             |
| Turn left         | robot.gait('turnL')                            |
| Turn right        | robot.gait('turnR')                            |
| Strafe left       | robot.gait('left')                             |
| Strafe right      | robot.gait('right')                            |
| Jump              | robot.gait('jump')                             |
| Jump forward      | robot.gait('jumpfwd')                          |
| Look up           | robot.gait('lookup')                           |
| Look down         | robot.gait('lookdown')                         |
| Look left         | robot.gait('lookleft')                         |
| Look right        | robot.gait('lookright')                        |
| Wag tail          | robot.gait('twerk')                            |
| Body roll         | robot.gait('roll')                             |
| Body pitch        | robot.gait('pitch')                            |
| Stretch           | robot.gait('stretch')                          |
| Dance             | robot.gait('bodycycle')                        |
| Raise body        | robot.gait('heightup')                         |
| Lower body        | robot.gait('heightdown')                       |
| Balance pose      | robot.gait('balance')                          |
| Bow back          | robot.gait('bowback')                          |
| Read IMU          | local i=robot.imu_read() print(i.ax,i.ay,i.az,i.gx,i.gy,i.gz) |

Rules:
1. Use "commands" array with Lua scripts — the robot executes them sequentially.
2. Use robot.delay_ms(N) for timing between gait changes.
3. Always include "text" for the PWA display.
4. Respond as a friendly robot dog.
5. Respond with ONLY the raw JSON object on a single line — NO markdown formatting, NO code fences, NO ```json or ``` blocks. Just the JSON.
6. For multi-step tasks, use multiple commands in the array — they execute in order.
7. If a task is not feasible (e.g., websites block your requests, data is unavailable, or the task is inherently impossible), tell the user clearly instead of silently retrying with different approaches. Say what you found and why it didn't work."""


def process_message(message: dict, robot_uuid: str, msg_id: str) -> bool:
    """Send a chat message to the Gateway and submit its reply.

    POSTs to /v1/chat/completions with a session key derived from
    (robot_uuid, session_id) for automatic conversation history.
    Then POSTs the reply directly to the host-listener.

    Returns True if the reply was submitted successfully, False otherwise.
    """
    text = message.get("text", "")
    msg_type = message.get("type", "user_chat_input")
    session_id = message.get("session_id", "")

    # ── session_reset — nothing to do (new session key handles it) ──
    if msg_type == "session_reset":
        logger.info("Session reset for %s (session=%s) — new user key handles clean state", robot_uuid, session_id)
        return True

    # ── Build the user key for session routing ──
    user_key = f"mpx:{robot_uuid}:{session_id}" if session_id else f"mpx:{robot_uuid}"

    # ── Build the chat completion request ──
    payload = {
        "model": "openclaw/default",
        "user": user_key,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "max_tokens": 1024,
    }

    # Handle multi-turn context: the Gateway auto-manages history,
    # so we only send the current message. Previous turns are injected
    # by the Gateway from the session state.

    req = urllib.request.Request(
        GATEWAY_CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GATEWAY_TOKEN}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=CHAT_TIMEOUT) as r:
            resp_data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        logger.warning(
            "Gateway HTTP %d for %s/%s: %s",
            e.code, robot_uuid, msg_id[:8], body,
        )
        return False
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        logger.warning("Gateway error for %s/%s: %s", robot_uuid, msg_id[:8], e)
        return False

    # ── Extract the assistant's reply ──
    try:
        choices = resp_data.get("choices", [])
        if not choices:
            logger.warning("Gateway returned empty choices for %s/%s", robot_uuid, msg_id[:8])
            return False

        content = choices[0].get("message", {}).get("content", "")
        if not content:
            logger.warning("Gateway returned empty content for %s/%s", robot_uuid, msg_id[:8])
            return False

        # Strip markdown code fences (```json ... ```) if present
        stripped = content.strip()
        if stripped.startswith("```"):
            # Remove opening fence (```json, ```, etc.)
            first_newline = stripped.find("\n")
            if first_newline != -1:
                stripped = stripped[first_newline + 1:]
            # Remove closing fence
            if stripped.endswith("```"):
                stripped = stripped[:-3].strip()
                if stripped.endswith("```"):
                    stripped = stripped[:-3].strip()

        # Try to parse as JSON (expected format: {"text": "...", "commands": [...]})
        try:
            reply_json = json.loads(stripped)
        except json.JSONDecodeError:
            # Not valid JSON — wrap as plain text
            reply_json = {
                "type": "chat_reply",
                "text": content,
                "commands": [],
                "session_id": session_id,
                "ts": int(time.time()),
            }

        # Ensure required fields
        if "type" not in reply_json:
            reply_json["type"] = "chat_reply"
        if "text" not in reply_json:
            reply_json["text"] = content
        if "commands" not in reply_json:
            reply_json["commands"] = []
        if "session_id" not in reply_json:
            reply_json["session_id"] = session_id
        reply_json["ts"] = int(time.time())

    except Exception as e:
        logger.warning("Error parsing Gateway reply for %s/%s: %s", robot_uuid, msg_id[:8], e)
        return False

    # ── Submit the reply to host-listener ──
    reply_payload = json.dumps(reply_json).encode("utf-8")
    reply_url = f"{HOST_LISTENER_URL}/v1/messages/{msg_id}/reply"

    # Also queue a reply for the robot by calling the process endpoint
    # with robot_uuid, so the core_gateway can pick it up
    req_reply = urllib.request.Request(
        reply_url,
        data=reply_payload,
        headers={
            "Content-Type": "application/json",
            "X-Robot-UUID": robot_uuid,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req_reply, timeout=10) as r:
            resp = json.loads(r.read().decode("utf-8"))
            logger.info(
                "Reply submitted for %s (msg_id=%s): %.80s",
                robot_uuid, msg_id[:8],
                reply_json.get("text", "")[:80],
            )
            return True
    except urllib.error.HTTPError as e:
        logger.warning(
            "Reply POST failed HTTP %d for %s/%s: %s",
            e.code, robot_uuid, msg_id[:8],
            e.read().decode()[:200],
        )
        return False
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        logger.warning("Reply POST error for %s/%s: %s", robot_uuid, msg_id[:8], e)
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    logger.info(
        "robot-responder started — polling %s every %.1fs → %s",
        HOST_LISTENER_URL,
        POLL_INTERVAL,
        GATEWAY_CHAT_URL,
    )
    logger.info("Gateway auth: %s", "configured" if GATEWAY_TOKEN else "MISSING — set GATEWAY_TOKEN or GATEWAY_HOOK_TOKEN env, or gateway.auth.token in config")

    while not _shutdown:
        try:
            pending = fetch_pending()

            for msg in pending:
                msg_id = msg.get("msg_id", "")
                if msg_id in _seen:
                    continue
                _seen.add(msg_id)

                robot_uuid = msg.get("robot_uuid", "unknown").rstrip("\x00")
                message = msg.get("message", {})
                text = message.get("text", "")

                logger.info(
                    "New message from %s: %.100s",
                    robot_uuid,
                    text[:100],
                )

                process_message(message, robot_uuid, msg_id)

        except Exception:
            logger.exception("Unexpected error in main loop")

        if not _shutdown:
            time.sleep(POLL_INTERVAL)

    logger.info("robot-responder stopped")


if __name__ == "__main__":
    main()
