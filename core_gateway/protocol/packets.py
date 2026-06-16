from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class RobotTelemetryPacket(BaseModel):
    """Streamed from the robot every N seconds."""

    type: Literal["telemetry"] = "telemetry"
    robot_uuid: UUID
    battery_percentage: float = Field(ge=0.0, le=100.0)
    pitch: float
    roll: float
    active_locomotion_profile: str = Field(min_length=1, max_length=128)

    @field_validator("active_locomotion_profile")
    @classmethod
    def normalize_profile(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("active_locomotion_profile must not be empty")
        return normalized


class UserInputPacket(BaseModel):
    """Forwarded from a Telegram message by the ESP32 bridge."""

    type: Literal["user_input"] = "user_input"
    robot_uuid: UUID
    chat_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=4096)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ConnectionAcceptancePacket(BaseModel):
    """Sent by the gateway when a telemetry packet is validated."""

    type: Literal["validated"] = "validated"
    robot_uuid: UUID
    message: str = "robot connection cached"


class ServerEchoPacket(BaseModel):
    """Echo of a user input back to the device, for the cognitive agent pipeline."""

    type: Literal["echo"] = "echo"
    original_type: str
    text: str
