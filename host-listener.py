#!/usr/bin/env python3
"""
host-listener — Bridge between openclaw-bridge (Docker) and the agent (me).

Receives POST /v1/chat/process from the bridge container and makes
messages available for the agent to process.  Stores replies for
retrieval by the listener so the bridge gets a complete response.

Data flow:
  1. Bridge POSTs → host-listener /v1/chat/process
  2. Listener stores message under data/pending/<msg_id>.json
  3. Agent polls GET /v1/messages/pending to fetch pending messages
  4. Agent POSTs reply to /v1/messages/<msg_id>/reply
  5. Listener returns the reply to the bridge's polling request

Usage:
  python3 host-listener.py [--port 19090] [--data-dir ./data]
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s host-listener %(message)s",
)
logger = logging.getLogger("host-listener")

DEFAULT_PORT = 19090

# Mutable box for the data directory — allows CLI arg override
_data_dir: list[Path] = [Path(os.getenv("HOST_LISTENER_DATA_DIR", "/tmp/mpx-bridge-data"))]


def _dd() -> Path:
    return _data_dir[0]


def _ensure_dirs():
    dd = _dd()
    dd.mkdir(parents=True, exist_ok=True)
    (dd / "pending").mkdir(exist_ok=True)
    (dd / "replies").mkdir(exist_ok=True)


def _store_pending(robot_uuid: str, message: dict) -> str:
    msg_id = str(uuid.uuid4())
    payload = {
        "msg_id": msg_id,
        "robot_uuid": robot_uuid,
        "message": message,
        "ts": int(time.time()),
    }
    (_dd() / "pending" / f"{msg_id}.json").write_text(
        json.dumps(payload, indent=2)
    )
    logger.info("Stored pending %s from %s", msg_id, robot_uuid)
    return msg_id


def _list_pending() -> list[dict]:
    pending_dir = _dd() / "pending"
    if not pending_dir.exists():
        return []
    results = []
    for f in sorted(pending_dir.iterdir()):
        if f.suffix == ".json":
            results.append(json.loads(f.read_text()))
    return results


def _store_reply(msg_id: str, reply_json: str) -> bool:
    try:
        json.loads(reply_json)
    except json.JSONDecodeError as e:
        logger.warning("Invalid reply JSON for %s: %s", msg_id, e)
        return False

    reply_file = _dd() / "replies" / f"{msg_id}.json"
    reply_file.write_text(reply_json)
    logger.info("Stored reply for %s", msg_id)

    pending_file = _dd() / "pending" / f"{msg_id}.json"
    if pending_file.exists():
        pending_file.unlink()

    return True


def _get_reply(msg_id: str) -> str | None:
    reply_file = _dd() / "replies" / f"{msg_id}.json"
    if reply_file.exists():
        return reply_file.read_text()
    return None


class Handler(BaseHTTPRequestHandler):
    """Simple HTTP handler for the bridge protocol."""

    def _send_json(self, status: int, body):
        if isinstance(body, str):
            body_bytes = body.encode("utf-8")
        else:
            body_bytes = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_error(self, status: int, message: str):
        self._send_json(status, {"error": message})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len else b"{}"

        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_error(400, "Invalid JSON")
            return

        if path == "/v1/chat/process":
            robot_uuid = data.get("robot_uuid", "unknown")
            message = data.get("message", {})
            msg_id = _store_pending(robot_uuid, message)

            # Block and wait for the reply (up to 25s)
            reply = self._wait_for_reply(msg_id, timeout=25)

            if reply is not None:
                self._send_json(200, reply)
            else:
                fallback = {
                    "type": "chat_reply",
                    "text": "🤖 Processing your message...",
                    "ts": int(time.time()),
                    "actions": [{"gait": "none", "param": 0}],
                    "commands": [],
                }
                self._send_json(200, fallback)

        elif path.startswith("/v1/messages/") and path.endswith("/reply"):
            parts = [p for p in path.split("/") if p]
            # parts = ["v1", "messages", "<msg_id>", "reply"]
            if len(parts) >= 4:
                msg_id = parts[2]
                reply_json_str = json.dumps(data)
                if _store_reply(msg_id, reply_json_str):
                    self._send_json(200, {"status": "ok", "msg_id": msg_id})
                else:
                    self._send_error(400, "Invalid reply JSON")
            else:
                self._send_error(400, "Invalid path")

        else:
            self._send_error(404, "Not found")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/v1/messages/pending":
            pending = _list_pending()
            self._send_json(200, pending)

        elif path == "/healthz":
            self._send_json(200, {
                "status": "ok",
                "service": "host-listener",
                "pending": len(_list_pending()),
            })

        else:
            self._send_error(404, "Not found")

    def _wait_for_reply(self, msg_id: str, timeout: float = 25) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = _get_reply(msg_id)
            if raw is not None:
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    pass
            time.sleep(1)
        return None

    def log_message(self, format, *args):
        logger.info("%s %s", self.command, self.path)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Host-side listener for MPX bridge")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", type=str, default=str(_dd()))
    args = parser.parse_args()

    _data_dir[0] = Path(args.data_dir)
    _ensure_dirs()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    logger.info(
        "Listening on 0.0.0.0:%d  data_dir=%s",
        args.port,
        _dd(),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
