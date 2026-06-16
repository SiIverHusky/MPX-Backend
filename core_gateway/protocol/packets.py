from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class RobotTelemetryPacket(BaseModel):
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


class ConnectionAcceptancePacket(BaseModel):
    status: Literal["validated"] = "validated"
    robot_uuid: UUID
    message: str = "robot connection cached"
