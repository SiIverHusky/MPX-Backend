#!/usr/bin/env python3
"""
agent-poller — CLI tool for the agent to poll pending messages and submit replies.

Usage:
  # Check for pending messages
  python3 agent-poller.py check

  # Reply to a specific message (use commands format with Lua scripts)
  python3 agent-poller.py reply <msg_id> <reply_json>

  # Example: reply with Lua command
  python3 agent-poller.py reply <msg_id> '{"text":"Hello!","commands":[{"type":"lua","script":"robot.gait(\"twerk\")"}]}'

  # Full conversation flow: poll and let agent handle via stdin
  python3 agent-poller.py process <msg_id>

Lua command reference: lua-bindings.md
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error

BASE_URL = os.getenv("HOST_LISTENER_URL", "http://127.0.0.1:19090")


def check():
    """List all pending messages."""
    req = urllib.request.Request(f"{BASE_URL}/v1/messages/pending")
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        if not data:
            print("No pending messages.")
            return
        for msg in data:
            print(f"\n--- {msg['msg_id']} ---")
            print(f"  robot_uuid: {msg['robot_uuid']}")
            print(f"  ts:         {msg['ts']}")
            print(f"  message:    {json.dumps(msg['message'], indent=4)}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def reply(msg_id: str, reply_body: dict):
    """Submit a reply for a pending message.

    Automatically fetches the pending message to get the robot_uuid
    for queue support (in case the robot disconnected).
    """
    # Fetch the pending message to get robot_uuid
    robot_uuid = ""
    try:
        req = urllib.request.Request(f"{BASE_URL}/v1/messages/pending")
        with urllib.request.urlopen(req) as r:
            pending_list = json.loads(r.read())
        for m in pending_list:
            if m["msg_id"] == msg_id:
                robot_uuid = m.get("robot_uuid", "")
                # Strip null padding if present
                robot_uuid = robot_uuid.rstrip("\x00")
                break
    except Exception:
        pass

    # Build complete ChatReply JSON (v2.0 — no actions, Lua-only)
    import time
    msg_type = reply_body.get("type", "chat_reply")
    chat_reply = {
        "type": msg_type,
        "ts": int(time.time()),
        "commands": reply_body.get("commands", []),
        "text": reply_body.get("text", ""),
        "session_id": reply_body.get("session_id", ""),
    }
    if msg_type == "step":
        chat_reply["seq"] = reply_body.get("seq", 1)
        chat_reply["total"] = reply_body.get("total", 1)

    # Include robot_uuid so host-listener can queue the reply
    payload = json.dumps(chat_reply).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/v1/messages/{msg_id}/reply",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Robot-UUID": robot_uuid,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as r:
            resp = json.loads(r.read())
            if robot_uuid:
                print(f"Reply submitted (queued for {robot_uuid}): {json.dumps(resp)}")
            else:
                print(f"Reply submitted: {json.dumps(resp)}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def process(msg_id: str):
    """Interactive flow: print message, read reply from stdin, submit it."""
    # Fetch the pending message
    req = urllib.request.Request(f"{BASE_URL}/v1/messages/pending")
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
    except Exception as e:
        print(f"Error fetching pending: {e}", file=sys.stderr)
        sys.exit(1)

    msg = None
    for m in data:
        if m["msg_id"] == msg_id:
            msg = m
            break

    if msg is None:
        print(f"Message {msg_id} not found in pending.")
        sys.exit(1)

    print(f"Processing message {msg_id}:")
    print(f"  From robot: {msg['robot_uuid']}")
    print(f"  Text:       {msg['message'].get('text', '')}")
    print()
    print("Enter reply text (Ctrl+D to finish, or pipe from another command):")
    reply_text = sys.stdin.read().strip()

    if reply_text:
        reply_body = {"text": reply_text}
        reply(msg_id, reply_body)
    else:
        print("No reply text provided. Exiting.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "check":
        check()
    elif command == "reply":
        if len(sys.argv) < 4:
            print("Usage: agent-poller.py reply <msg_id> <json>")
            sys.exit(1)
        msg_id = sys.argv[2]
        reply_body = json.loads(sys.argv[3])
        reply(msg_id, reply_body)
    elif command == "process":
        if len(sys.argv) < 3:
            print("Usage: agent-poller.py process <msg_id>")
            sys.exit(1)
        process(sys.argv[2])
    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
