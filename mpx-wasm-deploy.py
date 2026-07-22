#!/usr/bin/env python3
"""
mpx-wasm-deploy — Direct WASM skill deployer (bypasses OpenClaw entirely).

Queries the MPX database and fake GCS directly, then pushes WASM binaries
to the robot via the existing WebSocket or host-listener bridge.

Encryption is applied automatically using the robot's AES-256-GCM key
(per-skill key wrapping, see docs/wasm-encryption-design.md).

Usage:
  # List WASM assignments for a robot
  python3 mpx-wasm-deploy.py list MPX-DOG-01

  # Download WASM artifacts locally (to ./downloads/)
  python3 mpx-wasm-deploy.py download MPX-DOG-01

  # Encrypt a local .wasm for a specific robot (standalone, no deploy)
  python3 mpx-wasm-deploy.py encrypt skill.wasm MPX-DOG-01

  # Deploy WASM to the robot (encrypted, prints Lua commands)
  python3 mpx-wasm-deploy.py deploy MPX-DOG-01

  # Deploy unencrypted (development / debugging)
  python3 mpx-wasm-deploy.py deploy MPX-DOG-01 --plain

  # Direct push via Lua commands through the bridge (encrypted)
  python3 mpx-wasm-deploy.py push MPX-DOG-01

  # Push unencrypted (development / debugging)
  python3 mpx-wasm-deploy.py push MPX-DOG-01 --plain
"""

import hashlib
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s mpx-wasm-deploy %(message)s",
)
logger = logging.getLogger("mpx-wasm-deploy")

# ── Configuration ──────────────────────────────────────────────────────
CORE_GATEWAY_URL = os.getenv("CORE_GATEWAY_URL", "http://127.0.0.1:8080")
GCS_API_URL = os.getenv("GCS_API_URL", "http://localhost:4443")
GCS_BUCKET = "mpx-marketplace-artifacts"
HOST_LISTENER_URL = os.getenv("HOST_LISTENER_URL", "http://127.0.0.1:19090")
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
ROBOT_FS_BASE = ""  # skills live at the root of LittleFS (/skill.wasm, /backflipping.wasm, etc.)

# ── AES-256-GCM WASM encryption ────────────────────────────────────────

MPXE_MAGIC = b"MPXE"
MPXE_VERSION = 0x01


def get_robot_key(robot_uuid: str) -> str | None:
    """Fetch the robot's AES-256 key (64 hex chars) from the DB."""
    sql = f"SELECT aes_key_hex FROM robots WHERE robot_uuid = '{robot_uuid}';"
    rows = db_query(sql)
    if rows and len(rows[0]) >= 1:
        return rows[0][0]
    logger.warning("No AES key found for robot %s", robot_uuid)
    return None


