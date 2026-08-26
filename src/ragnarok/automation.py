from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx
from pydantic import BaseModel, Field, model_validator

from .config import AppConfig, BenchmarkSelection, ModelConfig, RuntimeConfig
from .interrupts import RunInterrupted
from .runner import create_result_dir, run_experiment
from .reports import validate_report_dependency
from .results import ResultStore


Progress = Callable[[str, int, int | None, str, dict[str, object] | None], None]


class AutomationSettings(BaseModel):
    prefetch: bool = True
    download_concurrency: int = Field(2, ge=1, le=4)
    cleanup_downloaded_models: bool = True
    resume: bool = True
    min_free_disk_gb: float = Field(10.0, ge=0)
    ollama_url: str = "http://localhost:11434"
    output_dir: Path = Path("outputs")
    resume_suite: Path | None = None
    sync_command: list[str] = Field(default_factory=list)


class AutomationModel(ModelConfig):
    estimated_size_gb: float = Field(0, ge=0)


class AutomationFile(BaseModel):
    version: int = 1
    automation: AutomationSettings = Field(default_factory=AutomationSettings)
    benchmarks: list[BenchmarkSelection]
    models: list[AutomationModel]
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)

    @model_validator(mode="after")
    def validate_content(self):
        if self.version != 1:
            raise ValueError(f"unsupported automation file version: {self.version}")
        if not self.models:
            raise ValueError("automation file must contain at least one enabled model")
        if len({item.id for item in self.models}) != len(self.models):
            raise ValueError("automation model ids must be unique")
        if not self.benchmarks:
            raise ValueError("automation file must contain at least one benchmark")
        return self


def load_automation(path: Path, root: Path) -> AutomationFile:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)
    raw["models"] = [item for item in raw.get("models", []) if item.get("enabled", True)]
    raw["benchmarks"] = [item for item in raw.get("benchmarks", []) if item.get("enabled", True)]
    result = AutomationFile.model_validate(raw)
    if not result.automation.output_dir.is_absolute():
        result.automation.output_dir = (root / result.automation.output_dir).resolve()
    if result.automation.resume_suite is not None and not result.automation.resume_suite.is_absolute():
        result.automation.resume_suite = (root / result.automation.resume_suite).resolve()
    return result


class OllamaModelManager:
    def __init__(self, base_url: str, progress: Progress | None = None):
        self.base_url = base_url.rstrip("/")
        self.progress = progress
        self.initial_models: set[str] = set()
        self.owned_models: set[str] = set()

    async def installed(self) -> set[str]:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.base_url}/api/tags")
            response.raise_for_status()
            return {
                str(item.get("name") or item.get("model"))
                for item in response.json().get("models", [])
                if item.get("name") or item.get("model")
            }

    async def initialize(self) -> None:
        self.initial_models = await self.installed()

    async def ensure(self, model: ModelConfig) -> None:
        if model.adapter != "ollama":
            return
        if model.model in await self.installed():
            return
        if self.progress:
            self.progress("download", 0, None, f"Downloading {model.model}", None)
        timeout = httpx.Timeout(None, connect=30)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/api/pull",
                json={"name": model.model, "stream": True},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise RuntimeError(str(event["error"]))
                    completed = int(event.get("completed") or 0)
                    total = int(event.get("total") or 0) or None
                    if self.progress:
                        self.progress("download", completed, total, f"Downloading {model.model}", None)
        self.owned_models.add(model.model)

    async def remove_if_owned(self, model: ModelConfig) -> bool:
        if model.adapter != "ollama" or model.model not in self.owned_models:
            return False
        if model.model in self.initial_models:
            return False
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request("DELETE", f"{self.base_url}/api/delete", json={"name": model.model})
            response.raise_for_status()
        self.owned_models.discard(model.model)
        return True


def _run_sync(command: list[str], suite_dir: Path) -> None:
    if not command:
        return
    expanded = [part.replace("{suite_dir}", str(suite_dir)) for part in command]
    subprocess.run(expanded, check=True, shell=False)


def preflight(configuration: AutomationFile) -> list[str]:
    from .cloud import cloud_preflight

    report = cloud_preflight(configuration)
    return [
        f"{item['name']}: {item['detail']}"
        for item in report["checks"]
        if item["required"] and not item["ok"]
    ]


