from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


OUTPUT_COLUMNS = [
    "case_id",
    "conversation_id",
    "turn_index",
    "is_continuation",
    "prompt",
    "is_attack",
    "attack_vector",
    "expected_behavior",
    "success_criteria",
    "evaluation_target",
    "model_name",
    "model_provider",
    "retrieved_sources",
    "response",
    "status",
    "error",
]


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ExtractedUnit(BaseModel):
    document_path: str
    document_id: str = "unknown"
    page_number: int | None = None
    extracted_surface: Literal["body", "metadata"]
    content: str
    content_hash: str


class Chunk(BaseModel):
    chunk_id: str
    document_path: str
    document_id: str
    page_number: int | None
    extracted_surface: Literal["body", "metadata"]
    content: str
    content_hash: str


class RetrievalHit(BaseModel):
    rank: int
    chunk_id: str
    document_path: str
    document_id: str
    page_number: int | None
    extracted_surface: Literal["body", "metadata"]
    similarity_score: float
    content: str


class ProviderRequest(BaseModel):
    system_prompt: str
    conversation_messages: list[ChatMessage]
    model: str
    temperature: float = 0
    max_output_tokens: int = 1000
    timeout: float = 120


class ProviderResult(BaseModel):
    response_text: str = ""
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_type: str = ""
    error_message: str = ""
