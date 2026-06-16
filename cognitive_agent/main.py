from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

try:
    from openclaw import ClawAgent, OpenAILogic  # type: ignore
except Exception:  # pragma: no cover - optional dependency stub
    @dataclass(slots=True)
    class OpenAILogic:  # type: ignore[no-redef]
        model: str = "stub"

        def route(self, context: dict[str, Any]) -> dict[str, Any]:
            return {"route": "fallback", "context": context}

    @dataclass(slots=True)
    class ClawAgent:  # type: ignore[no-redef]
        logic: OpenAILogic = field(default_factory=OpenAILogic)

        def reason(self, context: dict[str, Any]) -> dict[str, Any]:
            return self.logic.route(context)


class ReasonRequest(BaseModel):
    context: dict[str, Any] = Field(default_factory=dict)


app = FastAPI(title="MPX Cognitive Agent", version="1.0.0")
agent = ClawAgent()


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "cognitive_agent"}


@app.post("/v1/reason")
async def reason(request: ReasonRequest) -> dict[str, Any]:
    result = agent.reason(request.context)
    return {"status": "accepted", "result": result, "isolated_lifecycle": True}
