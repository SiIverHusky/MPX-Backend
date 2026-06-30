#!/usr/bin/env python3
"""
host-listener — Bridge between openclaw-bridge (Docker) and the agent (me).

Implements a **proper message queue system** with:

  • Thread-safe in-memory message store with O(1) lookups
  • Condition-based waiting (no busy-polling)
  • File-backed persistence for durability across restarts
  • Automatic message expiry and periodic cleanup
  • Per-robot reply queuing for disconnected robot scenarios

Data flow:
  1. Bridge POSTs → host-listener /v1/chat/process
  2. Listener stores message in the queue (memory + file)
  3. Agent polls GET /v1/messages/pending to fetch pending messages
  4. Agent POSTs reply to /v1/messages/<msg_id>/reply
  5. Listener stores reply, notifies the waiting request thread
  6. On reconnect, bridge polls GET /v1/replies/<robot_uuid>
  7. Listener returns queued reply, or 404 if none

Usage:
  python3 host-listener.py [--port 19090] [--data-dir ./data]
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s host-listener %(message)s",
)
logger = logging.getLogger("host-listener")

DEFAULT_PORT = 19090
MESSAGE_TTL_SEC = 300  # 5 minutes — messages older than this are evicted
CLEANUP_INTERVAL_SEC = 60  # run cleanup every 60s

# Mutable box for the data directory — allows CLI arg override
_data_dir: list[Path] = [Path(os.getenv("HOST_LISTENER_DATA_DIR", "/tmp/mpx-bridge-data"))]


def _dd() -> Path:
    return _data_dir[0]


# ---------------------------------------------------------------------------
# Message store — thread-safe in-memory queue with file persistence
# ---------------------------------------------------------------------------


@dataclass
class PendingMessage:
    """A message waiting for agent processing."""

    msg_id: str
    robot_uuid: str
    message: dict[str, Any]
    ts: int  # Unix timestamp when the message was received
    expires_at: float  # Unix timestamp after which the message is stale


class MessageStore:
    """Thread-safe message queue with efficient condition-based waiting.

    This is the core of the queue system. Instead of busy-polling with
    ``time.sleep(1)`` like the original implementation, waiters use a
    ``threading.Condition`` that is notified when a reply arrives.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._lock = threading.Lock()
        # Condition to notify waiters when a reply is stored.
        # Waiters call wait(timeout); the notifier calls notify_all().
        self._reply_available = threading.Condition(self._lock)

        # ── In-memory stores ────────────────────────────────────
        self._pending: dict[str, PendingMessage] = {}
        self._pending_by_robot: dict[str, list[str]] = {}  # robot_uuid → [msg_id, ...]
        self._replies: dict[str, dict[str, Any]] = {}  # msg_id → reply dict
        self._robot_replies: dict[str, dict[str, Any]] = {}  # robot_uuid → latest reply

        # ── Load existing files from disk into memory ───────────
        self._load_from_disk()

        # ── Start background cleanup ────────────────────────────
        self._stop_cleanup = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            name="msg-cleanup",
            daemon=True,
        )
        self._cleanup_thread.start()

    # -- Persistence helpers ------------------------------------------------

    def _ensure_dirs(self) -> None:
        self._data_dir.mkdir(parents=True, exist_ok=True)
        (self._data_dir / "pending").mkdir(exist_ok=True)
        (self._data_dir / "replies").mkdir(exist_ok=True)
        (self._data_dir / "replies" / "by_robot").mkdir(parents=True, exist_ok=True)

    def _pending_path(self, msg_id: str) -> Path:
        return self._data_dir / "pending" / f"{msg_id}.json"

    def _reply_path(self, msg_id: str) -> Path:
        return self._data_dir / "replies" / f"{msg_id}.json"

    def _robot_reply_path(self, robot_uuid: str) -> Path:
        return self._data_dir / "replies" / "by_robot" / f"{robot_uuid}.json"

    def _load_from_disk(self) -> None:
        """Scan data dirs and load any existing files into memory."""
        self._ensure_dirs()

        # Load pending messages
        pending_dir = self._data_dir / "pending"
        if pending_dir.exists():
            for f in sorted(pending_dir.iterdir()):
                if f.suffix != ".json":
                    continue
                try:
                    data = json.loads(f.read_text())
                    msg = PendingMessage(
                        msg_id=data["msg_id"],
                        robot_uuid=data["robot_uuid"],
                        message=data["message"],
                        ts=data["ts"],
                        expires_at=data.get(
                            "expires_at",
                            time.time() + MESSAGE_TTL_SEC,
                        ),
                    )
                    self._pending[msg.msg_id] = msg
                    self._pending_by_robot.setdefault(msg.robot_uuid, []).append(msg.msg_id)
                except (json.JSONDecodeError, KeyError, OSError) as e:
                    logger.warning("Failed to load pending file %s: %s", f.name, e)

        # Load replies
        replies_dir = self._data_dir / "replies"
        if replies_dir.exists():
            for f in sorted(replies_dir.iterdir()):
                if f.suffix != ".json" or f.name == "by_robot":
                    continue
                try:
                    self._replies[f.stem] = json.loads(f.read_text())
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Failed to load reply file %s: %s", f.name, e)

        # Load robot reply queue
        by_robot_dir = self._data_dir / "replies" / "by_robot"
        if by_robot_dir.exists():
            for f in sorted(by_robot_dir.iterdir()):
                if f.suffix != ".json":
                    continue
                try:
                    data = json.loads(f.read_text())
                    robot_uuid = data.get("robot_uuid") or f.stem
                    self._robot_replies[robot_uuid] = data.get("reply", data)
                except (json.JSONDecodeError, OSError) as e:
                    logger.warning("Failed to load robot reply %s: %s", f.name, e)

        if self._pending or self._replies or self._robot_replies:
            logger.info(
                "Loaded %d pending, %d replies, %d robot-queue entries from disk",
                len(self._pending),
                len(self._replies),
                len(self._robot_replies),
            )

    # -- Public API ---------------------------------------------------------

    def store_pending(self, robot_uuid: str, message: dict[str, Any]) -> str:
        """Store a pending message and return its ``msg_id``.

        Thread-safe.  Persists to disk immediately.
        """
        msg_id = str(uuid.uuid4())
        now = time.time()
        pm = PendingMessage(
            msg_id=msg_id,
            robot_uuid=robot_uuid,
            message=message,
            ts=int(now),
            expires_at=now + MESSAGE_TTL_SEC,
        )

        with self._lock:
            self._pending[msg_id] = pm
            self._pending_by_robot.setdefault(robot_uuid, []).append(msg_id)

        # Persist to disk (outside the lock to minimise contention)
        self._ensure_dirs()
        payload = {
            "msg_id": msg_id,
            "robot_uuid": robot_uuid,
            "message": message,
            "ts": pm.ts,
            "expires_at": pm.expires_at,
        }
        try:
            (self._data_dir / "pending" / f"{msg_id}.json").write_text(
                json.dumps(payload, indent=2)
            )
        except OSError as e:
            logger.error("Failed to persist pending %s: %s", msg_id, e)

        logger.info("Stored pending %s from %s", msg_id, robot_uuid)
        return msg_id

    def list_pending(self) -> list[dict[str, Any]]:
        """Return all non-expired pending messages (thread-safe)."""
        now = time.time()
        with self._lock:
            result = []
            expired_ids = []
            for msg_id, pm in self._pending.items():
                if pm.expires_at <= now:
                    expired_ids.append(msg_id)
                else:
                    result.append({
                        "msg_id": pm.msg_id,
                        "robot_uuid": pm.robot_uuid,
                        "message": pm.message,
                        "ts": pm.ts,
                    })
            # Clean up expired entries lazily
            for msg_id in expired_ids:
                self._remove_pending_nolock(msg_id)
        return result

    def get_pending(self, msg_id: str) -> dict[str, Any] | None:
        """Get a specific pending message by ID."""
        with self._lock:
            pm = self._pending.get(msg_id)
            if pm is None or pm.expires_at <= time.time():
                return None
            return {
                "msg_id": pm.msg_id,
                "robot_uuid": pm.robot_uuid,
                "message": pm.message,
                "ts": pm.ts,
            }

    def wait_for_reply(self, msg_id: str, timeout: float = 25) -> dict[str, Any] | None:
        """Wait for a reply to be submitted (efficient condition wait).

        Instead of busy-polling every 1 second like the original
        implementation, this uses ``threading.Condition.wait()`` which
        blocks the thread until notified or the timeout expires.

        Returns the reply dict, or ``None`` if the timeout is reached.
        """
        with self._reply_available:
            deadline = time.time() + timeout
            while msg_id not in self._replies:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._reply_available.wait(timeout=remaining)
            # Reply found — consume it atomically
            reply = self._replies.pop(msg_id)
            return reply

    def store_reply(self, msg_id: str, robot_uuid: str, reply_json: str) -> bool:
        """Store a reply and notify any waiting threads.

        Returns ``True`` if the reply was valid JSON, ``False`` otherwise.
        """
        try:
            parsed = json.loads(reply_json)
        except json.JSONDecodeError as e:
            logger.warning("Invalid reply JSON for %s: %s", msg_id, e)
            return False

        with self._lock:
            # Store in memory
            self._replies[msg_id] = parsed

            # Queue by robot_uuid for disconnected-robot scenario
            if robot_uuid:
                self._robot_replies[robot_uuid] = parsed

            # Remove from pending
            self._remove_pending_nolock(msg_id)

            # Notify all threads waiting on replies
            self._reply_available.notify_all()

        # Persist to disk (outside the lock)
        self._ensure_dirs()
        try:
            (self._data_dir / "replies" / f"{msg_id}.json").write_text(reply_json)
        except OSError as e:
            logger.error("Failed to persist reply %s: %s", msg_id, e)

        if robot_uuid:
            queue_entry = {
                "msg_id": msg_id,
                "robot_uuid": robot_uuid,
                "reply": parsed,
            }
            try:
                (self._data_dir / "replies" / "by_robot" / f"{robot_uuid}.json").write_text(
                    json.dumps(queue_entry, indent=2)
                )
            except OSError as e:
                logger.error("Failed to persist robot reply queue %s: %s", robot_uuid, e)

        logger.info("Stored reply for %s (robot=%s)", msg_id, robot_uuid or "?")
        return True

    def consume_robot_reply(self, robot_uuid: str) -> dict[str, Any] | None:
        """Get and remove the queued reply for a robot (atomic).

        Used when a robot reconnects and needs any pending replies
        that were generated while it was offline.
        """
        with self._lock:
            reply = self._robot_replies.pop(robot_uuid, None)

        if reply is not None:
            # Remove the disk file
            try:
                (self._data_dir / "replies" / "by_robot" / f"{robot_uuid}.json").unlink(
                    missing_ok=True
                )
            except OSError as e:
                logger.warning("Failed to remove robot reply file %s: %s", robot_uuid, e)
            logger.info("Consumed queued reply for %s", robot_uuid)

        return reply

    def get_robot_reply(self, robot_uuid: str) -> dict[str, Any] | None:
        """Peek at the queued reply for a robot without consuming it."""
        with self._lock:
            return self._robot_replies.get(robot_uuid)

    def pending_count(self) -> int:
        """Return the number of non-expired pending messages."""
        return len(self.list_pending())

    def shutdown(self) -> None:
        """Stop the background cleanup thread."""
        self._stop_cleanup.set()
        if self._cleanup_thread.is_alive():
            self._cleanup_thread.join(timeout=5)

    # -- Internal helpers ---------------------------------------------------

    def _remove_pending_nolock(self, msg_id: str) -> None:
        """Remove a pending message (caller MUST hold ``_lock``)."""
        pm = self._pending.pop(msg_id, None)
        if pm is not None:
            # Remove from robot index
            robot_list = self._pending_by_robot.get(pm.robot_uuid)
            if robot_list:
                try:
                    robot_list.remove(msg_id)
                except ValueError:
                    pass
                if not robot_list:
                    del self._pending_by_robot[pm.robot_uuid]
            # Remove disk file
            try:
                (self._data_dir / "pending" / f"{msg_id}.json").unlink(missing_ok=True)
            except OSError:
                pass

    def _cleanup_loop(self) -> None:
        """Background thread: periodically remove expired messages."""
        logger.info("Cleanup thread started (TTL=%ds, interval=%ds)", MESSAGE_TTL_SEC, CLEANUP_INTERVAL_SEC)
        while not self._stop_cleanup.wait(CLEANUP_INTERVAL_SEC):
            try:
                self._cleanup_expired()
            except Exception:
                logger.exception("Cleanup error")
        logger.info("Cleanup thread stopped")

    def _cleanup_expired(self) -> None:
        """Remove all expired pending messages and orphaned reply files."""
        now = time.time()
        removed = 0
        with self._lock:
            expired_ids = [
                msg_id for msg_id, pm in self._pending.items()
                if pm.expires_at <= now
            ]
            for msg_id in expired_ids:
                self._remove_pending_nolock(msg_id)
                removed += 1

        # Also clean up orphaned reply files (replies without corresponding pending)
        # that are older than the TTL
        orphan_cutoff = now - MESSAGE_TTL_SEC
        orphan_removed = 0
        try:
            replies_dir = self._data_dir / "replies"
            if replies_dir.exists():
                for f in replies_dir.iterdir():
                    if f.suffix != ".json" or f.name == "by_robot":
                        continue
                    msg_id = f.stem
                    with self._lock:
                        if msg_id not in self._replies and msg_id not in self._pending:
                            # Check file age
                            try:
                                mtime = f.stat().st_mtime
                                if mtime < orphan_cutoff:
                                    f.unlink()
                                    orphan_removed += 1
                            except OSError:
                                pass
        except OSError:
            pass

        if removed or orphan_removed:
            logger.info(
                "Cleanup: removed %d expired pending, %d orphaned reply files",
                removed,
                orphan_removed,
            )


