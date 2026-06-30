from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class IngressConfig:
    """Top-level ingress gateway configuration."""

    host: str = os.getenv("CORE_GATEWAY_HOST", "0.0.0.0")
    port: int = int(os.getenv("CORE_GATEWAY_PORT", "8080"))
    workers: int = int(os.getenv("CORE_GATEWAY_WORKERS", "1"))
    keepalive_interval: float = float(os.getenv("CORE_GATEWAY_KEEPALIVE_INTERVAL", "3.0"))


@dataclass(slots=True)
class OpenClawConfig:
    """Configuration for the OpenClaw autonomous agent client."""

    base_url: str = os.getenv("OPENCLAW_BASE_URL", "http://localhost:9090")
    api_key: str = os.getenv("OPENCLAW_API_KEY", "")
    request_timeout: float = float(os.getenv("OPENCLAW_TIMEOUT", "15.0"))
    max_retries: int = int(os.getenv("OPENCLAW_MAX_RETRIES", "2"))


# Singletons
settings = IngressConfig()
openclaw_settings = OpenClawConfig()
