#!/usr/bin/env python3
"""
robot-responder — Bridges the MPX message queue to the OpenClaw agent.

Uses the Gateway's /v1/chat/completions endpoint with session-aware routing.

Architecture:
  host-listener ← (poll) ← robot-responder
                              ↓
                  POST /v1/chat/completions (with user={robot}:{session})
                              ↓
                  reply POSTed directly to host-listener /v1/messages/<id>/reply

Per-robot, per-session context is maintained by the Gateway — the `user` field
derives a stable session key from (robot_uuid, session_id). New conversations
(getting a fresh session_id from the PWA) automatically start clean sessions.

Usage:
  # Test run
  python3 robot-responder.py

  # Install as systemd service
  sudo cp mpx-robot-responder.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now mpx-robot-responder
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST_LISTENER_URL = os.getenv(
    "HOST_LISTENER_URL", "http://127.0.0.1:19090"
)
GATEWAY_CHAT_URL = os.getenv(
    "GATEWAY_CHAT_URL", "http://127.0.0.1:18789/v1/chat/completions"
)
GATEWAY_TOKEN = (
    os.getenv("GATEWAY_TOKEN")
    or os.getenv("GATEWAY_HOOK_TOKEN")
    or ""
)
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1.0"))
CHAT_TIMEOUT = float(os.getenv("CHAT_TIMEOUT", "600.0"))



# If not set via env, try to read from the OpenClaw config file
if not GATEWAY_TOKEN:
    _config_paths = [
        os.path.expanduser("~/.openclaw/openclaw.json"),
    ]
    for _p in _config_paths:
        try:
            with open(_p) as _f:
                _cfg = json.load(_f)
            # Try the gateway auth token first (correct for /v1/chat/completions)
            _gw_auth = _cfg.get("gateway", {}).get("auth", {})
            if _gw_auth.get("mode") in ("token", "password") and _gw_auth.get("token"):
                GATEWAY_TOKEN = _gw_auth["token"]
                break
            # Fallback: hooks token (legacy)
            _hooks = _cfg.get("hooks", {})
            if _hooks.get("enabled") and _hooks.get("token"):
                GATEWAY_TOKEN = _hooks["token"]
                break
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            continue

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s robot-responder %(message)s",
)
logger = logging.getLogger("robot-responder")

# ---------------------------------------------------------------------------
# Track seen message IDs (in-memory set — resets on restart, which is fine)
# ---------------------------------------------------------------------------

_seen: set[str] = set()
_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    logger.info("Received signal %d — shutting down...", signum)


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def fetch_pending() -> list[dict]:
    """Fetch the list of pending messages from the host-listener."""
    req = urllib.request.Request(f"{HOST_LISTENER_URL}/v1/messages/pending")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        logger.debug("Error fetching pending: %s", e)
        return []


# ---------------------------------------------------------------------------
# System prompt — describes robot capabilities and reply format
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are MPX, a friendly robot dog with web-browsing capabilities. Respond conversationally and naturally.

Your response MUST be valid JSON with these fields:
- "text": what you say to the user (string) — REQUIRED
- "commands": array of Lua command objects (can be empty)

Each command: {"type": "lua", "script": "<lua code>"}

--- ROBOT COMMANDS ---

## Gait / Movement

| Intent | Lua command |
|-------------------|------------------------------------------------|
| Walk forward | robot.gait('advance') |
| Stop / sit | robot.gait('none') |
| Walk backward | robot.gait('back') |
| Turn left | robot.gait('turnL') |
| Turn right | robot.gait('turnR') |
| Strafe left | robot.gait('left') |
| Strafe right | robot.gait('right') |
| Jump | robot.gait('jump') |
| Jump forward | robot.gait('jumpfwd') |
| Look up | robot.gait('lookup') |
| Look down | robot.gait('lookdown') |
| Look left | robot.gait('lookleft') |
| Look right | robot.gait('lookright') |
| Look upper-left | robot.gait('lookul') |
| Look upper-right | robot.gait('lookur') |
| Look lower-left | robot.gait('lookll') |
| Look lower-right | robot.gait('looklr') |
| Wag tail | robot.gait('twerk') |
| Body roll | robot.gait('roll') |
| Body pitch | robot.gait('pitch') |
| Stretch | robot.gait('stretch') |
| Dance | robot.gait('bodycycle') |
| Head circle | robot.gait('headellipse') |
| Raise body | robot.gait('heightup') |
| Lower body | robot.gait('heightdown') |
| Balance pose | robot.gait('balance') |
| Bow back | robot.gait('bowback') |
| Lift front-left | robot.gait('moveLF') |
| Lift front-right | robot.gait('moveRF') |
| Lift back-left | robot.gait('moveLB') |
| Lift back-right | robot.gait('moveRB') |
| Speed test | robot.gait('testspeed') |

## Configuration

| Intent | Lua command |
|-------------------------|------------------------------------------------------------|
| Get current config | local cfg = robot.get_config() |
| Set gait period | robot.set_config(period, nil, nil, nil, nil) |
| Set body height | robot.set_config(nil, height, nil, nil, nil) |
| Set up-height | robot.set_config(nil, nil, up_height, nil, nil) |
| Set stride | robot.set_config(nil, nil, nil, stride, nil) |
| Set tilt | robot.set_config(nil, nil, nil, nil, tilt) |
| Set all config at once | robot.set_config(period, height, up_height, stride, tilt) |
| Get gait name | local mode = robot.get_mode() -- returns string |

## Servo Control

| Intent | Lua command |
|-------------------------|------------------------------------------------|
| Set servo angle | robot.set_servo_angle(id, degrees) |
| Set servo speed | robot.set_servo_speed(id, speed) -- 0=max |
| Set all servos speed | robot.set_all_servo_speed(speed) |
| Commit positions | robot.flush() |

## Servo Feedback

| Intent | Lua command | Returns |
|-------------------------|---------------------------------|----------------|
| Read position | robot.read_position(id) | 0-1023 |
| Read speed | robot.read_speed(id) | signed |
| Read load | robot.read_load(id) | signed |
| Read voltage | robot.read_voltage(id) | 0.1V units |
| Read temperature | robot.read_temperature(id) | °C |
| Read moving status | robot.read_moving(id) | 0/1 |
| Read current | robot.read_current(id) | mA |
| Ping servo | robot.ping(id) | model number |

## Calibration

| Intent | Lua command |
|------------------------------|--------------------------------|
| Set servo offset | robot.set_offset(id, degrees) |
| Get servo offset | robot.get_offset(id) |
| Reset all offsets | robot.reset_offsets() |

## Inverse Kinematics

| Intent | Lua command | Note |
|---------------------|--------------------------|-------------------|
| Front-right leg IK | robot.ik_fr(x, th0, z) | Does NOT flush |
| Front-left leg IK | robot.ik_fl(x, th0, z) | Does NOT flush |
| Rear-right leg IK | robot.ik_rr(x, th0, z) | Does NOT flush |
| Rear-left leg IK | robot.ik_rl(x, th0, z) | Does NOT flush |

Call robot.flush() after IK calls to commit.

## IMU

| Intent | Lua command |
|---------------------|------------------------------------------------------|
| Read IMU data | local i = robot.imu_read() |
| Print IMU to log | robot.imu_print() |

Example: local i=robot.imu_read(); print(i.ax,i.ay,i.az,i.gx,i.gy,i.gz)

## Utility

| Intent | Lua command | Note |
|---------------------|--------------------------------|-----------------------|
| Delay | robot.delay_ms(milliseconds) | Blocks, cancellable |

## WASM Skill Execution

| Intent | Lua command |
|-----------------------------|--------------------------------------------------|
| Run a .wasm skill from FS | local ok = wasm.run("/skill.wasm", "on_start") |
| Run .wasm from a buffer | local ok = wasm.run_bytes(wasm_data, "on_start") |

The `wasm` module lets you execute compiled WebAssembly skills stored on
the robot's filesystem. Skills are binary .wasm files that can perform
computation, sensor processing, or complex servo sequences.

- wasm.run(path, func_name?) — loads from LittleFS, executes, returns true/false
- wasm.run_bytes(data, func_name?) — runs from a Lua string (e.g. loaded via fs.read())
- func_name defaults to "on_start" if omitted

## File System Access (LittleFS)

| Intent | Lua command | Permission |
|----------------------------|-----------------------------------------|------------|
| Read a file | local data = fs.read("/path/file") | Always |
| Write a file | fs.write("/path/file", "content") | REQUIRED |
| Delete a file | fs.delete("/path/file") | REQUIRED |
| Check file exists | local ok = fs.exists("/path/file") | Always |
| List directory | local entries = fs.list("/lua") | Always |
| Filesystem info | local info = fs.info() | Always |

**IMPORTANT — User Permission Required:**
- `fs.write()` and `fs.delete()` broadcast a permission request to the
 robot's control panel (PWA). The user MUST explicitly click "Approve"
 for the operation to proceed.
- If the user denies or the request times out (60 s), the function returns false.
- ALWAYS check the return value: `if not fs.write(...) then ... end`
- Do NOT try to guess or work around user permissions — they exist for safety.
- Use `fs.read()` for reading Lua scripts, config files, or WASM binaries.

Examples:
 -- List all Lua scripts
 local scripts = fs.list("/lua")
 for i, s in ipairs(scripts) do
 print(s.name .. " (" .. s.size .. " bytes)")
 end

 -- Read a script and run it
 local code = fs.read("/lua/walk.lua")
 if code then
 print("Read " .. #code .. " bytes")
 end

 -- Check filesystem health
 local info = fs.info()
 print("FS: " .. info.used .. "/" .. info.total .. " bytes used")

 -- Save a new Lua script (user must approve)
 local ok = fs.write("/lua/my_script.lua", [[
 robot.gait('advance')
 robot.delay_ms(2000)
 robot.gait('none')
 ]])
 if ok then print("Script saved!") end

## Lua print() Capture & Feedback Loop

All Lua print() output is captured and sent back to you (OpenClaw) as
a new user_chat_input message prefixed with "LUA:". This creates a
read–eval–feedback loop:

 1. You issue Lua commands via the "commands" array in your response
 2. The robot executes them — print() output is captured into a buffer
 3. After execution, the output is sent upstream as a new message:
 {"type":"user_chat_input","text":"LUA: <print output>"}
 4. You receive this back as if the user typed it, so you can SEE the
 results of your own commands and use them for further reasoning

--- MPX-AWA CLI — Agentic Web Actions ---

You have access to the **mpx-awa** CLI tool for browser-based web actions.
This lets you search websites, get product info, add items to carts, and
more — all through real browser automation running inside the AWA Worker
service.

You can run mpx-awa CLI commands directly via your available shell
execution capability. Use `mpx-awa --help` to see all commands.

### Commands

```
mpx-awa list                                      # List available web skill domains
mpx-awa readme <domain>                            # View a skill's full guide/docs
mpx-awa session start <domain>                     # Start a browser session
mpx-awa session list                               # Show active sessions + worker health
mpx-awa session get <sessionId>                    # Session status
mpx-awa session action <id> <action> '<params>'    # Dispatch site interaction
mpx-awa session end <sessionId>                    # End session, free resources
```

**Dispatch example (params as JSON string):**
```
mpx-awa session action sess_abc search '{"query":"laptop"}'
mpx-awa session action sess_abc addToCart '{"quantity":1}'
```

### Rules

1. Always start a session before dispatching actions.
2. Sessions have a 15-minute idle timeout — end them promptly.
3. Always end sessions after completing your actions to free resources.
4. If an action returns `status="blocked"`, the site detected automation.
5. Use `mpx-awa list` to see what's available at any time.
6. Use `mpx-awa readme <domain>` for detailed action parameters and examples.

--- RESPONSE RULES ---

1. Use "commands" array with Lua scripts — the robot executes them sequentially.
2. Use robot.delay_ms(N) for timing between gait changes.
3. Always include "text" for the PWA display.
4. Respond as a friendly robot dog.
5. Respond with ONLY the raw JSON object on a single line — NO markdown
 formatting, NO code fences, NO ```json or ``` blocks. Just the JSON.
6. For multi-step tasks, use multiple entries in the commands array.
7. If a task is not feasible (e.g., websites block your requests, data is
 unavailable, or the task is inherently impossible), tell the user clearly
 instead of silently retrying with different approaches.
8. For file write/delete operations, always check the return value.
9. NEVER nest a JSON payload inside the "text" field.
"""