def encrypt_wasm_for_robot(
    robot_uuid: str,
    robot_key_hex: str,
    skill_id: str,
    wasm_bytes: bytes,
) -> bytes:
    """
    Encrypt a WASM binary for a specific robot.

    Key hierarchy (design doc §3.1):
      robot_root_key ──(wrap)──→ per_skill_key ──(encrypt)──→ WASM binary

    Returns a single MPXE-format blob ready for LittleFS.
    The per-skill key is generated fresh, used once, and discarded.

    Layout:
      [4]   magic "MPXE"
      [1]   version (0x01)
      [12]  wrap_iv
      [48]  wrapped_skill_key (32 ct + 16 tag)
      [1]   key_algo (0x00)
      [8]   skill_id_hash (sha256[:8])
      [12]  wasm_iv
      [N]   encrypted_wasm_ciphertext
      [16]  wasm_tag
    """
    robot_key = bytes.fromhex(robot_key_hex)

    # 1. Generate a fresh, random per-skill key
    skill_key = os.urandom(32)

    # 2. Wrap the skill key with the robot's root key
    #    AAD includes version prefix to prevent downgrade attacks
    wrap_iv = os.urandom(12)
    aesgcm = AESGCM(robot_key)
    wrap_aad = f"wasm-wrap:v1:{robot_uuid}".encode("utf-8")
    wrapped_key_ct = aesgcm.encrypt(wrap_iv, skill_key, wrap_aad)
    # wrapped_key_ct = 32 bytes ciphertext + 16 bytes GCM tag = 48 bytes

    # 3. Hash skill_id for fixed-width AAD
    skill_id_hash = hashlib.sha256(skill_id.encode("utf-8")).digest()[:8]

    # 4. Encrypt the WASM binary with the skill key
    wasm_iv = os.urandom(12)
    aesgcm_wasm = AESGCM(skill_key)
    wasm_aad = f"wasm-skill:v1:{skill_id_hash.hex()}".encode("utf-8")
    wasm_ct_and_tag = aesgcm_wasm.encrypt(wasm_iv, wasm_bytes, wasm_aad)

    # 5. Build single blob
    blob = bytearray()
    blob.extend(MPXE_MAGIC)                    # [4]  magic
    blob.append(MPXE_VERSION)                  # [1]  version
    blob.extend(wrap_iv)                       # [12] wrap_iv
    blob.extend(wrapped_key_ct)                # [48] wrapped_skill_key
    blob.append(0x00)                          # [1]  key_algo (reserved)
    blob.extend(skill_id_hash)                 # [8]  skill_id_hash
    blob.extend(wasm_iv)                       # [12] wasm_iv
    blob.extend(wasm_ct_and_tag)               # [N+16] ciphertext + tag

    return bytes(blob)


def load_encrypted_blob(path: str | Path) -> dict | None:
    """Parse an MPXE blob from disk and return its fields (for debugging)."""
    data = Path(path).read_bytes()
    if data[:4] != MPXE_MAGIC:
        print(f"  ✗ Not an MPXE file (magic: {data[:4]!r})")
        return None

    return {
        "magic": data[:4],
        "version": data[4],
        "wrap_iv": data[5:17],
        "wrapped_key": data[17:65],
        "key_algo": data[65],
        "skill_id_hash": data[66:74],
        "wasm_iv": data[74:86],
        "encrypted_wasm_size": len(data) - 86 - 16,
        "total_size": len(data),
    }

# DB direct access
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_USER = os.getenv("DB_USER", "mpx_admin")
DB_PASS = os.getenv("DB_PASS", "mpx_secret_password")
DB_NAME = os.getenv("DB_NAME", "mpx_marketplace_prod")