# ---------------------------------------------------------------------------
# Global message store singleton
# ---------------------------------------------------------------------------
_store: MessageStore | None = None


def get_store() -> MessageStore:
    global _store
    if _store is None:
        _store = MessageStore(_dd())
    return _store


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """Simple HTTP handler for the bridge protocol."""

    # Silence per-request log lines (we use our own logger)
    def log_message(self, format, *args):
        logger.info("%s %s", self.command, self.path)

    def _send_json(self, status: int, body: Any):
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

        store = get_store()

        if path == "/v1/chat/process":
            robot_uuid = data.get("robot_uuid", "unknown")
            message = data.get("message", {})
            msg_id = store.store_pending(robot_uuid, message)

            # Block and wait for the reply (efficient condition wait)
            reply = store.wait_for_reply(msg_id, timeout=25)

            if reply is not None:
                self._send_json(200, reply)
            else:
                # Check if there's a robot-level queued reply (from a previous
                # disconnect scenario)
                queued = store.consume_robot_reply(robot_uuid)
                if queued is not None:
                    logger.info("Delivering queued reply for %s", robot_uuid)
                    self._send_json(200, queued)
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
            if len(parts) >= 4:
                msg_id = parts[2]
                robot_uuid = self.headers.get("X-Robot-UUID", "") or ""
                robot_uuid = robot_uuid.rstrip("\x00")
                reply_json_str = json.dumps(data)
                if store.store_reply(msg_id, robot_uuid, reply_json_str):
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

        store = get_store()

        if path == "/v1/messages/pending":
            pending = store.list_pending()
            self._send_json(200, pending)

        elif path.startswith("/v1/replies/"):
            # GET /v1/replies/<robot_uuid> — consume queued reply for this robot
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 3:
                robot_uuid = parts[2]
                reply = store.consume_robot_reply(robot_uuid)
                if reply is not None:
                    self._send_json(200, reply)
                else:
                    self._send_json(404, {
                        "status": "no_reply",
                        "robot_uuid": robot_uuid,
                    })
            else:
                self._send_error(400, "Invalid path")

        elif path == "/healthz":
            self._send_json(200, {
                "status": "ok",
                "service": "host-listener",
                "pending": store.pending_count(),
            })

        else:
            self._send_error(404, "Not found")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Host-side listener for MPX bridge")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", type=str, default=str(_dd()))
    args = parser.parse_args()

    _data_dir[0] = Path(args.data_dir)
    _store = get_store()  # Initialise the store (loads files + starts cleanup)

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    logger.info(
        "Listening on 0.0.0.0:%d  data_dir=%s  queue=enabled (TTL=%ds)",
        args.port,
        _dd(),
        MESSAGE_TTL_SEC,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down")
        _store.shutdown()
        server.shutdown()


if __name__ == "__main__":
    main()
