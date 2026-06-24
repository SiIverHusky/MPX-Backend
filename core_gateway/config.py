from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True)
class IngressConfig:
    """Top-level ingress gateway configuration."""

    host: str = os.getenv("CORE_GATEWAY_HOST", "0.0.0.0")
    port: int = int(os.getenv("CORE_GATEWAY_PORT", "8080"))
    workers: int = int(os.getenv("CORE_GATEWAY_WORKERS", "1"))


# Singleton
settings = IngressConfig()
