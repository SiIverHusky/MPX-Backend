#!/usr/bin/env python3
"""
auto-responder — Automatically polls for pending messages and submits
a quick reply to keep the robot connection alive. Run this as a background
process alongside the bridge stack.
"""

from __future__ import annotations

import json
import logging
import sys
import time
import urllib.request
import urllib.error

LISTENER_URL = "http://127.0.0.1:19090"
POLL_INTERVAL = 1.0  # seconds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s auto-responder %(message)s",
)
logger = logging.getLogger("auto-responder")

# Simple state: track which msg_ids we've seen
_seen: set[str] = set()


def fetch_pending() -> list[dict]:
    req = urllib.request.Request(f"{LISTENER_URL}/v1/messages/pending")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return []


def submit_reply(msg_id: str, text: str):
    reply = {
        "type": "chat_reply",
        "text": text,
        "actions": [{"gait": "wag", "param": 1}],
        "commands": [],
    }
    req = urllib.request.Request(
        f"{LISTENER_URL}/v1/messages/{msg_id}/reply",
        data=json.dumps(reply).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            result = json.loads(r.read())
            return result.get("status") == "ok"
    except Exception as e:
        logger.warning("Reply failed for %s: %s", msg_id[:8], e)
        return False


def auto_reply(text: str) -> str:
    """Generate an automatic reply based on the user's message."""
    text_lower = text.lower().strip()

    if any(w in text_lower for w in ["hey", "hi", "hello", "wazzup", "sup", "hiya"]):
        return "Hey! I am minipupper and I am online! Try asking me to walk."
    if "walk" in text_lower or "move" in text_lower or "forward" in text_lower:
        return "Walking forward! 🚶"
    if "back" in text_lower or "backward" in text_lower:
        return "Moving backward! 🔙"
    if "turn" in text_lower or "left" in text_lower or "right" in text_lower:
        return "Turning! 🔄"
    if "stop" in text_lower or "sit" in text_lower:
        return "Stopping. Sitting tight."
    if "wag" in text_lower or "tail" in text_lower:
        return "Wagging my tail! 🐕"
    if "how" in text_lower and "you" in text_lower:
        return "I am doing great! Ready to roam around."
    if "dance" in text_lower:
        return "Let me dance! 💃"
    if "status" in text_lower or "battery" in text_lower:
        return "All systems nominal. Battery good."
    return f"I heard you say: \"{text}\". How can I help?"


def main():
    logger.info("Auto-responder started (poll every %ds)", POLL_INTERVAL)
    while True:
        try:
            pending = fetch_pending()
            for msg in pending:
                msg_id = msg["msg_id"]
                if msg_id in _seen:
                    continue
                _seen.add(msg_id)
                text = msg.get("message", {}).get("text", "")
                robot = msg.get("robot_uuid", "unknown")
                reply_text = auto_reply(text)
                logger.info(
                    "Auto-replying to %s (%s): \"%s\" → \"%s\"",
                    msg_id[:8], robot, text[:50], reply_text,
                )
                if submit_reply(msg_id, reply_text):
                    logger.info("Reply sent OK for %s", msg_id[:8])
                else:
                    logger.warning("Reply FAILED for %s", msg_id[:8])
        except Exception as e:
            logger.error("Poll error: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
