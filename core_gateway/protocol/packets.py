from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, Field


# =========================================================================
# Frame layout constants (mirrors Comm.md §2)
# =========================================================================
HEADER_SIZE = 28      # 16 uuid + 12 iv
TAG_SIZE = 16         # GCM authentication tag
AES_KEY_SIZE = 32     # 256-bit
AES_IV_SIZE = 12      # 96-bit nonce


def frame_size(plaintext_len: int) -> int:
    """Calculate total binary frame size for a given plaintext length."""
    return HEADER_SIZE + plaintext_len + TAG_SIZE


# =========================================================================
# Upstream (Robot → Server) — JSON inside ciphertext
# =========================================================================

class UserChatInput(BaseModel):
    """User chat message forwarded by the robot over the encrypted link.

    Matches the upstream payload schema in chat-ingress-spec.md §3.2.
    """

    type: Literal["user_chat_input"] = "user_chat_input"
    text: str = Field(min_length=1, max_length=4096)
    session_id: str = Field(default="", min_length=0, max_length=64)
    ts: int = 0


class SessionReset(BaseModel):
    """Sent by the PWA when the user starts a 'New Conversation'.

    The ingress must forward this to OpenClaw to discard conversation
    context for this (robot_uuid, session_id) pair.
    """

    type: Literal["session_reset"] = "session_reset"
    session_id: str = Field(default="", min_length=0, max_length=64)
    ts: int = 0


UpstreamMessage = Union[UserChatInput, SessionReset]


# =========================================================================
# Downstream (Server → Robot) — JSON inside ciphertext
# =========================================================================

class LuaCommand(BaseModel):
    """A single Lua command for the robot to execute sequentially.

    chat-ingress-spec.md §4.4. Each script gets a 5-second timeout on
    the robot.
    """

    type: Literal["lua"] = "lua"
    script: str = Field(..., min_length=1, max_length=4096)


Command = Union[LuaCommand]


class StepMessage(BaseModel):
    """Intermediate progress step from OpenClaw during a multi-stage task.

    chat-ingress-spec.md §4.2. Sent before the final chat_reply when
    the task has multiple stages.
    """

    type: Literal["step"] = "step"
    text: str = Field(..., max_length=4096)
    seq: int = Field(..., ge=1)
    total: int = Field(..., ge=1)
    session_id: str = Field(default="", max_length=64)
    ts: int = 0


class ChatReply(BaseModel):
    """Chat response sent back to the robot.

    Matches the downstream payload schema in chat-ingress-spec.md §4.3.
    The ``actions`` field is REMOVED in v2.0 — all robot interaction uses
    Lua scripts in the ``commands`` array.
    """

    type: Literal["chat_reply"] = "chat_reply"
    text: str = Field(default="", max_length=4096)
    commands: list[Command] = Field(default_factory=list)
    session_id: str = Field(default="", max_length=64)
    ts: int = 0


DownstreamMessage = Union[StepMessage, ChatReply]
