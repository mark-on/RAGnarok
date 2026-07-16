from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class ProjectConfig(BaseModel):
    name: str = "RAGnarok evaluation"
    experiment_id: str | None = None
    output_dir: Path = Path("outputs")


class DatasetConfig(BaseModel):
    path: Path = Path("dataset/dataset.csv")
    knowledge_base_dir: Path = Path("knowledge_base")


class PdfExtractionConfig(BaseModel):
    policy: Literal["body_only", "body_and_metadata", "metadata_only"] = "body_and_metadata"
    metadata_fields: list[str] = Field(default_factory=lambda: ["Title", "Author", "Subject", "Keywords", "Creator", "Producer", "IndexingNote"])


class RagConfig(BaseModel):
    chunk_size: int = Field(900, ge=100)
    chunk_overlap: int = Field(120, ge=0)
    top_k: int = Field(4, ge=1)
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_backend: Literal["sentence_transformers", "mock"] = "sentence_transformers"
    cache_dir: Path = Path(".ragnarok/cache")

    @model_validator(mode="after")
    def overlap_is_smaller(self):
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap must be smaller than chunk_size")
        return self


class AuthenticationConfig(BaseModel):
    type: Literal["none", "bearer", "header"] = "none"
    token_env: str | None = None
    credential_id: str | None = None
    header_name: str = "Authorization"


class PollingConfig(BaseModel):
    enabled: bool = False
    status_url_path: str | None = None
    interval_seconds: float = 1
    max_attempts: int = 30


class ModelConfig(BaseModel):
    id: str
    adapter: Literal["mock", "ollama", "openai_compatible", "custom_http"]
    model: str = "mock"
    base_url: str | None = None
    endpoint: str | None = None
    api_key_env: str | None = None
    credential_id: str | None = None
    temperature: float = 0
    max_output_tokens: int = 1000
    timeout_seconds: float = 120
    seed: int | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)
    method: Literal["GET", "POST", "PUT"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    authentication: AuthenticationConfig = Field(default_factory=AuthenticationConfig)
    request_mapping: dict[str, str] = Field(default_factory=dict)
    response_text_path: str = "response"
    input_tokens_path: str | None = None
    output_tokens_path: str | None = None
    error_path: str | None = None
    polling: PollingConfig = Field(default_factory=PollingConfig)


class JudgeConfig(BaseModel):
    enabled: bool = False
    confidence_threshold: float = Field(0.7, ge=0, le=1)
    model: ModelConfig | None = None


class EvaluationConfig(BaseModel):
    system_prompt_path: Path = Path("prompts/default_system_prompt.txt")


class RuntimeConfig(BaseModel):
    checkpoint_every: int = Field(10, ge=1)
    retries: int = Field(2, ge=0, le=10)
    retry_backoff_seconds: float = Field(0.25, ge=0)
    seed: int = 42


class ReportingConfig(BaseModel):
    redact_targets: bool = True
    generate_pdf: bool = True
    generate_charts: bool = True


class AppConfig(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    dataset: DatasetConfig = Field(default_factory=DatasetConfig)
    pdf_extraction: PdfExtractionConfig = Field(default_factory=PdfExtractionConfig)
    rag: RagConfig = Field(default_factory=RagConfig)
    models: list[ModelConfig]
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    evaluation: EvaluationConfig = Field(default_factory=EvaluationConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)

    @model_validator(mode="after")
    def unique_models(self):
        ids = [model.id for model in self.models]
        if not ids:
            raise ValueError("at least one inference model is required")
        if len(ids) != len(set(ids)):
            raise ValueError("model ids must be unique")
        return self


def load_config(path: Path, root: Path | None = None) -> AppConfig:
    path = path.resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = AppConfig.model_validate(data)
    base = (root or Path.cwd()).resolve()
    config.project.output_dir = (base / config.project.output_dir).resolve() if not config.project.output_dir.is_absolute() else config.project.output_dir
    config.dataset.path = (base / config.dataset.path).resolve() if not config.dataset.path.is_absolute() else config.dataset.path
    config.dataset.knowledge_base_dir = (base / config.dataset.knowledge_base_dir).resolve() if not config.dataset.knowledge_base_dir.is_absolute() else config.dataset.knowledge_base_dir
    config.rag.cache_dir = (base / config.rag.cache_dir).resolve() if not config.rag.cache_dir.is_absolute() else config.rag.cache_dir
    config.evaluation.system_prompt_path = (base / config.evaluation.system_prompt_path).resolve() if not config.evaluation.system_prompt_path.is_absolute() else config.evaluation.system_prompt_path
    return config


def configuration_hash(config: AppConfig) -> str:
    safe = config.model_dump(mode="json")
    for model in safe["models"]:
        model.pop("headers", None)
    return hashlib.sha256(yaml.safe_dump(safe, sort_keys=True).encode()).hexdigest()
