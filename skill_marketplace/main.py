from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator
from uuid import uuid4

from fastapi import FastAPI, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import String, Enum, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# -----------------------------------------------------------------------------
# Configuration & Storage Contracts
# -----------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://mpx_user:mpx_password@mpx_db:5432/mpx_marketplace")

@dataclass(slots=True)
class MarketplaceStorage:
    root: Path
    signing_secret: str

    def signed_local_url(self, object_name: str, expires_in_seconds: int) -> dict[str, str]:
        token_source = f"{object_name}:{expires_in_seconds}:{self.signing_secret}".encode("utf-8")
        signature = hashlib.sha256(token_source).hexdigest()
        target = self.root / object_name
        return {
            "signed_url": f"http://127.0.0.1:8080/storage/{object_name}?expires={expires_in_seconds}&signature={signature}",
            "local_path": str(target),
            "expires_in_seconds": str(expires_in_seconds),
        }

storage = MarketplaceStorage(
    root=Path(os.getenv("MARKETPLACE_STORAGE_ROOT", "/tmp/mpx-marketplace-storage")),
    signing_secret=os.getenv("MARKETPLACE_SIGNING_SECRET", "change-me"),
)

# -----------------------------------------------------------------------------
# Database Setup & Declarative Schemas
# -----------------------------------------------------------------------------
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True, future=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

class Base(DeclarativeBase):
    pass

class SkillModel(Base):
    __tablename__ = "marketplace_skills"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    execution_type: Mapped[str] = mapped_column(String(50), nullable=False)  # 'cloud_faas' or 'edge_wasm'
    tool_schema: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)

class EntitlementModel(Base):
    __tablename__ = "user_entitlements"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()"))
    robot_uuid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    skill_id: Mapped[str] = mapped_column(UUID(as_uuid=False), nullable=False)
    entitlement_hash: Mapped[str] = mapped_column(String(256), nullable=False)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session

# -----------------------------------------------------------------------------
# Pydantic Structural Contracts
# -----------------------------------------------------------------------------
class SkillCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    execution_type: str = Field(..., pattern="^(cloud_faas|edge_wasm)$")
    tool_schema: dict[str, Any] = Field(...)
    storage_path: str = Field(..., min_length=1, max_length=512)

class EntitlementRequest(BaseModel):
    robot_uuid: str = Field(..., min_length=1, max_length=64)
    skill_id: str = Field(...)

# -----------------------------------------------------------------------------
# API Bootstrap
# -----------------------------------------------------------------------------
app = FastAPI(title="MPX Skill Marketplace Engine", version="1.0.0")

@app.on_event("startup")
async def startup_event():
    # Automatically initialize schemas if running in isolated local/hacker loops
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "skill_marketplace"}

@app.post("/v1/skills", status_code=status.HTTP_201_CREATED)
async def create_skill(request: SkillCreateRequest, db: AsyncSession = Depends(get_db)):
    new_skill = SkillModel(
        title=request.title,
        execution_type=request.execution_type,
        tool_schema=request.tool_schema,
        storage_path=request.storage_path
    )
    db.add(new_skill)
    await db.commit()
    await db.refresh(new_skill)
    return {"status": "created", "skill_id": new_skill.id}

@app.get("/v1/skills")
async def list_skills(db: AsyncSession = Depends(get_db)):
    # Simple stateless query mapping for the discovery engine
    result = await db.execute(text("SELECT id, title, execution_type, tool_schema FROM marketplace_skills"))
    skills = [
        {"id": row[0], "title": row[1], "execution_type": row[2], "tool_schema": row[3]}
        for row in result.fetchall()
    ]
    return {"skills": skills}

@app.get("/v1/skills/{skill_id}")
async def get_skill(skill_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(text("SELECT id, title, execution_type, tool_schema FROM marketplace_skills WHERE id = :id"), {"id": skill_id})
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Requested hardware capability package not found")
    return {"id": row[0], "title": row[1], "execution_type": row[2], "tool_schema": row[3]}

@app.post("/v1/skills/entitlements")
async def issue_entitlement(request: EntitlementRequest, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    # 1. Validate that the targeted skill actually exists inside the marketplace registry
    skill_check = await db.execute(text("SELECT id, storage_path FROM marketplace_skills WHERE id = :id"), {"id": request.skill_id})
    skill = skill_check.fetchone()
    if not skill:
        raise HTTPException(status_code=400, detail="Cannot entitlement gate an unregistered skill resource")

    # 2. Compute a structural entitlement validation hash mapping back to hardware identity curve bindings
    raw_hash_source = f"{request.robot_uuid}:{request.skill_id}:{storage.signing_secret}".encode("utf-8")
    entitlement_hash = hashlib.sha256(raw_hash_source).hexdigest()

    # 3. Log the purchase entitlement block into PostgreSQL
    new_entitlement = EntitlementModel(
        robot_uuid=request.robot_uuid,
        skill_id=request.skill_id,
        entitlement_hash=entitlement_hash
    )
    db.add(new_entitlement)
    await db.commit()
    await db.refresh(new_entitlement)

    # 4. Generate the short-lived 60-second storage obscurant mapping configuration
    artifact_name = Path(skill[1]).name
    signed = storage.signed_local_url(artifact_name, expires_in_seconds=60)

    return {
        "status": "accepted",
        "entitlement_id": new_entitlement.id,
        "robot_uuid": request.robot_uuid,
        "skill_id": request.skill_id,
        "entitlement_hash": entitlement_hash,
        "artifact": signed,
    }