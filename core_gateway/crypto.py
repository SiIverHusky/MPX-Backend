from __future__ import annotations

import logging
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from protocol.packets import HEADER_SIZE, TAG_SIZE

logger = logging.getLogger("core_gateway.crypto")


# ---------------------------------------------------------------------------
# AES-256-GCM key store
# ---------------------------------------------------------------------------
# Maps robot_uuid (16 bytes) → 32-byte AES key.
#
# In development, a single dev key is loaded from the environment variable
# MPX_DEV_AES_KEY_HEX.  In production, keys are provisioned per-robot via
# the database (see lookup_key_by_uuid()).
#
# Frame format (Comm.md §2):
#   offset  size  field
#       0    16   robot_uuid  (plain-text routing header, also GCM AAD)
#      16    12   iv_nonce    (96-bit unique nonce)
#      28     N   ciphertext  (AES-256-GCM encrypted payload)
#     28+N   16   auth_tag    (128-bit GCM authentication tag)

def _normalize_uuid(uuid_bytes: bytes) -> bytes:
    """Strip trailing null bytes and right-pad/truncate to exactly 16 bytes."""
    stripped = uuid_bytes.rstrip(b"\x00")
    if len(stripped) > 16:
        stripped = stripped[:16]
    return stripped.ljust(16, b"\x00")


def _load_dev_key() -> dict[bytes, bytes]:
    """Load the development key from environment, if configured.

    Env vars:
      MPX_DEV_AES_KEY_HEX  — 64-char hex string (32-byte AES-256 key)
      MPX_DEV_ROBOT_UUID   — robot identifier (will be null-padded to 16 bytes)
                             Default: MPX-DOG-01
    """
    hex_key = os.getenv("MPX_DEV_AES_KEY_HEX", "")
    if not hex_key:
        return {}
    key = bytes.fromhex(hex_key)
    if len(key) != 32:
        logger.warning("MPX_DEV_AES_KEY_HEX has length %d, expected 32 bytes", len(key))
        return {}
    uuid_str = os.getenv("MPX_DEV_ROBOT_UUID", "MPX-DOG-01")
    dev_uuid = _normalize_uuid(uuid_str.encode("utf-8"))
    return {dev_uuid: key}


# In-memory key cache — populated on first use
_KEY_STORE: dict[bytes, bytes] | None = None


def _get_key_store() -> dict[bytes, bytes]:
    global _KEY_STORE
    if _KEY_STORE is None:
        _KEY_STORE = {}
        _KEY_STORE.update(_load_dev_key())
        logger.info("Key store initialised with %d key(s)", len(_KEY_STORE))
    return _KEY_STORE


def register_key(robot_uuid: bytes, key: bytes) -> None:
    """Register a robot's AES-256 key in the in-memory store.

    In production, call this after fetching the key from the database.
    """
    if len(robot_uuid) != 16:
        raise ValueError(f"robot_uuid must be exactly 16 bytes, got {len(robot_uuid)}")
    if len(key) != 32:
        raise ValueError(f"key must be exactly 32 bytes (256-bit), got {len(key)}")
    _get_key_store()[robot_uuid] = key
    logger.info("Registered key for robot %.16s...", robot_uuid.hex())


def lookup_key_by_uuid(robot_uuid: bytes) -> bytes | None:
    """Look up a robot's AES-256 key by its UUID (16 bytes).

    Returns the 32-byte key, or ``None`` if unknown.

    The ESP32 firmware writes the UUID string into a fixed 16-byte field
    and null-pads the remainder (e.g. ``b"MPX-DOG-01\\x00\\x00\\x00\\x00\\x00\\x00"``).
    We also try a null-stripped variant as a fallback.
    """
    store = _get_key_store()
    key = store.get(robot_uuid)
    if key is not None:
        return key
    # Fallback: strip null padding and re-lookup
    normalized = _normalize_uuid(robot_uuid)
    if normalized != robot_uuid:
        key = store.get(normalized)
    return key


# ---------------------------------------------------------------------------
# Decryption
# ---------------------------------------------------------------------------

def decrypt_frame(frame: bytes) -> tuple[bytes, bytes] | None:
    """Decrypt a binary frame from the robot.

    Args:
        frame: Complete binary frame (header + ciphertext + tag).

    Returns:
        Tuple of ``(robot_uuid, plaintext_json)`` on success,
        ``None`` on authentication failure or unknown UUID.

    Frame layout (Comm.md §4.2):
        [0:16]   robot_uuid   — also used as GCM AAD
        [16:28]  iv           — 12-byte nonce
        [28:-16] ciphertext   — encrypted payload
        [-16:]   auth_tag     — GCM authentication tag
    """
    if len(frame) < HEADER_SIZE + TAG_SIZE:
        logger.warning("Frame too short: %d bytes (min %d)", len(frame), HEADER_SIZE + TAG_SIZE)
        return None

    robot_uuid = frame[:16]
    iv = frame[16:28]
    ct_len = len(frame) - HEADER_SIZE - TAG_SIZE
    ct = frame[28:28 + ct_len]
    tag = frame[28 + ct_len:]

    key = lookup_key_by_uuid(robot_uuid)
    if key is None:
        logger.warning("Unknown robot UUID: %s", robot_uuid.hex())
        return None

    try:
        aesgcm = AESGCM(key)
        # cryptography expects ciphertext || tag concatenated
        plaintext = aesgcm.decrypt(iv, ct + tag, robot_uuid)
        return robot_uuid, plaintext
    except Exception as exc:
        logger.warning("Decryption FAILED (tampered frame): %s", exc)
        return None


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------

def build_downstream_frame(
    robot_uuid: bytes,
    key: bytes,
    json_payload: bytes,
) -> bytes:
    """Build an encrypted binary frame for the downstream direction.

    Args:
        robot_uuid: 16-byte robot UUID (also used as AAD).
        key:        32-byte AES-256 key.
        json_payload: UTF-8 JSON response to send.

    Returns:
        Complete binary frame ready to send over WebSocket.

    Frame layout (Comm.md §6.1):
        [0:16]   robot_uuid
        [16:28]  iv (fresh random nonce)
        [28:-16] ciphertext
        [-16:]   auth_tag
    """
    iv = os.urandom(12)  # fresh nonce per message

    aesgcm = AESGCM(key)
    ct_and_tag = aesgcm.encrypt(iv, json_payload, robot_uuid)

    ct = ct_and_tag[:-16]
    tag = ct_and_tag[-16:]

    return robot_uuid + iv + ct + tag
