from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ProviderRequest(BaseModel):
    system_prompt: str | None = None
    conversation_messages: list[ChatMessage]
    model: str
    temperature: float = Field(0, ge=0, le=2)
    max_output_tokens: int = Field(1000, ge=1, le=4096)
    stop_sequences: list[str] = Field(default_factory=list, max_length=8)
    response_schema: dict[str, Any] | None = None
    timeout: float = Field(120, gt=0, le=600)


class ProviderResult(BaseModel):
    response_text: str = ""
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    error_type: str = ""
    error_message: str = ""
    runtime_metadata: dict[str, Any] = Field(default_factory=dict)
