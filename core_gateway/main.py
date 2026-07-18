from __future__ import annotations

import hashlib
import hmac
import json
import logging
import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import httpx
import jwt as pyjwt
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from google.cloud import storage as gcs_storage

from config import db_settings, storage_settings
from crypto import build_downstream_frame, build_downstream_frame_with_iv, decrypt_frame, lookup_key_by_uuid
from openclaw import openclaw_process, openclaw_process_stream, send_lua_output, shutdown_client

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("core_gateway")

# ---------------------------------------------------------------------------
# Global pool / client references
# ---------------------------------------------------------------------------
pg_pool: asyncpg.Pool | None = None
gcs_client: gcs_storage.Client | None = None


async def get_pg_pool() -> asyncpg.Pool:
    global pg_pool
    if pg_pool is None:
        pg_pool = await asyncpg.create_pool(
            host=db_settings.host,
            port=db_settings.port,
            user=db_settings.user,
            password=db_settings.password,
            database=db_settings.db_name,
            min_size=1,
            max_size=5,
        )
    return pg_pool


def get_gcs_client() -> gcs_storage.Client:
    global gcs_client
    if gcs_client is None:
        gcs_client = gcs_storage.Client(
            project="openclaw-dev",
            client_options={"api_endpoint": storage_settings.endpoint},
        )
    return gcs_client


# ---------------------------------------------------------------------------
# Helper: verify pbkdf2_sha256 hash
# ---------------------------------------------------------------------------
def make_pbkdf2_sha256(password: str) -> str:
    """Hash a password using pbkdf2_sha256 and return the stored hash format."""
    import os as _os
    algorithm = "pbkdf2_sha256"
    iterations = 100000
    salt = _os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations,
    )
    return f"{algorithm}${iterations}${salt}${digest.hex()}"


def verify_pbkdf2_sha256(password: str, stored_hash: str) -> bool:
    """Verify a password against a pbkdf2_sha256 hash string.

    Expected hash format::
        pbkdf2_sha256$iterations$salt$hex_digest
    """
    try:
        algorithm, iterations_str, salt, expected_hex = stored_hash.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    iterations = int(iterations_str)
    actual = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations,
    )
    return hmac.compare_digest(actual.hex(), expected_hex)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan."""
    logger.info("Starting MPX Chat Ingress Gateway (protocol v1.0)")
    yield
    logger.info("Shutting down core gateway...")
    global pg_pool, gcs_client
    if pg_pool is not None:
        await pg_pool.close()
        pg_pool = None
    if gcs_client is not None:
        gcs_client = None
    await shutdown_client()


app = FastAPI(title="MPX Chat Ingress Gateway", version="2.0.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "core_gateway",
        "version": "2.0.0",
    })


# ---------------------------------------------------------------------------
# REST — Auth & Publish for mpx-cli
# ---------------------------------------------------------------------------


@app.get("/v1/skills/check")
async def check_skill_slug(request: Request) -> JSONResponse:
    """Check if a slug is already taken by the authenticated developer.

    Query params:
        slug (str): The slug to check (e.g., "sale")

    Returns::
        {"exists": true, "skill_id": "haris_dev~sale"}
        or
        {"exists": false}
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"error": "missing or invalid Authorization header"})
    token = auth[7:]
    try:
        decoded = pyjwt.decode(token, db_settings.jwt_secret, algorithms=["HS256"])
    except pyjwt.InvalidTokenError:
        return JSONResponse(status_code=401, content={"error": "invalid token"})

    username = decoded.get("username", "")
    slug = request.query_params.get("slug", "")
    if not slug:
        return JSONResponse(status_code=422, content={"error": "slug query parameter required"})

    skill_id = f"{username}~{slug}"
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM marketplace_skills WHERE id = $1", skill_id,
        )

    if row:
        return JSONResponse(content={"exists": True, "skill_id": skill_id})
    return JSONResponse(content={"exists": False})


