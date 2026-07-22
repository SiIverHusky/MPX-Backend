from __future__ import annotations

import logging
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from protocol.packets import HEADER_SIZE, TAG_SIZE

# ── MPXE WASM encryption constants ──────────────────────────────────────

MPXE_MAGIC = b"MPXE"
MPXE_VERSION = 0x01

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


async def load_keys_from_db(pool) -> int:
    """Load robot AES keys from the database into the in-memory store.

    Queries the ``robots`` table for all rows and registers each
    UUID→key mapping.  Returns the number of keys loaded.

    This is called once during gateway startup (see ``main.py`` lifespan).
    """
    store = _get_key_store()
    count = 0
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT robot_uuid, aes_key_hex FROM robots",
            )
        for row in rows:
            uuid_bytes = _normalize_uuid(row["robot_uuid"].encode("utf-8"))
            key = bytes.fromhex(row["aes_key_hex"])
            if len(key) != 32:
                logger.warning(
                    "Skipping robot %s: aes_key_hex has %d bytes, expected 32",
                    row["robot_uuid"], len(key),
                )
                continue
            store[uuid_bytes] = key
            count += 1
        logger.info("Loaded %d robot key(s) from database", count)
    except Exception as exc:
        logger.warning("Failed to load keys from database: %s", exc)
    return count


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

def decrypt_frame(frame: bytes) -> tuple[bytes, bytes, bytes] | None:
    """Decrypt a binary frame from the robot.

    Args:
        frame: Complete binary frame (header + ciphertext + tag).

    Returns:
        Tuple of ``(robot_uuid, iv, plaintext_json)`` on success,
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
        return robot_uuid, iv, plaintext
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
    """Build an encrypted binary frame with a **fresh random IV**.

    Suitable for keep-alive frames and other one-shot messages not
    tied to a specific request–response pair.

    Args:
        robot_uuid: 16-byte robot UUID (also used as AAD).
        key:        32-byte AES-256 key.
        json_payload: UTF-8 JSON response to send.

    Returns:
        Complete binary frame ready to send over WebSocket.

    Frame layout:
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


def build_downstream_frame_with_iv(
    robot_uuid: bytes,
    key: bytes,
    iv: bytes,
    json_payload: bytes,
) -> bytes:
    """Build an encrypted downstream frame **reusing the given IV**.

    Per CLOUD_INGRESS.md §4.1 the reply must reuse the same IV that
    came with the upstream frame.  This is safe because each
    request–response pair is unique and the IV was generated by the
    robot's hardware TRNG.

    Args:
        robot_uuid: 16-byte robot UUID (also used as AAD).
        key:        32-byte AES-256 key.
        iv:         12-byte nonce from the upstream frame (reused).
        json_payload: UTF-8 JSON response to send.

    Returns:
        Complete binary frame ready to send over WebSocket.
    """
    aesgcm = AESGCM(key)
    ct_and_tag = aesgcm.encrypt(iv, json_payload, robot_uuid)

    ct = ct_and_tag[:-16]
    tag = ct_and_tag[-16:]

    return robot_uuid + iv + ct + tag


# ---------------------------------------------------------------------------
# MPXE WASM skill encryption (design doc: docs/wasm-encryption-design.md)
# ---------------------------------------------------------------------------

def encrypt_wasm_for_robot(
    robot_uuid: str,
    robot_key: bytes,
    skill_id: str,
    wasm_bytes: bytes,
) -> bytes:
    """Encrypt a WASM binary for a specific robot using two-layer key wrapping.

    Key hierarchy:
      robot_root_key ──(GCM-wrap)──→ per_skill_key ──(GCM-encrypt)──→ WASM

    The per-skill key is generated fresh, used once, and discarded.

    Returns a single MPXE-format blob ready for LittleFS
    (docs/wasm-encryption-design.md §4.1).
    """
    import hashlib

    # 1. Generate random per-skill key
    skill_key = os.urandom(32)

    # 2. Wrap skill key with robot's root key
    wrap_iv = os.urandom(12)
    aesgcm = AESGCM(robot_key)
    wrap_aad = f"wasm-wrap:v1:{robot_uuid}".encode("utf-8")
    wrapped_key_ct = aesgcm.encrypt(wrap_iv, skill_key, wrap_aad)

    # 3. Hash skill_id for fixed-width AAD
    skill_id_hash = hashlib.sha256(skill_id.encode("utf-8")).digest()[:8]

    # 4. Encrypt WASM with skill key
    wasm_iv = os.urandom(12)
    aesgcm_wasm = AESGCM(skill_key)
    wasm_aad = f"wasm-skill:v1:{skill_id_hash.hex()}".encode("utf-8")
    wasm_ct_and_tag = aesgcm_wasm.encrypt(wasm_iv, wasm_bytes, wasm_aad)

    # 5. Build single MPXE blob
    blob = bytearray()
    blob.extend(MPXE_MAGIC)           # [4]  magic
    blob.append(MPXE_VERSION)         # [1]  version
    blob.extend(wrap_iv)              # [12] wrap_iv
    blob.extend(wrapped_key_ct)       # [48] wrapped key (32 ct + 16 tag)
    blob.append(0x00)                 # [1]  key_algo (reserved)
    blob.extend(skill_id_hash)        # [8]  skill_id hash
    blob.extend(wasm_iv)              # [12] wasm_iv
    blob.extend(wasm_ct_and_tag)      # [N+16] ciphertext + tag

    return bytes(blob)
