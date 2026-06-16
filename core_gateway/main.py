from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from protocol.packets import ConnectionAcceptancePacket, RobotTelemetryPacket

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
            packet = RobotTelemetryPacket.model_validate_json(raw_frame)
            logger.info(
                "validated robot packet robot_uuid=%s battery=%.2f pitch=%.2f roll=%.2f profile=%s",
                packet.robot_uuid,
                packet.battery_percentage,
                packet.pitch,
                packet.roll,
                packet.active_locomotion_profile,
            )
            acceptance = ConnectionAcceptancePacket(robot_uuid=packet.robot_uuid)
            await socket.send_text(acceptance.model_dump_json())
    except WebSocketDisconnect:
        logger.info("robot websocket disconnected")
    except Exception as exc:  # pragma: no cover - defensive socket guard
        logger.exception("robot websocket validation failure")
        await socket.close(code=1003, reason=str(exc))