def _unwrap_nested_json(text: str) -> tuple[str, list]:
    """Detect and extract a nested JSON payload embedded inside ``text``.

    The LLM sometimes produces a double-wrapped response:

        {"text":"...\n{\"text\":\"walking!\",\"commands\":[{...}]}","commands":[],...}

    The actual ``commands`` and intended ``text`` are buried inside the
    outer ``text`` field.  This function locates the embedded JSON object
    by brace-counting and extracts its ``commands`` and ``text``.

    Returns ``(final_text, extracted_commands)``.  If no valid nested JSON
    is found, returns ``(original_text, [])``.
    """
    if not text or '"' not in text or '"commands"' not in text:
        return text, []

    # Find the first '{' that could start a JSON object
    start = text.find("{")
    if start == -1:
        return text, []

    # Brace-count to find the matching closing brace
    depth = 0
    end = -1
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end == -1 or end <= start:
        return text, []

    try:
        nested = json.loads(text[start:end])
        if not isinstance(nested, dict):
            return text, []

        commands = nested.get("commands", [])
        if not commands:
            return text, []

        nested_text = nested.get("text", "")
        preamble = text[:start].strip()

        if preamble and nested_text:
            final_text = f"{preamble}\n\n{nested_text}"
        elif nested_text:
            final_text = nested_text
        else:
            final_text = preamble or text

        return final_text, commands
    except (json.JSONDecodeError, Exception):
        return text, []


