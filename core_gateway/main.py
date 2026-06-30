from __future__ import annotations

import json
import logging
import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from crypto import build_downstream_frame, build_downstream_frame_with_iv, decrypt_frame, lookup_key_by_uuid
from config import settings
from openclaw import openclaw_process, shutdown_client
from protocol.packets import ChatReply, GaitAction

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("core_gateway")

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan."""
    logger.info("Starting MPX Chat Ingress Gateway (protocol v1.0)")
    yield
    logger.info("Shutting down core gateway...")
    await shutdown_client()


app = FastAPI(title="MPX Chat Ingress Gateway", version="2.0.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "core_gateway",
        "version": "2.0.0",
    })


# ===========================================================================
# WebSocket — Encrypted Binary Frame Ingress
# ===========================================================================
# Shared WebSocket handler — both / and /v1/chat/ingress point here
# because the ESP32 firmware connects to the root path by default.
# ===========================================================================


async def _handle_ingress(socket: WebSocket) -> None:
    """Encrypted binary-frame chat ingress for MPX robots.

    Protocol (Comm.md §2–§6):
      - Each binary frame: [16B uuid][12B IV][N ciphertext][16B auth_tag]
      - Payload is AES-256-GCM encrypted with robot_uuid as AAD
      - Upstream JSON: {"type":"user_chat_input","text":"..."}
      - Downstream JSON: {"type":"chat_reply","text":"...","actions":[...]}

    The ESP32 WebSocket client starts ``recv()`` immediately on connect
    with a short timeout.  To prevent the connection from dying during
    idle periods (no user input), a background keep-alive task sends
    lightweight encrypted frames every 3 seconds.
    """
    await socket.accept()

    robot_uuid: bytes | None = None
    robot_uuid_str: str | None = None
    _keepalive_task: asyncio.Task | None = None

    try:
        # ── Wait for the first frame to discover the robot's UUID ──
        async for message in socket.iter_bytes():
            result = decrypt_frame(message)
            if result is None:
                logger.warning("Auth failure — dropping connection")
                await socket.close(code=1008, reason="Auth failure")
                return

            robot_uuid, iv, plaintext = result
            robot_uuid_str = robot_uuid.decode("utf-8", errors="replace")

            key = lookup_key_by_uuid(robot_uuid)
            if key is None:
                logger.warning("No key for %s — dropping", robot_uuid_str)
                await socket.close(code=1008, reason="Unknown robot")
                return

            logger.info("Robot %s connected", robot_uuid_str)

            # ── Check for queued reply from previous session ─────
            queued_reply = await _check_pending_reply(robot_uuid_str, key, robot_uuid, socket)
            if queued_reply is not None:
                # Queued reply was sent to the robot — proceed normally
                logger.info(
                    "Delivered queued reply to %s, awaiting next message",
                    robot_uuid_str,
                )

            # ── Start keep-alive background task ─────────────────
            async def _keepalive():
                """Send an encrypted empty frame every 3 s to keep
                the ESP32's recv() from timing out."""
                ka_payload = ChatReply(
                    text="",
                    actions=[GaitAction(gait="none", param=0)],
                    commands=[],
                ).model_dump_json().encode("utf-8")
                while True:
                    await asyncio.sleep(settings.keepalive_interval)
                    try:
                        frame = build_downstream_frame(robot_uuid, key, ka_payload)
                        await socket.send_bytes(frame)
                    except Exception:
                        break

            _keepalive_task = asyncio.create_task(_keepalive())

            # ── Process the first message ────────────────────────
            await _process_chat_frame(socket, robot_uuid, robot_uuid_str, key, iv, plaintext)

            # ── Continue receiving subsequent frames ─────────────
            async for message in socket.iter_bytes():
                result = decrypt_frame(message)
                if result is None:
                    continue
                _, iv, plaintext = result
                await _process_chat_frame(socket, robot_uuid, robot_uuid_str, key, iv, plaintext)

            # If we exit the for-loop the connection was closed
            break

    except WebSocketDisconnect:
        logger.info("Robot %s disconnected", robot_uuid_str or "unknown")
    except Exception as exc:
        logger.exception("WebSocket error for %s", robot_uuid_str or "unknown")
        try:
            await socket.close(code=1011, reason="Internal error")
        except Exception:
            pass
    finally:
        if _keepalive_task is not None:
            _keepalive_task.cancel()


# Both paths handled by the same logic — the ESP32 firmware may
# connect to "/" (default) or "/v1/chat/ingress".

@app.websocket("/")
async def chat_ingress_root(socket: WebSocket) -> None:
    await _handle_ingress(socket)


@app.websocket("/v1/chat/ingress")
async def chat_ingress(socket: WebSocket) -> None:
    await _handle_ingress(socket)


async def _check_pending_reply(
    robot_uuid_str: str,
    key: bytes,
    robot_uuid: bytes,
    socket: WebSocket,
) -> dict | None:
    """Check the bridge for a queued reply from a previous session.

    Returns the reply dict if one was found and sent, None otherwise.
    """
    try:
        from config import openclaw_settings
        base = openclaw_settings.base_url.rstrip("/")
        url = f"{base}/v1/pending/{robot_uuid_str}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                reply_data = resp.json()
                if isinstance(reply_data, dict) and "type" in reply_data:
                    import os
                    fresh_iv = os.urandom(12)
                    reply_json = json.dumps(reply_data)
                    frame = build_downstream_frame_with_iv(
                        robot_uuid, key, fresh_iv, reply_json.encode(),
                    )
                    await socket.send_bytes(frame)
                    logger.info(
                        "Sent queued reply to %s: %.80s",
                        robot_uuid_str,
                        reply_data.get("text", "")[:80],
                    )
                    return reply_data
    except (httpx.RequestError, Exception) as exc:
        logger.debug(
            "Pending reply check for %s: %s", robot_uuid_str, exc,
        )
    return None


async def _process_chat_frame(
    socket: WebSocket,
    robot_uuid: bytes,
    robot_uuid_str: str,
    key: bytes,
    iv: bytes,
    plaintext: bytes,
) -> None:
    """Decrypt, route to OpenClaw, encrypt reply, and send."""
    try:
        data = json.loads(plaintext)
    except json.JSONDecodeError:
        logger.warning("Non-JSON plaintext from %s", robot_uuid_str)
        return

    msg_type = data.get("type")

    # ── session_reset: forward to OpenClaw, discard context ──────
    if msg_type == "session_reset":
        logger.info("Session reset for %s", robot_uuid_str)
        reply_json = await openclaw_process(data, robot_uuid_str)
        frame = build_downstream_frame_with_iv(robot_uuid, key, iv, reply_json.encode("utf-8"))
        await socket.send_bytes(frame)
        return

    # ── Only handle user_chat_input ──────────────────────────────
    if msg_type != "user_chat_input":
        logger.debug("Ignoring non-chat frame type=%s", msg_type)
        return

    user_text = data.get("text", "").strip()
    if not user_text:
        return

    logger.info("Chat from %s: %.120s", robot_uuid_str, user_text)

    # ── Call OpenClaw agent ──────────────────────────────────────
    reply_json = await openclaw_process(data, robot_uuid_str)

    # ── Encrypt and send reply (reusing upstream IV per spec) ────
    frame = build_downstream_frame_with_iv(robot_uuid, key, iv, reply_json.encode("utf-8"))
    await socket.send_bytes(frame)
    logger.info("Replied to %s: %.120s", robot_uuid_str, json.loads(reply_json).get("text", "")[:120])
