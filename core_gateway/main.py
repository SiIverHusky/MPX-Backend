from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from crypto import build_downstream_frame, decrypt_frame, lookup_key_by_uuid
from config import settings
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
    logger.info("Core gateway shut down")


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

@app.websocket("/v1/chat/ingress")
async def chat_ingress(socket: WebSocket) -> None:
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

            robot_uuid, plaintext = result
            robot_uuid_str = robot_uuid.decode("utf-8", errors="replace")

            key = lookup_key_by_uuid(robot_uuid)
            if key is None:
                logger.warning("No key for %s — dropping", robot_uuid_str)
                await socket.close(code=1008, reason="Unknown robot")
                return

            logger.info("Robot %s connected", robot_uuid_str)

            # ── Start keep-alive background task ─────────────────
            async def _keepalive():
                """Send an encrypted empty frame every 3 s to keep
                the ESP32's recv() from timing out."""
                ka_payload = ChatReply(
                    text="", actions=[GaitAction(gait="none", param=0)],
                ).model_dump_json().encode("utf-8")
                while True:
                    await asyncio.sleep(3)
                    try:
                        frame = build_downstream_frame(robot_uuid, key, ka_payload)
                        await socket.send_bytes(frame)
                    except Exception:
                        break

            _keepalive_task = asyncio.create_task(_keepalive())

            # ── Process the first message ────────────────────────
            await _process_chat_frame(socket, robot_uuid, robot_uuid_str, key, plaintext)

            # ── Continue receiving subsequent frames ─────────────
            async for message in socket.iter_bytes():
                result = decrypt_frame(message)
                if result is None:
                    continue
                _, plaintext = result
                await _process_chat_frame(socket, robot_uuid, robot_uuid_str, key, plaintext)

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


async def _process_chat_frame(
    socket: WebSocket,
    robot_uuid: bytes,
    robot_uuid_str: str,
    key: bytes,
    plaintext: bytes,
) -> None:
    """Decode, persist, echo, and encrypt-reply to one chat frame."""
    try:
        data = json.loads(plaintext)
    except json.JSONDecodeError:
        logger.warning("Non-JSON plaintext from %s", robot_uuid_str)
        return

    if data.get("type") != "user_chat_input":
        logger.debug("Ignoring non-chat frame type=%s", data.get("type"))
        return

    user_text = data.get("text", "").strip()
    if not user_text:
        return

    logger.info("Chat from %s: %.120s", robot_uuid_str, user_text)

    # ── Echo reply (cognitive agent not yet available) ──────────
    reply_text = f"Echo from server: you said '{user_text}'"

    downstream = ChatReply(
        text=reply_text,
        actions=[GaitAction(gait="none", param=0)],
    )
    downstream_bytes = downstream.model_dump_json().encode("utf-8")

    # ── Encrypt and send reply ──────────────────────────────────
    frame = build_downstream_frame(robot_uuid, key, downstream_bytes)
    await socket.send_bytes(frame)
    logger.info("Replied to %s: %.120s", robot_uuid_str, reply_text)
