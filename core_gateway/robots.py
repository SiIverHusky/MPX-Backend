"""Robot management endpoints — register robots and assign skills."""

from __future__ import annotations

import logging

import asyncpg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("core_gateway.robots")

router = APIRouter(tags=["robots"])

# get_pg_pool is injected by main.py at import time
_get_pg_pool = None


def init(pool_getter):
    global _get_pg_pool
    _get_pg_pool = pool_getter


async def get_pool():
    return await _get_pg_pool()


@router.post("/v1/robots")
async def create_robot(request: Request) -> JSONResponse:
    """Register a new robot.

    Body::
        {"robot_uuid": "MPX-DOG-02", "aes_key_hex": "<64 hex chars>"}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    robot_uuid = (body.get("robot_uuid", "") or "").strip()
    aes_key_hex = (body.get("aes_key_hex", "") or "").strip()

    if not robot_uuid or not aes_key_hex:
        return JSONResponse(status_code=422, content={"error": "robot_uuid and aes_key_hex required"})

    if len(aes_key_hex) != 64:
        return JSONResponse(
            status_code=422,
            content={"error": "aes_key_hex must be exactly 64 hex chars (32 bytes)"},
        )

    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO robots (robot_uuid, aes_key_hex) VALUES ($1, $2)",
                robot_uuid, aes_key_hex,
            )
    except asyncpg.UniqueViolationError:
        return JSONResponse(status_code=409, content={"error": f"robot '{robot_uuid}' already exists"})

    logger.info("Registered robot: %s", robot_uuid)
    return JSONResponse(content={"status": "created", "robot_uuid": robot_uuid}, status_code=201)


@router.get("/v1/robots")
async def list_robots() -> JSONResponse:
    """List all registered robots."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT robot_uuid, created_at, last_seen_at FROM robots ORDER BY created_at DESC",
        )
    robots = [
        {
            "robot_uuid": row["robot_uuid"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
        }
        for row in rows
    ]
    return JSONResponse(content=robots)


@router.get("/v1/robots/{robot_uuid}")
async def get_robot(robot_uuid: str) -> JSONResponse:
    """Get a robot's info."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT robot_uuid, created_at, last_seen_at FROM robots WHERE robot_uuid = $1",
            robot_uuid,
        )
    if row is None:
        return JSONResponse(status_code=404, content={"error": "robot not found"})

    return JSONResponse(content={
        "robot_uuid": row["robot_uuid"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "last_seen_at": row["last_seen_at"].isoformat() if row["last_seen_at"] else None,
    })


@router.get("/v1/robots/{robot_uuid}/skills")
async def get_robot_skills(robot_uuid: str) -> JSONResponse:
    """List enabled skills for a robot."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        robot = await conn.fetchrow(
            "SELECT robot_uuid FROM robots WHERE robot_uuid = $1", robot_uuid,
        )
        if robot is None:
            return JSONResponse(status_code=404, content={"error": "robot not found"})

        rows = await conn.fetch(
            """SELECT rs.skill_id, rs.enabled, rs.assigned_at,
                      ms.title, ms.skill_type, ms.current_version
               FROM robot_skills rs
               JOIN marketplace_skills ms ON ms.id = rs.skill_id
               WHERE rs.robot_uuid = $1
               ORDER BY rs.enabled DESC, rs.assigned_at DESC""",
            robot_uuid,
        )
    skills = [
        {
            "skill_id": row["skill_id"],
            "title": row["title"],
            "skill_type": row["skill_type"],
            "current_version": row["current_version"],
            "enabled": row["enabled"],
            "assigned_at": row["assigned_at"].isoformat() if row["assigned_at"] else None,
        }
        for row in rows
    ]
    return JSONResponse(content=skills)


@router.post("/v1/robots/{robot_uuid}/skills")
async def assign_robot_skill(robot_uuid: str, request: Request) -> JSONResponse:
    """Assign a skill to a robot.

    Body::
        {"skill_id": "haris_dev~amazon"}
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    skill_id = (body.get("skill_id", "") or "").strip()
    if not skill_id:
        return JSONResponse(status_code=422, content={"error": "skill_id required"})

    pool = await get_pool()
    async with pool.acquire() as conn:
        robot = await conn.fetchrow(
            "SELECT robot_uuid FROM robots WHERE robot_uuid = $1", robot_uuid,
        )
        if robot is None:
            return JSONResponse(status_code=404, content={"error": "robot not found"})

        skill = await conn.fetchrow(
            "SELECT id FROM marketplace_skills WHERE id = $1", skill_id,
        )
        if skill is None:
            return JSONResponse(status_code=404, content={"error": f"skill '{skill_id}' not found"})

        try:
            await conn.execute(
                "INSERT INTO robot_skills (robot_uuid, skill_id) VALUES ($1, $2)",
                robot_uuid, skill_id,
            )
        except asyncpg.UniqueViolationError:
            return JSONResponse(
                status_code=409,
                content={"error": f"skill '{skill_id}' already assigned to robot '{robot_uuid}'"},
            )

    logger.info("Skill %s assigned to robot %s", skill_id, robot_uuid)
    return JSONResponse(content={"status": "assigned", "robot_uuid": robot_uuid, "skill_id": skill_id}, status_code=201)


@router.patch("/v1/robots/{robot_uuid}/skills/{skill_id}")
async def toggle_robot_skill(robot_uuid: str, skill_id: str, request: Request) -> JSONResponse:
    """Toggle a skill's enabled status on a robot.

    Body::
        {"enabled": true}   # or false
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return JSONResponse(status_code=422, content={"error": "'enabled' must be a boolean"})

    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE robot_skills SET enabled = $1 WHERE robot_uuid = $2 AND skill_id = $3",
            enabled, robot_uuid, skill_id,
        )
    if result == "UPDATE 0":
        return JSONResponse(status_code=404, content={"error": "assignment not found"})

    status = "enabled" if enabled else "disabled"
    logger.info("Skill %s %s for robot %s", skill_id, status, robot_uuid)
    return JSONResponse(content={
        "status": status,
        "robot_uuid": robot_uuid,
        "skill_id": skill_id,
        "enabled": enabled,
    })


@router.delete("/v1/robots/{robot_uuid}/skills/{skill_id}")
async def remove_robot_skill(robot_uuid: str, skill_id: str) -> JSONResponse:
    """Permanently remove a skill assignment from a robot."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM robot_skills WHERE robot_uuid = $1 AND skill_id = $2",
            robot_uuid, skill_id,
        )
    if result == "DELETE 0":
        return JSONResponse(status_code=404, content={"error": "assignment not found"})

    logger.info("Skill %s removed from robot %s", skill_id, robot_uuid)
    return JSONResponse(content={"status": "removed", "robot_uuid": robot_uuid, "skill_id": skill_id})
