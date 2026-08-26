from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from ..config import ModelConfig, RuntimeConfig


class ProgressCallback(Protocol):
    def __call__(
        self,
        phase: str,
        current: int,
        total: int | None,
        detail: str,
        stats: dict[str, object] | None = None,
    ) -> None: ...


@dataclass(frozen=True)
class BenchmarkInfo:
    id: str
    name: str
    upstream_url: str
    upstream_commit: str
    description: str
    python_extra: str = ""
    requires_judge: bool = False
    requires_attacker: bool = False


@dataclass(frozen=True)
class OptionChoice:
    label: str
    value: str


@dataclass(frozen=True)
class OptionSpec:
    key: str
    label: str
    kind: Literal["select", "integer"]
    default: str | int
    choices: tuple[OptionChoice, ...] = ()


class BenchmarkAdapter(ABC):
    @property
    @abstractmethod
    def info(self) -> BenchmarkInfo: ...

    @abstractmethod
    def option_specs(self) -> tuple[OptionSpec, ...]: ...

    @abstractmethod
    def validate_options(self, options: dict[str, object]) -> dict[str, object]: ...

    @abstractmethod
    def validate_installation(self) -> list[str]: ...

    @abstractmethod
    def estimate_model_calls(self, options: dict[str, object]) -> int: ...

    def estimate_judge_calls(self, options: dict[str, object]) -> int:
        return self.estimate_model_calls(options) if self.info.requires_judge else 0

    def estimate_attacker_calls(self, options: dict[str, object]) -> int:
        return 0

    def prepare(
        self,
        *,
        progress: ProgressCallback | None = None,
        log_path: Path | None = None,
    ) -> dict[str, object]:
        """Download and prepare deterministic assets required at run time."""

        return {}

    def validate_prepared(self) -> list[str]:
        """Report missing or stale prepared assets without modifying the system."""

        return []

    @abstractmethod
    async def run(
        self,
        *,
        options: dict[str, object],
        models: list[ModelConfig],
        runtime: RuntimeConfig,
        output_dir: Path,
        progress: ProgressCallback | None = None,
        judge: ModelConfig | None = None,
        attacker: ModelConfig | None = None,
    ) -> list[Path]: ...