# ── DB queries via docker exec (no psql binary needed) ─────────────────
def db_query(sql: str, fmt: str = "csv") -> list[dict]:
    """Run a SQL query via docker exec on the postgres container."""
    import subprocess
    cmd = [
        "docker", "exec", "gcp-cloudsql-postgres",
        "psql", "-U", DB_USER, "-d", DB_NAME,
        "-t", "-A", f"-F\t",
        "-c", sql,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        logger.error("DB query failed: %s", result.stderr.strip())
        return []

    lines = [line.strip() for line in result.stdout.split("\n") if line.strip()]
    if not lines:
        return []

    # Parse tab-separated output
    rows = []
    for line in lines:
        parts = line.split("\t")
        rows.append(parts)
    return rows


def get_wasm_skills_for_robot(robot_uuid: str) -> list[dict]:
    """Fetch enabled WASM skills for a robot from the marketplace DB."""
    sql = f"""SELECT ms.id, ms.title, ms.current_version, ms.gcs_artifact_path,
                     sv.gcs_artifact_path as version_artifact_path
              FROM robot_skills rs
              JOIN marketplace_skills ms ON ms.id = rs.skill_id
              JOIN skill_versions sv ON sv.skill_id = ms.id AND sv.version = ms.current_version
              WHERE rs.robot_uuid = '{robot_uuid}'
                AND ms.skill_type = 'WASM'
                AND rs.enabled = true;"""
    raw = db_query(sql)
    skills = []
    for row in raw:
        if len(row) >= 5:
            skills.append({
                "skill_id": row[0],
                "title": row[1],
                "version": row[2],
                "artifact_path": row[4],  # use version_artifact_path
            })
    return skills


# ── GCS download ───────────────────────────────────────────────────────
def download_wasm(skill_id: str, artifact_path: str, version: str) -> bytes | None:
    """Download a WASM binary from fake GCS. Returns bytes or None."""
    url = f"{GCS_API_URL}/download/storage/v1/b/{GCS_BUCKET}/o/{urllib.parse.quote(artifact_path, safe='')}?alt=media"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        logger.info("Downloaded %s (%d bytes) — %s", artifact_path, len(data), skill_id)
        return data
    except urllib.error.HTTPError as e:
        # Try alternative — sometimes fake-gcs needs the path without alt=media
        alt_url = f"{GCS_API_URL}/{GCS_BUCKET}/{artifact_path}"
        try:
            with urllib.request.urlopen(alt_url, timeout=30) as r:
                data = r.read()
            logger.info("Downloaded %s (%d bytes) via alt path — %s", artifact_path, len(data), skill_id)
            return data
        except Exception as e2:
            logger.error("Failed to download %s: HTTP %s / alt: %s", artifact_path, e, e2)
            return None
    except Exception as e:
        logger.error("Failed to download %s: %s", artifact_path, e)
        return None


def download_manifest(skill_id: str, version: str) -> dict | None:
    """Download the manifest JSON for a WASM skill version."""
    manifest_path = f"skills/{skill_id}/versions/v{version}/manifest.json"
    url = f"{GCS_API_URL}/download/storage/v1/b/{GCS_BUCKET}/o/{urllib.parse.quote(manifest_path, safe='')}?alt=media"
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except Exception:
        # Try XML API path
        alt_url = f"{GCS_API_URL}/{GCS_BUCKET}/{manifest_path}"
        try:
            with urllib.request.urlopen(alt_url, timeout=10) as r:
                return json.loads(r.read())
        except Exception:
            logger.warning("No manifest found for %s v%s", skill_id, version)
            return None


# ── Push to robot via host-listener bridge ─────────────────────────────
def push_lua_commands_via_bridge(robot_uuid: str, lua_commands: list[str]) -> bool:
    """Push Lua commands to the robot via the host-listener bridge.

    Creates a fake pending message that the robot-responder would normally
    handle, but we side-step by directly injecting Lua commands as a reply.
    Then the bridge queues them for the robot's next WebSocket poll.
    """
    # We inject directly into the reply queue for the robot
    # The host-listener has /v1/replies/<robot_uuid> endpoint for queued replies
    reply_payload = {
        "type": "chat_reply",
        "text": "Deploying WASM skills to robot...",
        "commands": [{"type": "lua", "script": script} for script in lua_commands],
        "ts": int(time.time()),
    }

    # The host-listener accepts replies via the /v1/messages endpoint
    # But for direct deployment we need a msg_id. Let's use a synthetic one.
    import uuid
    msg_id = f"deploy-{uuid.uuid4().hex[:12]}"

    payload_bytes = json.dumps(reply_payload).encode("utf-8")
    reply_url = f"{HOST_LISTENER_URL}/v1/messages/{msg_id}/reply"
    req = urllib.request.Request(
        reply_url,
        data=payload_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Robot-UUID": robot_uuid,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read())
            logger.info("Lua commands queued for %s: %s", robot_uuid, resp)
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        logger.warning("Bridge push failed HTTP %d: %s", e.code, body)
        return False
    except Exception as e:
        logger.warning("Bridge push failed: %s", e)
        return False


# ── Direct core_gateway WebSocket push ─────────────────────────────────
# Note: This requires an active WebSocket connection. The robot connects
# to core_gateway and we'd need to proxy through it.
# For now, we use the bridge path above, which is simpler.


# ── Generate deployment Lua scripts ────────────────────────────────────

def make_write_lua_commands(robot_fs_path: str, wasm_data: bytes) -> list[str]:
    """Generate Lua commands to write a WASM binary to the robot's LittleFS.

    Pads WASM to a multiple of 3 bytes to avoid base64 ``=`` padding,
    which the robot's ``crypto.base64_decode()`` mishandles.
    The trailing null padding byte is stripped after decode on the robot.
    """
    import base64

    # Pad to 3N so base64 has no ``=`` padding
    pad_len = (3 - len(wasm_data) % 3) % 3
    if pad_len:
        wasm_padded = wasm_data + b"\x00" * pad_len
    else:
        wasm_padded = wasm_data
    b64 = base64.b64encode(wasm_padded).decode("ascii")
    chunk_size = 512  # safe chunk size for Lua strings

    commands = []

    # Check filesystem first
    commands.append(f"""
local info = fs.info()
print("FS: " .. tostring(info.used) .. "/" .. tostring(info.total) .. " bytes used")
""".strip())

    parent = os.path.dirname(robot_fs_path)
    fname = os.path.basename(robot_fs_path)
    commands.append(f"""
local entries = fs.list("{parent}")
if not entries then
  print("listing root failed")
else
  for _, e in ipairs(entries) do
    if e.name == "{fname}" then
      print("WASM already exists: " .. e.name .. " (" .. e.size .. " bytes)")
    end
  end
end
""".strip())

    # Build base64 in chunks
    commands.append("local chunks = {}")
    for i in range(0, len(b64), chunk_size):
        chunk = b64[i:i + chunk_size]
        commands.append(f"table.insert(chunks, [[{chunk}]])")
        if (i // chunk_size) % 20 == 19:
            commands.append("robot.delay_ms(10)")

    # Decode, strip trailing null padding, write
    commands.append(f"""
local b64 = table.concat(chunks)
local raw = crypto.base64_decode(b64)
-- strip trailing null padding (added to avoid base64 =)
local sz = #raw
if sz > 0 and raw:byte(sz) == 0 then raw = raw:sub(1, sz - 1) end
local ok = fs.write("{robot_fs_path}", raw)
if ok then
  print("WASM deployed to " .. "{robot_fs_path}" .. " (" .. tostring(#raw) .. " bytes)")
else
  print("ERROR: fs.write failed for " .. "{robot_fs_path}" .. " — denied")
end
chunks = nil
""".strip())

    return commands


# ── CLI commands ───────────────────────────────────────────────────────

def cmd_list(robot_uuid: str):
    """List WASM skills assigned to a robot."""
    skills = get_wasm_skills_for_robot(robot_uuid)
    if not skills:
        print(f"No WASM skills assigned to {robot_uuid}")
        return

    print(f"\nWASM Skills for {robot_uuid}:")
    print(f"{'Skill ID':<30} {'Title':<35} {'Version':<10} {'Artifact':<55}")
    print("-" * 130)
    for s in skills:
        print(f"{s['skill_id']:<30} {s['title']:<35} {s['version']:<10} {s['artifact_path']:<55}")


def cmd_download(robot_uuid: str):
    """Download WASM artifacts locally."""
    skills = get_wasm_skills_for_robot(robot_uuid)
    if not skills:
        print(f"No WASM skills assigned to {robot_uuid}")
        return

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for s in skills:
        print(f"\nDownloading {s['skill_id']} v{s['version']}...")
        data = download_wasm(s["skill_id"], s["artifact_path"], s["version"])
        if data:
            local_path = DOWNLOAD_DIR / f"{s['skill_id']}.wasm"
            local_path.write_bytes(data)
            print(f"  → Saved to {local_path} ({len(data)} bytes)")

        manifest = download_manifest(s["skill_id"], s["version"])
        if manifest:
            manifest_path = DOWNLOAD_DIR / f"{s['skill_id']}-manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2))
            print(f"  → Manifest saved to {manifest_path}")


def cmd_deploy(robot_uuid: str, plain: bool = False):
    """Download WASM, encrypt (unless --plain), and generate deploy Lua commands (prints them)."""
    skills = get_wasm_skills_for_robot(robot_uuid)
    if not skills:
        print(f"No WASM skills assigned to {robot_uuid}")
        return

    if not plain:
        robot_key_hex = get_robot_key(robot_uuid)
        if not robot_key_hex:
            print(f"  ✗ Cannot encrypt: no AES key found for robot {robot_uuid}. Use --plain for unencrypted deploy.")
            return

    all_commands = []
    for s in skills:
        print(f"\n=== Preparing {s['skill_id']} v{s['version']} ===")
        data = download_wasm(s["skill_id"], s["artifact_path"], s["version"])
        if not data:
            print(f"  ✗ Failed to download {s['skill_id']}")
            continue

        slug = s['skill_id'].split('~', 1)[1] if '~' in s['skill_id'] else s['skill_id']
        robot_path = f"{ROBOT_FS_BASE}/{slug}.wasm" if ROBOT_FS_BASE else f"/{slug}.wasm"

        if not plain:
            data = encrypt_wasm_for_robot(robot_uuid, robot_key_hex, s['skill_id'], data)
            print(f"  🔒 Encrypted ({len(data)} bytes MPXE blob)")
            robot_path = f"{ROBOT_FS_BASE}/{slug}.mpxe" if ROBOT_FS_BASE else f"/{slug}.mpxe"

        commands = make_write_lua_commands(robot_path, data)
        all_commands.extend(commands)

        # Add a verification command
        all_commands.append(f"""
-- Verify {s['skill_id']}
local ok = fs.exists("{robot_path}")
if ok then
  print("✓ {s['skill_id']} deployed to {robot_path}")
  wasm.run("{robot_path}", "on_start")
else
  print("✗ WASM file not found at {robot_path}")
end
""".strip())

    print(f"\n{'='*60}")
    print(f"Total Lua commands to execute: {len(all_commands)}")
    print(f"{'='*60}")
    for i, cmd in enumerate(all_commands):
        print(f"\n--- Command {i+1} ---")
        print(cmd.strip())


def cmd_push(robot_uuid: str, plain: bool = False):
    """Download WASM, encrypt (unless --plain), and push directly to robot via bridge."""
    skills = get_wasm_skills_for_robot(robot_uuid)
    if not skills:
        print(f"No WASM skills assigned to {robot_uuid}")
        return

    if not plain:
        robot_key_hex = get_robot_key(robot_uuid)
        if not robot_key_hex:
            print(f"  ✗ Cannot encrypt: no AES key found for robot {robot_uuid}. Use --plain for unencrypted push.")
            return

    all_commands = []
    for s in skills:
        print(f"\n=== Deploying {s['skill_id']} v{s['version']} ===")
        data = download_wasm(s["skill_id"], s["artifact_path"], s["version"])
        if not data:
            print(f"  ✗ Failed to download {s['skill_id']}")
            continue

        slug = s['skill_id'].split('~', 1)[1] if '~' in s['skill_id'] else s['skill_id']
        robot_path = f"{ROBOT_FS_BASE}/{slug}.wasm" if ROBOT_FS_BASE else f"/{slug}.wasm"

        if not plain:
            data = encrypt_wasm_for_robot(robot_uuid, robot_key_hex, s['skill_id'], data)
            print(f"  🔒 Encrypted ({len(data)} bytes MPXE blob)")
            robot_path = f"{ROBOT_FS_BASE}/{slug}.mpxe" if ROBOT_FS_BASE else f"/{slug}.mpxe"

        commands = make_write_lua_commands(robot_path, data)
        all_commands.extend(commands)

        # Verification + run
        all_commands.append(f"""
-- Verify + execute {s['skill_id']}
if fs.exists("{robot_path}") then
  print("✓ Deployed: {s['skill_id']}")
  wasm.run("{robot_path}", "on_start")
else
  print("✗ Missing: {robot_path}")
end
""".strip())

    if all_commands:
        print(f"\nPushing {len(all_commands)} Lua commands to robot {robot_uuid} via bridge...")
        success = push_lua_commands_via_bridge(robot_uuid, all_commands)
        if success:
            print("✓ Commands queued. Robot will execute on next WebSocket poll.")
        else:
            print("✗ Failed to queue commands. Try 'deploy' instead to see what would be sent.")


# ── Build a standalone shell script for headless deploy ────────────────
def generate_deploy_script(robot_uuid: str) -> str:
    """Generate a standalone bash script that does the full deploy."""
    skills = get_wasm_skills_for_robot(robot_uuid)
    lines = [
        "#!/bin/bash",
        f"# MPX WASM Deploy Script — generated for {robot_uuid}",
        f"# Bypasses OpenClaw entirely — direct DB + GCS + Lua bridge",
        "",
        'echo "=== MPX WASM Deploy ==="',
        "",
    ]

    for s in skills:
        artifact = s["artifact_path"]
        skill_id = s["skill_id"]
        version = s["version"]
        slug = skill_id.split('~', 1)[1] if '~' in skill_id else skill_id
        robot_path = f"{ROBOT_FS_BASE}/{slug}.wasm" if ROBOT_FS_BASE else f"/{slug}.wasm"
        wasm_url = f"{GCS_API_URL}/{GCS_BUCKET}/{artifact}"

        lines += [
            f"",
            f'echo "--- {skill_id} v{version} ---"',
            f"",
            f"# 1. Download WASM from fake GCS",
            f"WASM_DATA=$(curl -s '{wasm_url}' | base64 -w0)",
            f'WASM_LEN=$(echo "$WASM_DATA" | wc -c)',
            f'echo "Downloaded: $WASM_LEN bytes (base64)"',
            f"",
            f"# 2. Build Lua command to write to robot filesystem",
            f"# Chunked base64 write (512-byte chunks for Lua string limits)",
            f"LUA_SCRIPT=\"\"",
            f"LUA_SCRIPT+='local chunks = {{}}'\\n",
            f"",
            f"# Split into chunks and generate inline Lua inserts",
            f"CHUNK_SIZE=512",
            f"OFFSET=0",
            f"while [ $OFFSET -lt $WASM_LEN ]; do",
            f"  CHUNK=$(echo \"$WASM_DATA\" | dd bs=1 skip=$OFFSET count=$CHUNK_SIZE 2>/dev/null)",
            f"  ESCAPED=$(echo \"$CHUNK\" | sed \"s/'/'\\\"'\\\"'/g\")",
            f"  LUA_SCRIPT+=\"table.insert(chunks, '${ESCAPED}')\\n\"",
            f"  OFFSET=$((OFFSET + CHUNK_SIZE))",
            f"  if [ $((OFFSET / CHUNK_SIZE % 20)) -eq 19 ]; then",
            f'    LUA_SCRIPT+="robot.delay_ms(10)\\n"',
            f"  fi",
            f"done",
            f"",
            f"LUA_SCRIPT+='local b64 = table.concat(chunks)'\\n",
            f"LUA_SCRIPT+='local data = crypto.base64_decode(b64)'\\n",
            f"LUA_SCRIPT+='local ok = fs.write(\"{robot_path}\", data)'\\n",
            f"LUA_SCRIPT+='local ok = result'\\n",
            f"LUA_SCRIPT+='print(\"ok=\" .. tostring(ok))'\\n",
            f"",
            f"# 3. Push via bridge",
            f'MSG_ID="deploy-$(date +%s)"',
            f'PAYLOAD=$(cat <<ENDPAYLOAD',
            f'{{"type":"chat_reply","text":"Deploying {skill_id}...","commands":[{{"type":"lua","script":"{{{{LUA_SCRIPT}}}}"}}],"ts":$(date +%s)}}',
            f'ENDPAYLOAD',
            f')',
            f'PAYLOAD=$(echo "$PAYLOAD" | sed "s/{{{{LUA_SCRIPT}}}}/$LUA_SCRIPT/g")',
            f"",
            f'curl -s -X POST "{HOST_LISTENER_URL}/v1/messages/$MSG_ID/reply" \\',
            f'  -H "Content-Type: application/json" \\',
            f"  -H \"X-Robot-UUID: {robot_uuid}\" \\",
            f'  -d "$PAYLOAD"',
            f"",
            f'echo ""',
        ]

    lines += [
        "",
        'echo "=== Deploy complete ==="',
    ]

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    command = sys.argv[1]

    if command == "list":
        if len(sys.argv) < 3:
            print("Usage: mpx-wasm-deploy.py list <robot_uuid>")
            sys.exit(1)
        cmd_list(sys.argv[2])

    elif command == "download":
        if len(sys.argv) < 3:
            print("Usage: mpx-wasm-deploy.py download <robot_uuid>")
            sys.exit(1)
        cmd_download(sys.argv[2])

    elif command == "encrypt":
        """Encrypt a local .wasm file for a specific robot."""
        if len(sys.argv) < 4:
            print("Usage: mpx-wasm-deploy.py encrypt <input.wasm> <robot_uuid> [--output output.mpxe]")
            sys.exit(1)
        input_path = sys.argv[2]
        robot_uuid = sys.argv[3]
        output_path = None
        if "--output" in sys.argv:
            idx = sys.argv.index("--output")
            if idx + 1 < len(sys.argv):
                output_path = sys.argv[idx + 1]

        wasm_bytes = Path(input_path).read_bytes()
        robot_key_hex = get_robot_key(robot_uuid)
        if not robot_key_hex:
            print(f"  ✗ No AES key found for robot {robot_uuid}")
            sys.exit(1)

        # Derive skill_id from filename if not in DB context
        skill_id = Path(input_path).stem

        blob = encrypt_wasm_for_robot(robot_uuid, robot_key_hex, skill_id, wasm_bytes)

        if output_path:
            Path(output_path).write_bytes(blob)
            print(f"  🔒 Encrypted blob written to {output_path} ({len(blob)} bytes)")
        else:
            output_path = str(Path(input_path).with_suffix(".mpxe"))
            Path(output_path).write_bytes(blob)
            print(f"  🔒 Encrypted blob written to {output_path} ({len(blob)} bytes)")

        # Print blob info
        info = load_encrypted_blob(output_path)
        if info:
            print(f"  Version:  {info['version']}")
            print(f"  Skill ID hash: {info['skill_id_hash'].hex()}")
            print(f"  Encrypted WASM: {info['encrypted_wasm_size']} bytes")

    elif command == "deploy":
        if len(sys.argv) < 3:
            print("Usage: mpx-wasm-deploy.py deploy <robot_uuid> [--plain]")
            sys.exit(1)
        plain = "--plain" in sys.argv
        cmd_deploy(sys.argv[2], plain=plain)

    elif command == "push":
        if len(sys.argv) < 3:
            print("Usage: mpx-wasm-deploy.py push <robot_uuid> [--plain]")
            sys.exit(1)
        plain = "--plain" in sys.argv
        cmd_push(sys.argv[2], plain=plain)

    elif command == "generate-script":
        if len(sys.argv) < 3:
            print("Usage: mpx-wasm-deploy.py generate-script <robot_uuid>")
            sys.exit(1)
        script = generate_deploy_script(sys.argv[2])
        script_path = DOWNLOAD_DIR / f"deploy-wasm-{sys.argv[2]}.sh"
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        script_path.write_text(script)
        os.chmod(script_path, 0o755)
        print(f"Deploy script written to {script_path}")

    elif command == "all":
        """Do the full pipeline: list → download → push."""
        if len(sys.argv) < 3:
            print("Usage: mpx-wasm-deploy.py all <robot_uuid>")
            sys.exit(1)
        robot = sys.argv[2]
        cmd_list(robot)
        cmd_download(robot)
        cmd_push(robot)

    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
