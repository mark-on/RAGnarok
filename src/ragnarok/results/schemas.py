from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UniversalCase(BaseModel):
    suite_id: str
    benchmark_id: str
    model_id: str
    case_id: str
    attack_family: str
    prompt: str
    response: str
    target: str | None = None
    payload: str | None = None
    baseline_response: str | None = None
    reference_answer: str | None = None
    adversarial_answer: str | None = None
    retrieved_contexts: list[str] = Field(default_factory=list)
    injected_contexts: list[str] = Field(default_factory=list)
    official_evaluation: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