@app.post("/v1/auth/signup")
async def auth_signup(request: Request) -> JSONResponse:
    """Register a new developer account.

    Body::
        {"username": "my_dev", "password": "***"}

    Returns::
        {"status": "created", "username": "my_dev"}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    username = (body.get("username", "") or "").strip()
    password = body.get("password", "")

    if not username or not password:
        return JSONResponse(status_code=422, content={"error": "username and password required"})

    if len(username) < 3:
        return JSONResponse(status_code=422, content={"error": "username must be at least 3 characters"})
    if len(password) < 8:
        return JSONResponse(status_code=422, content={"error": "password must be at least 8 characters"})

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]+$", username):
        return JSONResponse(status_code=422, content={
            "error": "username must start with a letter and contain only letters, numbers, underscores, and hyphens",
        })

    password_hash = make_pbkdf2_sha256(password)

    pool = await get_pg_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (username, password_hash) VALUES ($1, $2)",
                username, password_hash,
            )
    except asyncpg.UniqueViolationError:
        return JSONResponse(status_code=409, content={"error": f"username '{username}' is already taken"})

    logger.info("New user registered: %s", username)
    return JSONResponse(content={"status": "created", "username": username}, status_code=201)


@app.post("/v1/auth/login")
async def auth_login(request: Request) -> JSONResponse:
    """Authenticate a developer and issue a JWT.

    Body::
        {"username": "haris_dev", "password": "password123"}

    Returns::
        {"token": "<JWT>", "expires_in": 86400}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    username = body.get("username", "")
    password = body.get("password", "")

    if not username or not password:
        return JSONResponse(status_code=422, content={"error": "username and password required"})

    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, password_hash FROM users WHERE username = $1", username,
        )

    if row is None:
        return JSONResponse(status_code=401, content={"error": "invalid credentials"})

    if not verify_pbkdf2_sha256(password, row["password_hash"]):
        return JSONResponse(status_code=401, content={"error": "invalid credentials"})

    now = datetime.now(tz=timezone.utc)
    payload = {
        "sub": str(row["id"]),
        "username": username,
        "iat": now,
        "exp": now.timestamp() + 86400,  # 24 hours
    }
    token = pyjwt.encode(payload, db_settings.jwt_secret, algorithm="HS256")
    return JSONResponse(content={"token": token, "expires_in": 86400})


