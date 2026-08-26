from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..config import ModelConfig, RuntimeConfig
from ..core.benchmark import ProgressCallback
from ..files import safe_name, sha256_file, sha256_text, write_json
from ..models import provider_for
from ..schemas import ChatMessage, ProviderRequest


def recoverable_run_dir(
    output_dir: Path,
    benchmark_id: str,
    model_ids: list[str],
) -> Path | None:
    """Return the newest unfinished native run for the requested models."""

    benchmark_dir = output_dir / benchmark_id
    if not benchmark_dir.is_dir():
        return None
    for candidate in sorted(
        (path for path in benchmark_dir.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    ):
        if (candidate / "run_manifest.json").is_file():
            continue
        if all((candidate / safe_name(model_id)).is_dir() for model_id in model_ids):
            return candidate
    return None


def ensure_pinned_repository(url: str, commit: str, destination: Path) -> None:
    """Clone a public upstream repository anonymously and pin its exact revision."""

    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if not (destination / ".git").is_dir():
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", url, str(destination)], check=True, env=environment)
    subprocess.run(
        ["git", "-c", f"safe.directory={destination.as_posix()}", "-C", str(destination), "fetch", "--quiet", "origin", commit],
        check=True,
        env=environment,
    )
    subprocess.run(
        ["git", "-c", f"safe.directory={destination.as_posix()}", "-C", str(destination), "checkout", "--quiet", "--detach", commit],
        check=True,
        env=environment,
    )


class JudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    harm_types: list[Literal["H1", "H2", "H3", "H4", "H5"]]
    severity: int = Field(ge=0, le=4)
    attack_success: bool
    refusal_correctness: Literal["correct_refusal", "incorrect_refusal", "not_refusal"]
    rationale_short: str


class ModelCallError(RuntimeError):
    """A recorded model-call failure with a machine-readable provider error type."""

    def __init__(self, role: str, call_index: int, error_type: str, message: str):
        self.error_type = error_type
        self.call_index = call_index
        super().__init__(f"{role} call {call_index} failed ({error_type}): {message}")


def extract_json_object(value: str) -> dict:
    decoder = json.JSONDecoder()
    for position, character in enumerate(value):
        if character != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(value[position:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("response does not contain a valid JSON object")


class ModelCallSession:
    """Auditable provider bridge with serial sync and concurrent async calls."""

    def __init__(
        self,
        model: ModelConfig,
        runtime: RuntimeConfig,
        log_path: Path,
        *,
        role: str,
        total: int,
        progress: ProgressCallback | None,
    ):
        self.model = model
        self.runtime = runtime
        self.provider = provider_for(model, runtime)
        self.role = role
        self.total = total
        self.progress = progress
        self.calls = 0
        self.completed = 0
        self.errors = 0
        self.output_tokens = 0
        self.input_tokens = 0
        self.wall_seconds = 0.0
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.is_file():
            with log_path.open(encoding="utf-8") as existing:
                for line in existing:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self.calls = max(self.calls, int(row.get("call_index") or 0))
                    failed = bool(row.get("error_type"))
                    self.completed += int(not failed)
                    self.errors += int(failed)
                    self.output_tokens += int(row.get("output_tokens") or 0)
                    self.input_tokens += int(row.get("input_tokens") or 0)
                    self.wall_seconds += float(row.get("wall_duration_seconds") or 0.0)
        self.handle = log_path.open("a", encoding="utf-8", buffering=1024 * 1024)

    def _request(
        self,
        *,
        system_prompt: str | None,
        user_prompt: str,
        temperature: float,
        max_output_tokens: int,
        stop_sequences: list[str] | None = None,
        response_schema: dict | None = None,
    ) -> ProviderRequest:
        return ProviderRequest(
            system_prompt=system_prompt,
            conversation_messages=[ChatMessage(role="user", content=user_prompt)],
            model=self.model.model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            stop_sequences=stop_sequences or [],
            response_schema=response_schema,
            timeout=self.model.timeout_seconds,
        )

    def _record(
        self,
        *,
        result,
        elapsed: float,
        system_prompt: str | None,
        user_prompt: str,
        temperature: float,
        max_output_tokens: int,
        metadata: dict[str, object] | None,
    ) -> str:
        """Record one completed call; async callers invoke this without yielding."""
        self.calls += 1
        self.wall_seconds += elapsed
        self.errors += int(bool(result.error_type))
        self.completed += int(not result.error_type)
        self.output_tokens += result.output_tokens or 0
        self.input_tokens += result.input_tokens or 0
        row = {
            "call_index": self.calls,
            "phase": self.role,
            "raw_prompt": user_prompt,
            "system_prompt_sha256": sha256_text(system_prompt or ""),
            "response": result.response_text,
            "provider": result.provider,
            "model": result.model,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "wall_duration_seconds": elapsed,
            "error_type": result.error_type,
            "error_message": result.error_message,
            "runtime_metadata": result.runtime_metadata,
            "metadata": metadata or {},
        }
        self.handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        if self.calls % 10 == 0 or result.error_type:
            self.handle.flush()
        if self.progress:
            rate = self.output_tokens / self.wall_seconds if self.output_tokens and self.wall_seconds else None
            remaining = max(self.total - self.completed, 0)
            eta = self.wall_seconds / self.completed * remaining if self.completed else None
            self.progress(
                self.role,
                self.completed,
                self.total,
                f"{self.model.model}: {self.role} case {self.completed}/{self.total}",
                {"tokens_per_second": rate, "eta_seconds": eta},
            )
        if result.error_type:
            raise ModelCallError(
                self.role,
                self.calls,
                result.error_type,
                result.error_message or "no error detail",
            )
        return result.response_text

    async def generate_async(
        self,
        *,
        system_prompt: str | None,
        user_prompt: str,
        temperature: float,
        max_output_tokens: int,
        metadata: dict[str, object] | None = None,
        stop_sequences: list[str] | None = None,
        response_schema: dict | None = None,
    ) -> str:
        request = self._request(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            stop_sequences=stop_sequences,
            response_schema=response_schema,
        )
        started = time.perf_counter()
        result = await self.provider.generate(request)
        return self._record(
            result=result,
            elapsed=time.perf_counter() - started,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            metadata=metadata,
        )

    def generate(
        self,
        *,
        system_prompt: str | None,
        user_prompt: str,
        temperature: float,
        max_output_tokens: int,
        metadata: dict[str, object] | None = None,
        stop_sequences: list[str] | None = None,
        response_schema: dict | None = None,
    ) -> str:
        request = self._request(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            stop_sequences=stop_sequences,
            response_schema=response_schema,
        )
        started = time.perf_counter()
        if hasattr(self.provider, "generate_sync"):
            result = self.provider.generate_sync(request)
        else:
            result = asyncio.run(self.provider.generate(request))
        return self._record(
            result=result,
            elapsed=time.perf_counter() - started,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            metadata=metadata,
        )

    def summary(self, *, concurrency: int = 1) -> dict[str, object]:
        return {
            "role": self.role,
            "model": self.model.model_dump(mode="json", exclude={"credential_id", "authentication"}),
            "calls": self.calls,
            "completed_calls": self.completed,
            "errors": self.errors,
            "output_tokens": self.output_tokens,
            "input_tokens": self.input_tokens,
            "wall_duration_seconds": self.wall_seconds,
            "inference_workers": concurrency,
        }

    def flush(self) -> None:
        self.handle.flush()

    def close(self) -> None:
        self.handle.flush()
        self.handle.close()
        if hasattr(self.provider, "close_sync"):
            self.provider.close_sync()

    async def aclose(self) -> None:
        self.handle.flush()
        self.handle.close()
        await self.provider.aclose()
