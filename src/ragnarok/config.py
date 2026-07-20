from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class DatasetConfig(BaseModel):
    path: Path = Path("dataset/dataset.csv")
    knowledge_base_dir: Path = Path("knowledge_base")


class RagConfig(BaseModel):
    """The single, fixed RAG profile used by every run."""

    chunk_size: int = 900
    chunk_overlap: int = 120
    top_k: int = 4
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    cache_dir: Path = Path(".ragnarok/cache")


class AuthenticationConfig(BaseModel):
    type: Literal["none", "bearer", "header"] = "none"
    credential_id: str | None = None
    header_name: str = "Authorization"


class ModelConfig(BaseModel):
    id: str
    adapter: Literal["ollama", "openai", "anthropic", "custom_http"]
    model: str
    base_url: str | None = None
    endpoint: str | None = None
    credential_id: str | None = None
    temperature: float = 0
    max_output_tokens: int = 1000
    timeout_seconds: float = 120
    authentication: AuthenticationConfig = Field(default_factory=AuthenticationConfig)
    response_text_path: str = "response"


class RuntimeConfig(BaseModel):
    retries: int = Field(2, ge=0, le=10)
    retry_backoff_seconds: float = Field(0.25, ge=0)


class AppConfig(BaseModel):
    models: list[ModelConfig]
    dataset: DatasetConfig = Field(default_factory=DatasetConfig)
    rag: RagConfig = Field(default_factory=RagConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    system_prompt_path: Path = Path("prompts/default_system_prompt.txt")
    output_dir: Path = Path("outputs")

    @model_validator(mode="after")
    def validate_models(self):
        if not self.models:
            raise ValueError("at least one model is required")
        if len({model.id for model in self.models}) != len(self.models):
            raise ValueError("model ids must be unique")
        return self


def config_from_data(data: dict, root: Path | None = None) -> AppConfig:
    base = (root or Path.cwd()).resolve()
    config = AppConfig.model_validate(data)
    for owner, name in (
        (config.dataset, "path"),
        (config.dataset, "knowledge_base_dir"),
        (config.rag, "cache_dir"),
        (config, "system_prompt_path"),
        (config, "output_dir"),
    ):
        value = getattr(owner, name)
        if not value.is_absolute():
            setattr(owner, name, (base / value).resolve())
    return config
