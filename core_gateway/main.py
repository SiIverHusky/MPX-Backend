from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from protocol.packets import (
    ConnectionAcceptancePacket,
    RobotTelemetryPacket,
    ServerEchoPacket,
    UserInputPacket,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("core_gateway")

app = FastAPI(title="MPX Core Gateway", version="1.0.0")


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "core_gateway"})


@app.websocket("/v1/robot/connect")
async def robot_connect(socket: WebSocket) -> None:
    await socket.accept()
    try:
        while True:
            raw_frame = await socket.receive_text()
            logger.info("incoming robot packet frame: %s", raw_frame)

            # --- 1. Always echo the raw frame back immediately (latency baseline) ---
            try:
                await socket.send_text(raw_frame)
            except Exception:
                logger.exception("failed to send echo back to websocket")

            # --- 2. Route the packet by its "type" field ---
            try:
                frame_data: dict[str, Any] = json.loads(raw_frame)
            except json.JSONDecodeError:
                logger.info("non-JSON frame (echoed but not routed)")
                continue

            packet_type = frame_data.get("type", "")

            if packet_type == "telemetry":
                try:
                    packet = RobotTelemetryPacket.model_validate(frame_data)
                    logger.info(
                        "telemetry robot_uuid=%s battery=%.2f pitch=%.2f roll=%.2f profile=%s",
                        packet.robot_uuid,
                        packet.battery_percentage,
                        packet.pitch,
                        packet.roll,
                        packet.active_locomotion_profile,
                    )
                    acceptance = ConnectionAcceptancePacket(robot_uuid=packet.robot_uuid)
                    await socket.send_text(acceptance.model_dump_json())
                except Exception as exc:
                    logger.warning("invalid telemetry frame: %s", exc)

            elif packet_type == "user_input":
                try:
                    packet = UserInputPacket.model_validate(frame_data)
                    logger.info(
                        "user_input chat_id=%s text=%s",
                        packet.chat_id,
                        packet.text,
                    )
                    echo = ServerEchoPacket(
                        original_type="user_input",
                        text=packet.text,
                    )
                    await socket.send_text(echo.model_dump_json())
                except Exception as exc:
                    logger.warning("invalid user_input frame: %s", exc)

            else:
                logger.info(
                    "unknown packet type '%s' (echoed raw but no structured response)",
                    packet_type,
                )

    except WebSocketDisconnect:
        logger.info("robot websocket disconnected")
    except Exception as exc:  # pragma: no cover - defensive socket guard
        logger.exception("robot websocket validation failure")
        # WebSocket control frames (close reason) must be <=125 bytes.
        # Truncate the reason we send to the client to avoid ProtocolError.
        reason = str(exc) or "error"
        if len(reason) > 120:
            reason = reason[:120] + "..."
        try:
            await socket.close(code=1003, reason=reason)
        except Exception:
            logger.exception("failed to close websocket cleanly")