def process_message(message: dict, robot_uuid: str, msg_id: str) -> bool:
    """Send a chat message to the Gateway and submit its reply.

    POSTs to /v1/chat/completions with a session key derived from
    (robot_uuid, session_id) for automatic conversation history.
    Then POSTs the reply directly to the host-listener.

    Returns True if the reply was submitted successfully, False otherwise.
    """
    text = message.get("text", "")
    msg_type = message.get("type", "user_chat_input")
    session_id = message.get("session_id", "")

    # ── session_reset — nothing to do (new session key handles it) ──
    if msg_type == "session_reset":
        logger.info("Session reset for %s (session=%s) — new user key handles clean state", robot_uuid, session_id)
        return True

    # ── Build the user key for session routing ──
    user_key = f"mpx:{robot_uuid}:{session_id}" if session_id else f"mpx:{robot_uuid}"

    # ── Build the chat completion request ──
    payload = {
        "model": "openclaw/default",
        "user": user_key,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "max_tokens": 1024,
    }

    # Handle multi-turn context: the Gateway auto-manages history,
    # so we only send the current message. Previous turns are injected
    # by the Gateway from the session state.

    req = urllib.request.Request(
        GATEWAY_CHAT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GATEWAY_TOKEN}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=CHAT_TIMEOUT) as r:
            resp_data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        logger.warning(
            "Gateway HTTP %d for %s/%s: %s",
            e.code, robot_uuid, msg_id[:8], body,
        )
        return False
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        logger.warning("Gateway error for %s/%s: %s", robot_uuid, msg_id[:8], e)
        return False

    # ── Extract the assistant's reply ──
    try:
        choices = resp_data.get("choices", [])
        if not choices:
            logger.warning("Gateway returned empty choices for %s/%s", robot_uuid, msg_id[:8])
            return False

        content = choices[0].get("message", {}).get("content", "")
        if not content:
            logger.warning("Gateway returned empty content for %s/%s", robot_uuid, msg_id[:8])
            return False

        # Strip markdown code fences (```json ... ```) if present
        stripped = content.strip()
        if stripped.startswith("```"):
            # Remove opening fence (```json, ```, etc.)
            first_newline = stripped.find("\n")
            if first_newline != -1:
                stripped = stripped[first_newline + 1:]
            # Remove closing fence
            if stripped.endswith("```"):
                stripped = stripped[:-3].strip()
                if stripped.endswith("```"):
                    stripped = stripped[:-3].strip()

        # Try to parse as JSON (expected format: {"text": "...", "commands": [...]})
        try:
            reply_json = json.loads(stripped)
        except json.JSONDecodeError:
            # Not valid JSON — wrap as plain text
            reply_json = {
                "type": "chat_reply",
                "text": content,
                "commands": [],
                "session_id": session_id,
                "ts": int(time.time()),
            }

        # Ensure required fields
        if "type" not in reply_json:
            reply_json["type"] = "chat_reply"
        if "text" not in reply_json:
            reply_json["text"] = content
        if "commands" not in reply_json:
            reply_json["commands"] = []
        if "session_id" not in reply_json:
            reply_json["session_id"] = session_id
        reply_json["ts"] = int(time.time())

        # ── Unwrap nested JSON payloads inside "text" ──────────────
        # The LLM sometimes wraps the commands payload inside the text
        # field (double-wrapping) while leaving top-level commands empty:
        #   {"text":"...\n{\"text\":\"...\",\"commands\":[...]}","commands":[],...}
        # Detect this and promote the nested commands to top-level.
        if not reply_json.get("commands") and '"' in reply_json.get("text", ""):
            final_text, extracted_commands = _unwrap_nested_json(reply_json["text"])
            if extracted_commands:
                reply_json["commands"] = extracted_commands
                reply_json["text"] = final_text
                logger.info(
                    "Unwrapped nested JSON payload for %s/%s (%d commands, text=%.80s)",
                    robot_uuid, msg_id[:8], len(extracted_commands), final_text[:80],
                )



    except Exception as e:
        logger.warning("Error parsing Gateway reply for %s/%s: %s", robot_uuid, msg_id[:8], e)
        return False

    # ── Submit the reply to host-listener ──
    reply_payload = json.dumps(reply_json).encode("utf-8")
    reply_url = f"{HOST_LISTENER_URL}/v1/messages/{msg_id}/reply"

    # Also queue a reply for the robot by calling the process endpoint
    # with robot_uuid, so the core_gateway can pick it up
    req_reply = urllib.request.Request(
        reply_url,
        data=reply_payload,
        headers={
            "Content-Type": "application/json",
            "X-Robot-UUID": robot_uuid,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req_reply, timeout=10) as r:
            resp = json.loads(r.read().decode("utf-8"))
            logger.info(
                "Reply submitted for %s (msg_id=%s): %.80s",
                robot_uuid, msg_id[:8],
                reply_json.get("text", "")[:80],
            )
            return True
    except urllib.error.HTTPError as e:
        logger.warning(
            "Reply POST failed HTTP %d for %s/%s: %s",
            e.code, robot_uuid, msg_id[:8],
            e.read().decode()[:200],
        )
        return False
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        logger.warning("Reply POST error for %s/%s: %s", robot_uuid, msg_id[:8], e)
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main():
    logger.info(
        "robot-responder started — polling %s every %.1fs → %s",
        HOST_LISTENER_URL,
        POLL_INTERVAL,
        GATEWAY_CHAT_URL,
    )
    logger.info("Gateway auth: %s", "configured" if GATEWAY_TOKEN else "MISSING — set GATEWAY_TOKEN or GATEWAY_HOOK_TOKEN env, or gateway.auth.token in config")

    while not _shutdown:
        try:
            pending = fetch_pending()

            for msg in pending:
                msg_id = msg.get("msg_id", "")
                if msg_id in _seen:
                    continue
                _seen.add(msg_id)

                robot_uuid = msg.get("robot_uuid", "unknown").rstrip("\x00")
                message = msg.get("message", {})
                text = message.get("text", "")

                logger.info(
                    "New message from %s: %.100s",
                    robot_uuid,
                    text[:100],
                )

                process_message(message, robot_uuid, msg_id)

        except Exception:
            logger.exception("Unexpected error in main loop")

        if not _shutdown:
            time.sleep(POLL_INTERVAL)

    logger.info("robot-responder stopped")


if __name__ == "__main__":
    main()
