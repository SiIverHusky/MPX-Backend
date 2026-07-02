#!/usr/bin/env python3
"""
auto-responder — Rapid-response poller for robot messages.

Polls the host-listener every 1 second and immediately replies to
any pending messages, so the robot never waits long for a response.
"""

import json, logging, sys, time, urllib.request, urllib.error

LISTENER_URL = "http://127.0.0.1:19090"
POLL_INTERVAL = 1.0
SEEN = set()

logging.basicConfig(level=logging.INFO, format="%(asctime)s auto-responder %(message)s")
logger = logging.getLogger("auto-responder")

def fetch_pending():
    req = urllib.request.Request(f"{LISTENER_URL}/v1/messages/pending")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except: return []

def submit_reply(msg_id, robot_uuid, text, action="wag", param=1):
    reply = {
        "type": "chat_reply", "text": text,
        "actions": [{"gait": action, "param": param}], "commands": [],
    }
    req = urllib.request.Request(
        f"{LISTENER_URL}/v1/messages/{msg_id}/reply",
        data=json.dumps(reply).encode(),
        headers={"Content-Type": "application/json", "X-Robot-UUID": robot_uuid},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read()).get("status") == "ok"
    except: return False

def auto_reply(text):
    tl = text.lower().strip()
    if any(w in tl for w in ["hey","hi","hello","hiya","sup"]):
        return "Hey! I'm here. Ask me to walk or dance!", "wag", 1
    if "walk" in tl or "move" in tl or "forward" in tl:
        return "Walking forward!", "walk", 50
    if "back" in tl: return "Moving backward!", "back", 50
    if "turn" in tl or "left" in tl or "right" in tl: return "Turning!", "turn", 30
    if "stop" in tl or "sit" in tl: return "Stopping.", "none", 0
    if "wag" in tl or "tail" in tl: return "Wagging my tail!", "wag", 1
    if "dance" in tl: return "Let's dance!", "dance", 1
    return f"I heard: \"{text}\"", "wag", 1

def main():
    logger.info("Auto-responder started")
    while True:
        try:
            for m in fetch_pending():
                mid = m["msg_id"]
                if mid in SEEN: continue
                SEEN.add(mid)
                text = m["message"].get("text","")
                robot = m["robot_uuid"].rstrip("\x00")
                reply, action, param = auto_reply(text)
                if submit_reply(mid, robot, reply, action, param):
                    logger.info("Replied to %s: \"%s\"", mid[:8], reply[:50])
        except: pass
        time.sleep(POLL_INTERVAL)

main()
