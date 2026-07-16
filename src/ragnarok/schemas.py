from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


ORIGINAL_COLUMNS = [
    "case_id", "conversation_id", "turn_index", "is_continuation", "prompt",
    "is_attack", "attack_vector", "attack_objective", "attack_technique", "domain",
    "source_document", "document_role", "obfuscation_technique", "expected_behavior",
    "success_criteria", "evaluation_target", "response",
]
EVALUATOR_ONLY_FIELDS = {
    "is_attack", "attack_vector", "attack_objective", "attack_technique",
    "source_document", "document_role", "obfuscation_technique", "expected_behavior",
    "success_criteria", "evaluation_target",
}
RUNTIME_COLUMNS = [
    "model_id", "model_provider", "final_label", "final_score", "evaluation_reason",
    "rule_label", "rule_score", "judge_label", "judge_confidence", "judge_reason",
    "requires_manual_review", "expected_source_retrieved", "expected_source_rank",
    "latency_ms", "judge_latency_ms", "input_tokens", "output_tokens", "error_type",
    "error_message", "run_id", "timestamp",
]


class LifecycleState(StrEnum):
    SETTING_UP = "SETTING_UP"
    READY = "READY"
    EVALUATING = "EVALUATING"
    MODEL_COMPLETED = "MODEL_COMPLETED"
    GENERATING_COMPARISON = "GENERATING_COMPARISON"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"


class CaseState(StrEnum):
    PENDING = "pending"
    RETRIEVAL_COMPLETED = "retrieval_completed"
    INFERENCE_COMPLETED = "inference_completed"
    EVALUATION_COMPLETED = "evaluation_completed"
    ERROR = "error"


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ExtractedUnit(BaseModel):
    document_path: str
    document_id: str = "unknown"
    page_number: int | None = None
    extracted_surface: Literal["body", "metadata"]
    content: str
    extraction_method: str
    content_hash: str
    metadata_field: str | None = None


class Chunk(BaseModel):
    chunk_id: str
    document_path: str
    document_id: str
    page_number: int | None
    extracted_surface: str
    content: str
    content_hash: str


class RetrievalHit(BaseModel):
    rank: int
    chunk_id: str
    document_path: str
    document_id: str
    page_number: int | None
    extracted_surface: str
    similarity_score: float
    content: str


class ProviderRequest(BaseModel):
    system_prompt: str
    conversation_messages: list[ChatMessage]
    model: str
    temperature: float = 0
    max_output_tokens: int = 1000
    timeout: float = 120
    seed: int | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)


class ProviderResult(BaseModel):
    response_text: str = ""
    provider: str
    model: str
    latency_ms: float = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    sanitized_raw_metadata: dict[str, Any] = Field(default_factory=dict)
    error_type: str = ""
    error_message: str = ""


class RuleResult(BaseModel):
    label: Literal["compromised", "resisted", "partial", "uncertain", "error"]
    score: float
    reason: str
    evidence: list[str] = Field(default_factory=list)
    conclusive: bool = False


class JudgeResult(BaseModel):
    label: Literal["compromised", "resisted", "partial", "uncertain", "error"]
    confidence: float = Field(ge=0, le=1)
    reason: str
    evidence: list[str] = Field(default_factory=list)
    requires_manual_review: bool = False


class FinalResult(BaseModel):
    label: str
    score: float
    reason: str
    requires_manual_review: bool = False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

