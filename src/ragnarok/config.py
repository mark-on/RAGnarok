from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    temperature: float = Field(0, ge=0, le=2)
    max_output_tokens: int = Field(1000, ge=1, le=4096)
    timeout_seconds: float = Field(120, gt=0, le=600)
    reasoning_enabled: bool | None = None
    authentication: AuthenticationConfig = Field(default_factory=AuthenticationConfig)
    response_text_path: str = "response"

    @model_validator(mode="after")
    def default_openrouter_reasoning(self):
        """Keep OpenRouter evaluation calls deterministic and cost-conscious."""
        if self.reasoning_enabled is None and "openrouter.ai" in (self.base_url or "").lower():
            self.reasoning_enabled = False
        return self


class RuntimeConfig(BaseModel):
    retries: int = Field(2, ge=0, le=10)
    retry_backoff_seconds: float = Field(0.25, ge=0)
    subject_concurrency: Literal[1] = 1
    judge_concurrency: int = Field(4, ge=1, le=32)
    postprocess_workers: int = Field(0, ge=0, le=32)


class BenchmarkSelection(BaseModel):
    id: str
    options: dict[str, object] = Field(default_factory=dict)
    judge: ModelConfig | None = None
    attacker: ModelConfig | None = None


class AppConfig(BaseModel):
    benchmarks: list[BenchmarkSelection]
    models: list[ModelConfig]
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    output_dir: Path = Path("outputs")

    @model_validator(mode="after")
    def validate_models(self):
        if not self.benchmarks:
            raise ValueError("at least one benchmark is required")
        if len({benchmark.id for benchmark in self.benchmarks}) != len(self.benchmarks):
            raise ValueError("benchmark ids must be unique")
        if not self.models:
            raise ValueError("at least one model is required")
        if len({model.id for model in self.models}) != len(self.models):
            raise ValueError("model ids must be unique")
        return self


def config_from_data(data: dict, root: Path | None = None) -> AppConfig:
    base = (root or Path.cwd()).resolve()
    # Read the v0.2 single-benchmark format so saved configurations remain usable.
    if "benchmarks" not in data and "benchmark" in data:
        data = {**data, "benchmarks": [data["benchmark"]]}
    config = AppConfig.model_validate(data)
    if not config.output_dir.is_absolute():
        config.output_dir = (base / config.output_dir).resolve()
    return config
