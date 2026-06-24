from __future__ import annotations

from typing import Any, Literal

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

    Matches the upstream payload schema in Comm.md §5.1.
    """

    type: Literal["user_chat_input"] = "user_chat_input"
    text: str = Field(min_length=1, max_length=4096)


# =========================================================================
# Downstream (Server → Robot) — JSON inside ciphertext
# =========================================================================

class GaitAction(BaseModel):
    """A single gait command to execute on the robot.

    Matches Comm.md §5.3 gait action reference.
    """

    gait: str = Field(default="none", min_length=1, max_length=32)
    param: int = 0


class ChatReply(BaseModel):
    """Chat response sent back to the robot.

    Matches the downstream payload schema in Comm.md §5.2.
    """

    type: Literal["chat_reply"] = "chat_reply"
    text: str = Field(default="", max_length=4096)
    actions: list[GaitAction] = Field(default_factory=lambda: [GaitAction()])
