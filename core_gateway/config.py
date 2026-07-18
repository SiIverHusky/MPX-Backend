from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class IngressConfig:
    """Top-level ingress gateway configuration."""

    host: str = os.getenv("CORE_GATEWAY_HOST", "0.0.0.0")
    port: int = int(os.getenv("CORE_GATEWAY_PORT", "8080"))
    workers: int = int(os.getenv("CORE_GATEWAY_WORKERS", "1"))



@dataclass(slots=True)
class OpenClawConfig:
    """Configuration for the OpenClaw autonomous agent client."""

    base_url: str = os.getenv("OPENCLAW_BASE_URL", "http://localhost:9090")
    api_key: str = os.getenv("OPENCLAW_API_KEY", "")
    request_timeout: float = float(os.getenv("OPENCLAW_TIMEOUT", "15.0"))
    max_retries: int = int(os.getenv("OPENCLAW_MAX_RETRIES", "2"))


@dataclass(slots=True)
class DatabaseConfig:
    """Configuration for PostgreSQL (Cloud SQL emulation)."""

    host: str = os.getenv("DB_HOST", "localhost")
    port: int = int(os.getenv("DB_PORT", "5432"))
    user: str = os.getenv("DB_USER", "mpx_admin")
    password: str = os.getenv("DB_PASSWORD", "")
    db_name: str = os.getenv("DB_NAME", "mpx_marketplace_prod")
    jwt_secret: str = os.getenv("JWT_SECRET", "fallback_dev_secret")


@dataclass(slots=True)
class StorageConfig:
    """Configuration for GCS (Cloud Storage emulation)."""

    endpoint: str = os.getenv("GCS_ENDPOINT", "http://localhost:4443")
    bucket: str = os.getenv("GCS_BUCKET", "mpx-marketplace-artifacts")


# Singletons
settings = IngressConfig()
openclaw_settings = OpenClawConfig()
db_settings = DatabaseConfig()
storage_settings = StorageConfig()