async def run_automation(
    configuration: AutomationFile,
    *,
    progress: Progress | None = None,
    dry_run: bool = False,
) -> Path:
    validate_report_dependency()
    utc_started = datetime.now(timezone.utc)
    local_started = datetime.now().astimezone()
    execution_start = {
        "started_at_utc": utc_started.isoformat(),
        "started_at_local": local_started.isoformat(),
        "local_timezone": local_started.tzname(),
        "utc_offset": f"{local_started.strftime('%z')[:3]}:{local_started.strftime('%z')[3:]}",
    }
    problems = preflight(configuration)
    if problems:
        raise RuntimeError("preflight failed: " + "; ".join(problems))

    app_config = AppConfig(
        benchmarks=configuration.benchmarks,
        models=configuration.models,
        runtime=configuration.runtime,
        output_dir=configuration.automation.output_dir,
    )
    if configuration.automation.resume_suite is not None:
        suite_dir = configuration.automation.resume_suite
        if not (suite_dir / "results.sqlite").is_file():
            raise RuntimeError(f"resume suite does not contain results.sqlite: {suite_dir}")
        prior_manifest = suite_dir / "automation_manifest.json"
        if prior_manifest.is_file():
            prior = json.loads(prior_manifest.read_text(encoding="utf-8")).get("configuration", {})
            current = configuration.model_dump(mode="json")
            frozen_keys = ("models", "benchmarks", "runtime")
            if any(prior.get(key) != current.get(key) for key in frozen_keys):
                raise RuntimeError("resume suite model, benchmark, or runtime configuration does not match the automation file")
        suite = (suite_dir.name, suite_dir)
    else:
        suite = create_result_dir(app_config, prefix="automation")
    suite_id, suite_dir = suite
    if dry_run:
        (suite_dir / "automation_manifest.json").write_text(
            json.dumps({"status": "dry_run", "configuration": configuration.model_dump(mode="json")}, indent=2) + "\n",
            encoding="utf-8",
        )
        return suite_dir

    manager = OllamaModelManager(configuration.automation.ollama_url, progress)
    await manager.initialize()
    checkpoint_store = ResultStore(suite_dir)
    completed = [
        model.id for model in configuration.models
        if all(
            checkpoint_store.job_status(suite_id, selection.id, model.id) == "complete"
            for selection in configuration.benchmarks
        )
    ]
    models = [model for model in configuration.models if model.id not in completed]
    prefetch_tasks: dict[str, asyncio.Task[None]] = {}
    status = "running"
    error: str | None = None
    try:
        for index, model in enumerate(models):
            if model.id in prefetch_tasks:
                await prefetch_tasks.pop(model.id)
            else:
                free_gb = shutil.disk_usage(suite_dir).free / (1024 ** 3)
                required_gb = configuration.automation.min_free_disk_gb + model.estimated_size_gb
                if free_gb < required_gb:
                    raise RuntimeError(
                        f"{model.id} requires approximately {required_gb:.1f} GiB free including reserve; "
                        f"only {free_gb:.1f} GiB is available"
                    )
                await manager.ensure(model)

            if configuration.automation.prefetch:
                free_gb = shutil.disk_usage(suite_dir).free / (1024 ** 3)
                reserved_gb = 0.0
                for candidate in models[index + 1:]:
                    if len(prefetch_tasks) >= configuration.automation.download_concurrency:
                        break
                    if candidate.id in prefetch_tasks:
                        reserved_gb += candidate.estimated_size_gb
                        continue
                    required_gb = (
                        configuration.automation.min_free_disk_gb
                        + reserved_gb
                        + candidate.estimated_size_gb
                    )
                    if free_gb < required_gb:
                        break
                    prefetch_tasks[candidate.id] = asyncio.create_task(manager.ensure(candidate))
                    reserved_gb += candidate.estimated_size_gb

            if progress:
                progress("model", index, len(models), f"Evaluating {model.id}", None)
            single = app_config.model_copy(update={"models": [model]})
            await run_experiment(
                single,
                progress=progress,
                suite=suite,
                finalize=False,
                resume=configuration.automation.resume,
                execution_start=execution_start,
                warm_models=True,
            )
            incomplete = [
                selection.id for selection in configuration.benchmarks
                if checkpoint_store.job_status(suite_id, selection.id, model.id) != "complete"
            ]
            if incomplete:
                raise RuntimeError(
                    f"{model.id} did not complete: {', '.join(incomplete)}; the model was retained"
                )
            _run_sync(configuration.automation.sync_command, suite_dir)
            completed.append(model.id)
            if configuration.automation.cleanup_downloaded_models:
                await manager.remove_if_owned(model)

        await run_experiment(
            app_config,
            progress=progress,
            suite=suite,
            finalize=True,
            resume=True,
            execution_start=execution_start,
        )
        _run_sync(configuration.automation.sync_command, suite_dir)
        status = "complete"
        error = None
    except RunInterrupted as exc:
        status = "cancelled"
        error = f"{type(exc).__name__}: {exc}"
        raise
    except asyncio.CancelledError:
        status = "cancelled"
        error = "CancelledError: execution cancelled"
        raise
    except Exception as exc:
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        for task in prefetch_tasks.values():
            if not task.done():
                task.cancel()
        if prefetch_tasks:
            await asyncio.gather(*prefetch_tasks.values(), return_exceptions=True)
        manifest = {
            "framework": "RAGnarok",
            "mode": "automation",
            "suite_id": suite_id,
            "status": status,
            "error": error,
            "completed_models": completed,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "configuration": configuration.model_dump(mode="json"),
            "initial_ollama_models": sorted(manager.initial_models),
            "automation_owned_models_remaining": sorted(manager.owned_models),
        }
        (suite_dir / "automation_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return suite_dir