@app.post("/v1/publish")
async def publish_skill(request: Request) -> JSONResponse:
    """Publish a skill artifact (WASM or AWA) to the unified GCS bucket.

    Requires ``Authorization: Bearer <JWT>`` obtained from ``/v1/auth/login``.

    Body (multipart / JSON)::
        {
          "skill_id": "haris_dev~my-scraper",
          "title": "My Scraper",
          "skill_type": "AWA",          # "WASM" or "AWA"
          "source_language": "javascript",
          "version": "1.0.0",           # semver — required
          "artifact": "<base64-encoded file content>",
          "manifest": { ... }            # full manifest JSON
        }
    """
    # ── Validate JWT ────────────────────────────────────────────
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return JSONResponse(status_code=401, content={"error": "missing or invalid Authorization header"})
    token = auth[7:]
    try:
        decoded = pyjwt.decode(token, db_settings.jwt_secret, algorithms=["HS256"])
    except pyjwt.ExpiredSignatureError:
        return JSONResponse(status_code=401, content={"error": "token expired"})
    except pyjwt.InvalidTokenError:
        return JSONResponse(status_code=401, content={"error": "invalid token"})

    developer_id = int(decoded["sub"])

    # ── Parse body ──────────────────────────────────────────────
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    skill_id = body.get("skill_id", "")
    title = body.get("title", "")
    skill_type = (body.get("skill_type", "") or "").upper()
    source_language = body.get("source_language", "unknown")
    version = (body.get("version", "") or "").strip()
    artifact_b64 = body.get("artifact", "")
    manifest_data = body.get("manifest")

    if not all([skill_id, title, artifact_b64, version]):
        return JSONResponse(status_code=422, content={"error": "skill_id, title, version, and artifact are required"})
    if skill_type not in ("WASM", "AWA"):
        return JSONResponse(status_code=422, content={"error": "skill_type must be 'WASM' or 'AWA'"})

    # ── Validate semver format ──────────────────────────────────
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        return JSONResponse(status_code=422, content={"error": f"version '{version}' is not valid semver (expected X.Y.Z)"})
    gcs_version = f"v{version}"

    # ── Determine file extension ────────────────────────────────
    ext = "wasm" if skill_type == "WASM" else "js"
    version_dir = f"skills/{skill_id}/versions/{gcs_version}"
    artifact_path = f"{version_dir}/skill.{ext}"
    manifest_path_gcs = f"{version_dir}/manifest.json"

    # ── Decode artifact ─────────────────────────────────────────
    import base64
    try:
        file_bytes = base64.b64decode(artifact_b64)
    except Exception:
        return JSONResponse(status_code=422, content={"error": "artifact must be base64-encoded"})

    # ── Upload both artifact and manifest to GCS ────────────────
    client = get_gcs_client()
    bucket = client.bucket(storage_settings.bucket)

    blob = bucket.blob(artifact_path)
    blob.upload_from_string(file_bytes, content_type="application/octet-stream")
    logger.info("Uploaded %s to gs://%s/%s", skill_id, storage_settings.bucket, artifact_path)

    if manifest_data:
        blob_m = bucket.blob(manifest_path_gcs)
        blob_m.upload_from_string(
            json.dumps(manifest_data, ensure_ascii=False),
            content_type="application/json",
        )
        logger.info("Uploaded manifest to gs://%s/%s", storage_settings.bucket, manifest_path_gcs)

    # ── Upsert marketplace_skills + version check ───────────────
    pool = await get_pg_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, current_version FROM marketplace_skills WHERE id = $1 AND developer_id = $2",
            skill_id, developer_id,
        )

        if row is None:
            # ── New skill ───────────────────────────────────────
            await conn.execute(
                """INSERT INTO marketplace_skills
                       (id, developer_id, title, skill_type, gcs_artifact_path, source_language, current_version)
                   VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                skill_id, developer_id, title, skill_type, artifact_path, source_language, version,
            )
        else:
            # ── Existing skill — enforce version bump ───────────
            current = row["current_version"]
            current_tuple = tuple(int(x) for x in current.split("."))
            new_tuple = tuple(int(x) for x in version.split("."))
            if new_tuple <= current_tuple:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error": f"version {version} must be greater than current version {current}",
                        "current_version": current,
                    },
                )

            await conn.execute(
                "UPDATE marketplace_skills SET current_version = $1, gcs_artifact_path = $2 WHERE id = $3",
                version, artifact_path, skill_id,
            )

        # ── Record the immutable version ────────────────────────
        await conn.execute(
            """INSERT INTO skill_versions (skill_id, version, gcs_artifact_path)
               VALUES ($1, $2, $3)
               ON CONFLICT (skill_id, version) DO NOTHING""",
            skill_id, version, artifact_path,
        )

    return JSONResponse(content={
        "status": "published",
        "skill_id": skill_id,
        "version": version,
        "path": artifact_path,
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

    Uses a **single** ``async for`` loop to receive all frames.  The first
    valid frame establishes the robot's identity (UUID + AES key); subsequent
    frames are processed uniformly.
    """
    await socket.accept()

    robot_uuid: bytes | None = None
    robot_uuid_str: str | None = None
    key: bytes | None = None

    try:
        async for message in socket.iter_bytes():
            result = decrypt_frame(message)
            if result is None:
                if robot_uuid is None:
                    # First frame must authenticate
                    logger.warning("Auth failure — dropping connection")
                    await socket.close(code=1008, reason="Auth failure")
                    return
                # Subsequent decryption failures are silently skipped
                continue

            frame_uuid, iv, plaintext = result

            # ── First valid frame — discover robot identity ──────
            if robot_uuid is None:
                robot_uuid = frame_uuid
                robot_uuid_str = robot_uuid.decode("utf-8", errors="replace")

                key = lookup_key_by_uuid(robot_uuid)
                if key is None:
                    logger.warning("No key for %s — dropping", robot_uuid_str)
                    await socket.close(code=1008, reason="Unknown robot")
                    return

                logger.info("Robot %s connected", robot_uuid_str)

                # ── Check for queued reply from previous session ─
                queued_reply = await _check_pending_reply(
                    robot_uuid_str, key, robot_uuid, socket,
                )
                if queued_reply is not None:
                    logger.info(
                        "Delivered queued reply to %s, awaiting next message",
                        robot_uuid_str,
                    )

            # ── Process every frame (chat, Lua output, etc.) ─────
            await _process_chat_frame(
                socket, robot_uuid, robot_uuid_str, key, iv, plaintext,
            )

    except WebSocketDisconnect:
        logger.info("Robot %s disconnected", robot_uuid_str or "unknown")
    except Exception as exc:
        logger.exception("WebSocket error for %s", robot_uuid_str or "unknown")
        try:
            await socket.close(code=1011, reason="Internal error")
        except Exception:
            pass


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
    """Decrypt, route to OpenClaw via streaming, encrypt and send each message.

    For multi-step tasks, OpenClaw may return multiple frames:
      1. Zero or more ``step`` frames (intermediate progress)
      2. One ``chat_reply`` frame (final response with Lua commands)
    Each frame is encrypted independently with a fresh IV.
    """
    try:
        data = json.loads(plaintext)
    except json.JSONDecodeError:
        logger.warning("Non-JSON plaintext from %s", robot_uuid_str)
        return

    msg_type = data.get("type")
    session_id = data.get("session_id", "")

    # ── session_reset: forward to OpenClaw, discard context ──────
    if msg_type == "session_reset":
        logger.info("Session reset for %s (session=%s)", robot_uuid_str, session_id)
        reply_json = await openclaw_process(data, robot_uuid_str)
        frame = build_downstream_frame_with_iv(robot_uuid, key, iv, reply_json.encode("utf-8"))
        await socket.send_bytes(frame)
        return

    # ── Only handle user_chat_input ──────────────────────────────
    if msg_type != "user_chat_input":
        # ── Forward any non-chat frame as Lua output to the agent ──
        lua_text = data.get("text", "")
        lua_session = data.get("session_id", "")
        if lua_text:
            logger.info(
                "Lua output from %s (type=%s, session=%s): %.120s",
                robot_uuid_str, msg_type, lua_session, lua_text,
            )
            await send_lua_output(robot_uuid_str, lua_text, lua_session)
        else:
            logger.debug(
                "Ignoring non-chat frame type=%s (no text) from %s",
                msg_type, robot_uuid_str,
            )
        return

    user_text = data.get("text", "").strip()
    if not user_text:
        return

    logger.info(
        "Chat from %s (session=%s): %.120s",
        robot_uuid_str, session_id, user_text,
    )

    # ── Call OpenClaw agent — streaming ──────────────────────────
    async for downstream_msg in openclaw_process_stream(data, robot_uuid_str):
        # Each message gets its own IV (step streaming uses fresh per-frame IVs)
        payload_bytes = json.dumps(downstream_msg).encode("utf-8")
        frame = build_downstream_frame(robot_uuid, key, payload_bytes)
        await socket.send_bytes(frame)
        logger.debug(
            "Sent downstream %s to %s: %.80s",
            downstream_msg.get("type", "?"),
            robot_uuid_str,
            str(downstream_msg.get("text", ""))[:80],
        )

    logger.info("Streaming reply complete for %s", robot_uuid_str)
