#!/usr/bin/env python3
"""
robot-responder — Bridges the MPX message queue to the OpenClaw agent.

This replaces the canned-response auto-responder.py. When a robot message
arrives in the host-listener, this daemon:

  1. Polls the host-listener for pending messages
  2. For each new message (tracked by msg_id), POSTs to the OpenClaw
     Gateway's /hooks/agent endpoint, passing the message details
  3. The isolated agent turn processes the message and submits a reply
     via agent-poller.py (exec)

Architecture (portable):
  host-listener ← (poll) ← robot-responder → POST /hooks/agent → OpenClaw Gateway
                                                                      ↓
                                                             agent-poller.py reply
                                                                      ↓
                                                             host-listener reply store

Only robot-responder.py knows about OpenClaw. The rest of the pipeline
(ESP32 → core_gateway → openclaw-bridge → host-listener) stays generic.

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
GATEWAY_HOOK_URL = os.getenv(
    "GATEWAY_HOOK_URL", "http://127.0.0.1:18789/hooks/agent"
)
GATEWAY_HOOK_TOKEN = os.getenv(
    "GATEWAY_HOOK_TOKEN", ""
)
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.0"))
HOOK_REQUEST_TIMEOUT = float(os.getenv("HOOK_REQUEST_TIMEOUT", "30.0"))

# If not set via env, try to read from the OpenClaw config file
if not GATEWAY_HOOK_TOKEN:
    _config_paths = [
        os.path.expanduser("~/.openclaw/openclaw.json"),
    ]
    for _p in _config_paths:
        try:
            with open(_p) as _f:
                _cfg = json.load(_f)
            _hooks = _cfg.get("hooks", {})
            if _hooks.get("enabled") and _hooks.get("token"):
                GATEWAY_HOOK_TOKEN = _hooks["token"]
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


def trigger_agent(message: dict, robot_uuid: str, msg_id: str) -> bool:
    """POST a robot message to the OpenClaw Gateway's /hooks/agent endpoint.

    Returns True if the gateway accepted the request, False otherwise.
    """
    text = message.get("text", "")
    msg_type = message.get("type", "user_chat_input")
    session_id = message.get("session_id", "")

    prompt = (
        "A robot message arrived from **" + robot_uuid + "**:\n\n"
        "  " + text + "\n\n"
        "**msg_id:** `" + msg_id + "`\n"
        "**type:** " + msg_type + "\n"
        "**session_id:** `" + session_id + "`\n\n"
        "Submit the reply by running the following **single shell command**:\n"
        "```bash\n"
        "python3 /home/mangdang/mpx-server/agent-poller.py reply " + msg_id + " '<reply_json>'\n"
        "```\n\n"
        "**Reply format: use `commands` array with Lua scripts only (v2.0 protocol).**\n"
        "The old `actions` format is removed. All robot movement must use Lua.\n\n"
        "Single command example:\n"
        "```json\n"
        '{"text":"Walking forward!","commands":[{"type":"lua","script":"robot.gait(\'advance\')"}]}\n'
        "```\n\n"
        "Multi-step example (use `steps` array to report progress):\n"
        "```json\n"
        '{"text":"Walking 2s, turning, stopping","commands":[\n'
        '  {"type":"lua","script":"robot.gait(\'advance\')"},\n'
        '  {"type":"lua","script":"robot.delay_ms(2000)"},\n'
        '  {"type":"lua","script":"robot.gait(\'turnL\')"},\n'
        '  {"type":"lua","script":"robot.delay_ms(500)"},\n'
        '  {"type":"lua","script":"robot.gait(\'none\')"}\n'
        "]}\n"
        "```\n\n"
        "For multi-stage tasks, you can emit intermediate `step` messages before the final `chat_reply`.\n"
        "Each step must be a separate reply with `type: \"step\"`:\n"
        "```json\n"
        '{"type":"step","text":"Walking forward...","seq":1,"total":3,"session_id":"' + session_id + '"}\n'
        "```\n"
        "```json\n"
        '{"type":"step","text":"Turning...","seq":2,"total":3,"session_id":"' + session_id + '"}\n'
        "```\n"
        "Then the final reply:\n"
        "```json\n"
        '{"type":"chat_reply","text":"Done!","commands":[{"type":"lua","script":"robot.gait(\'none\')"}],"session_id":"' + session_id + '"}\n'
        "```\n\n"
        "**Available Lua commands** (full reference in /home/mangdang/mpx-server/lua-bindings.md):\n"
        "\n"
        "| Intent | Lua command |\n"
        "|--------|-------------|\n"
        "| Walk forward | `robot.gait('advance')` |\n"
        "| Stop / sit | `robot.gait('none')` |\n"
        "| Walk backward | `robot.gait('back')` |\n"
        "| Turn left | `robot.gait('turnL')` |\n"
        "| Turn right | `robot.gait('turnR')` |\n"
        "| Strafe left | `robot.gait('left')` |\n"
        "| Strafe right | `robot.gait('right')` |\n"
        "| Jump | `robot.gait('jump')` |\n"
        "| Jump forward | `robot.gait('jumpfwd')` |\n"
        "| Look up | `robot.gait('lookup')` |\n"
        "| Look down | `robot.gait('lookdown')` |\n"
        "| Look left | `robot.gait('lookleft')` |\n"
        "| Look right | `robot.gait('lookright')` |\n"
        "| Wag tail | `robot.gait('twerk')` |\n"
        "| Body roll | `robot.gait('roll')` |\n"
        "| Body pitch | `robot.gait('pitch')` |\n"
        "| Stretch | `robot.gait('stretch')` |\n"
        "| Dance | `robot.gait('bodycycle')` |\n"
        "| Raise body | `robot.gait('heightup')` |\n"
        "| Lower body | `robot.gait('heightdown')` |\n"
        "| Balance pose | `robot.gait('balance')` |\n"
        "| Bow back | `robot.gait('bowback')` |\n"
        "| Read IMU | `local i=robot.imu_read() print(i.ax,i.ay,i.az,i.gx,i.gy,i.gz)` |\n"
        "| Wait N ms | `robot.delay_ms(N)` |\n"
        "\n"
        "**Important rules (v2.0 protocol):**\n"
        "1. Use `commands` array with `lua` type scripts — the old `actions` format is REMOVED.\n"
        "2. Robot executes commands sequentially with a 5s timeout per script.\n"
        "3. Use `robot.delay_ms(N)` for timing between gait changes.\n"
        "4. Respond conversationally and naturally for a friendly robot dog.\n"
        "5. The reply JSON must be valid — use proper JSON escaping for Lua quotes.\n"
        "6. Always include `text` for the PWA display.\n"
        "7. Include `session_id` in every step and chat_reply message.\n"
        "8. For multi-stage tasks, emit step messages first, then chat_reply.\n"
    )

    payload = json.dumps({
        "message": prompt,
        "agentId": "main",
        "model": "deepseek/deepseek-v4-flash",
        "timeoutSeconds": 25,
    }).encode()

    req = urllib.request.Request(
        GATEWAY_HOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GATEWAY_HOOK_TOKEN}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=HOOK_REQUEST_TIMEOUT) as r:
            resp = json.loads(r.read())
            logger.info("Agent triggered for %s (msg_id=%s) — %s", robot_uuid, msg_id[:8], resp.get("status", "ok"))
            return True
    except urllib.error.HTTPError as e:
        logger.warning(
            "Gateway HTTP %d for %s/%s: %s",
            e.code, robot_uuid, msg_id[:8],
            e.read().decode()[:200],
        )
        return False
    except (urllib.error.URLError, OSError) as e:
        logger.warning("Gateway error for %s/%s: %s", robot_uuid, msg_id[:8], e)
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    logger.info(
        "robot-responder started — polling %s every %.1fs → %s",
        HOST_LISTENER_URL,
        POLL_INTERVAL,
        GATEWAY_HOOK_URL,
    )
    logger.info("Gateway hooks auth: %s", "configured" if GATEWAY_HOOK_TOKEN else "MISSING — set GATEWAY_HOOK_TOKEN")

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

                trigger_agent(message, robot_uuid, msg_id)

        except Exception:
            logger.exception("Unexpected error in main loop")

        if not _shutdown:
            time.sleep(POLL_INTERVAL)

    logger.info("robot-responder stopped")


if __name__ == "__main__":
    main()
