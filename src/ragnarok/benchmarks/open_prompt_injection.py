from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import types
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..config import ModelConfig, RuntimeConfig
from ..core import BenchmarkAdapter, BenchmarkInfo, OptionChoice, OptionSpec
from ..core.benchmark import ProgressCallback
from ..files import safe_name as _safe_name, sha256_file as _sha256_file, sha256_text as _sha256_text
from ..models import provider_for
from ..schemas import ChatMessage, ProviderRequest


UPSTREAM_URL = "https://github.com/liu00222/Open-Prompt-Injection"
UPSTREAM_COMMIT = "95290f7ce3794c4c52ad3fe8113db2bfcdfe89e0"
OFFICIAL_TASKS = (
    "sst2",
    "sms_spam",
    "mrpc",
    "hsol",
    "rte",
    "jfleg",
    "gigaword",
)
WINDOWS_INCOMPATIBLE_TASKS = {"sms_spam", "hsol"}
TASK_LABELS = {
    "sst2": "SST-2 — sentiment analysis",
    "sms_spam": "SMS Spam — spam detection",
    "mrpc": "MRPC — duplicate sentence detection",
    "hsol": "HSOL — hate detection",
    "rte": "RTE — natural-language inference",
    "jfleg": "JFLEG — grammar correction",
    "gigaword": "Gigaword — summarization",
}
REQUIRED_IMPORTS = {
    "accelerate": "accelerate>=0.32",
    "datasets": "datasets==2.19.2",
    "fastchat": "fschat==0.2.36",
    "psutil": "psutil>=5.9",
    "rouge": "rouge==1.0.1",
    "scipy": "scipy",
    "torch": "torch>=2.3",
    "tqdm": "tqdm==4.66.4",
    "transformers": "transformers>=4.42",
}


class OpenPromptInjectionOptions(BaseModel):
    target_task: str = "sst2"
    injected_task: str = "rte"
    data_num: int = Field(10, ge=2, le=10000)
    attack_strategy: str = "combine"
    defense: str = "no"

    @field_validator("target_task", "injected_task")
    @classmethod
    def validate_task(cls, value: str) -> str:
        if value not in OFFICIAL_TASKS:
            raise ValueError(f"unsupported official task: {value}")
        return value

    @field_validator("attack_strategy")
    @classmethod
    def validate_attack(cls, value: str) -> str:
        if value != "combine":
            raise ValueError("the pinned official run supports the combine attack strategy")
        return value

    @field_validator("defense")
    @classmethod
    def validate_defense(cls, value: str) -> str:
        if value != "no":
            raise ValueError("the first exact integration supports the official no-defense run")
        return value


def _json_default(value):
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"cannot encode {type(value).__name__}")


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


@contextmanager
def _working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class _OfficialModules:
    def __init__(self, upstream: Path):
        # The retired upstream imports a task-template namespace removed in
        # datasets 4.x. SPIKEE pins datasets 4.8.4, so provide only the legacy
        # metadata object required to import the byte-for-byte upstream task.
        if "datasets.tasks" not in sys.modules:
            try:
                importlib.import_module("datasets.tasks")
            except ModuleNotFoundError:
                datasets_tasks = types.ModuleType("datasets.tasks")

                class TextClassification:
                    def __init__(self, **kwargs):
                        self.__dict__.update(kwargs)

                datasets_tasks.TextClassification = TextClassification
                sys.modules["datasets.tasks"] = datasets_tasks
        package_name = "OpenPromptInjection"
        if package_name not in sys.modules:
            package = types.ModuleType(package_name)
            package.__path__ = [str(upstream / package_name)]
            package.__package__ = package_name
            sys.modules[package_name] = package

        # Avoid importing the upstream root and apps __init__ modules, which eagerly
        # load unrelated local-model and defense dependencies. The benchmark modules
        # below are loaded directly and remain byte-for-byte upstream code.
        apps_name = f"{package_name}.apps"
        if apps_name not in sys.modules:
            apps = types.ModuleType(apps_name)
            apps.__path__ = [str(upstream / package_name / "apps")]
            apps.__package__ = apps_name
            sys.modules[apps_name] = apps

        self.tasks = importlib.import_module(f"{package_name}.tasks")
        self.attackers = importlib.import_module(f"{package_name}.attackers")
        self.evaluator = importlib.import_module(f"{package_name}.evaluator")
        self.application = importlib.import_module(f"{package_name}.apps.Application")
        self.process_config = importlib.import_module(f"{package_name}.utils.process_config")

    def create_task(self, *args, **kwargs):
        return self.tasks.create_task(*args, **kwargs)

    def create_attacker(self, *args, **kwargs):
        return self.attackers.create_attacker(*args, **kwargs)

    def create_app(self, task, model, defense="no"):
        return self.application.Application(task, model, defense=defense)

    def create_evaluator(self, *args, **kwargs):
        return self.evaluator.create_evaluator(*args, **kwargs)

    def open_config(self, path: Path):
        return self.process_config.open_config(config_path=str(path))


class _ModelProxy:
    """Implements Open-Prompt-Injection's synchronous model.query contract."""

    def __init__(
        self,
        model: ModelConfig,
        runtime: RuntimeConfig,
        requests_path: Path,
        *,
        temperature: float,
        max_output_tokens: int,
        progress: ProgressCallback | None,
        progress_offset: int,
        progress_total: int,
    ):
        self.model = model
        self.provider = provider_for(model, runtime)
        self.requests_path = requests_path
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.progress = progress
        self.progress_offset = progress_offset
        self.progress_total = progress_total
        self.phase = "unknown"
        self.case_index = -1
        self.call_index = 0
        self.errors = 0
        self.wall_duration_seconds = 0.0
        self.output_tokens = 0
        self.eval_duration_ns = 0
        self.warm_up_metadata: dict[str, object] = {}
        self._log_handle = self.requests_path.open("a", encoding="utf-8", buffering=1024 * 1024)
        self._closed = False

    def _request(self, message: str) -> ProviderRequest:
        return ProviderRequest(
            system_prompt=None,
            conversation_messages=[ChatMessage(role="user", content=message)],
            model=self.model.model,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            timeout=self.model.timeout_seconds,
        )

    def warm_up(self) -> dict[str, object]:
        try:
            if hasattr(self.provider, "warm_up_sync"):
                self.warm_up_metadata = self.provider.warm_up_sync(self._request(""))
        except Exception as exc:
            self.warm_up_metadata = {"error": f"{type(exc).__name__}: {exc}"}
        return self.warm_up_metadata

    def set_context(self, phase: str, case_index: int) -> None:
        self.phase = phase
        self.case_index = case_index

    def query(self, msg: str) -> str:
        # The pinned upstream gpt_config selects GPTAzure, whose official query
        # method sends the complete constructed prompt as one user message.
        request = self._request(msg)
        started = time.perf_counter()
        if hasattr(self.provider, "generate_sync"):
            result = self.provider.generate_sync(request)
        else:
            result = asyncio.run(self.provider.generate(request))
        self.wall_duration_seconds += time.perf_counter() - started
        self.call_index += 1
        if result.output_tokens:
            self.output_tokens += result.output_tokens
        eval_duration = result.runtime_metadata.get("eval_duration_ns")
        if isinstance(eval_duration, int):
            self.eval_duration_ns += eval_duration
        if result.error_type:
            self.errors += 1

        record = {
            "call_index": self.call_index,
            "phase": self.phase,
            "case_index": self.case_index,
            "raw_prompt": msg,
            "request_sha256": _sha256_text(msg),
            "messages": [message.model_dump() for message in request.conversation_messages],
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
            "model": self.model.model,
            "provider": result.provider,
            "response": result.response_text,
            "response_sha256": _sha256_text(result.response_text),
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "error_type": result.error_type,
            "error_message": result.error_message,
            "runtime_metadata": result.runtime_metadata,
        }
        self._log_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        if self.call_index % 10 == 0 or result.error_type:
            self._log_handle.flush()

        if self.progress:
            completed = self.progress_offset + self.call_index
            measured_seconds = self.eval_duration_ns / 1_000_000_000 or self.wall_duration_seconds
            tokens_per_second = self.output_tokens / measured_seconds if self.output_tokens and measured_seconds else None
            remaining = max(self.progress_total - completed, 0)
            eta_seconds = (self.wall_duration_seconds / self.call_index) * remaining
            try:
                self.progress(
                    "inference",
                    completed,
                    self.progress_total,
                    f"{self.model.model}: {self.phase} case {self.case_index + 1}",
                    {
                        "tokens_per_second": tokens_per_second,
                        "eta_seconds": eta_seconds,
                    },
                )
            except BaseException:
                self.close()
                raise
        if result.error_type:
            self.close()
            raise RuntimeError(
                f"model call {self.call_index} failed ({result.error_type}): "
                f"{result.error_message or 'no error detail'}"
            )
        return result.response_text

    def flush(self) -> None:
        self._log_handle.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._log_handle.flush()
        self._log_handle.close()
        if hasattr(self.provider, "close_sync"):
            self.provider.close_sync()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class OpenPromptInjectionAdapter(BenchmarkAdapter):
    @property
    def info(self) -> BenchmarkInfo:
        return BenchmarkInfo(
            id="open_prompt_injection",
            name="Open-Prompt-Injection",
            upstream_url=UPSTREAM_URL,
            upstream_commit=UPSTREAM_COMMIT,
            description="Official target-task, injected-task, combined-attack, and native-evaluator workflow.",
            python_extra="open-prompt-injection",
        )

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[3]

    @property
    def upstream_dir(self) -> Path:
        return self.project_root / "benchmarks" / "open_prompt_injection" / "upstream"

    def option_specs(self) -> tuple[OptionSpec, ...]:
        choices = tuple(OptionChoice(TASK_LABELS[name], name) for name in OFFICIAL_TASKS)
        return (
            OptionSpec("target_task", "Select the official target task", "select", "sst2", choices),
            OptionSpec("injected_task", "Select the official injected task", "select", "rte", choices),
            OptionSpec("data_num", "Number of official examples per phase", "integer", 10),
        )

    def validate_options(self, options: dict[str, object]) -> dict[str, object]:
        validated = OpenPromptInjectionOptions.model_validate(options)
        if os.name == "nt":
            incompatible = sorted(
                {validated.target_task, validated.injected_task} & WINDOWS_INCOMPATIBLE_TASKS
            )
            if incompatible:
                names = ", ".join(incompatible)
                raise ValueError(
                    f"the pinned official cache paths for {names} contain ':' and cannot exist on Windows; "
                    "run this configuration under Linux or WSL to preserve upstream behavior"
                )
        return validated.model_dump()

    def validate_installation(self) -> list[str]:
        problems: list[str] = []
        if not (self.upstream_dir / "OpenPromptInjection").is_dir():
            problems.append("official submodule is missing; run: git submodule update --init --recursive")
        else:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    f"safe.directory={self.upstream_dir.as_posix()}",
                    "-C",
                    str(self.upstream_dir),
                    "rev-parse",
                    "HEAD",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            commit = result.stdout.strip()
            if result.returncode != 0 or commit != UPSTREAM_COMMIT:
                problems.append(f"official source must be pinned to {UPSTREAM_COMMIT}; found {commit or 'unknown'}")
        for module, requirement in REQUIRED_IMPORTS.items():
            if importlib.util.find_spec(module) is None:
                problems.append(f"missing Python dependency: {requirement}")
        return problems

    def estimate_model_calls(self, options: dict[str, object]) -> int:
        validated = OpenPromptInjectionOptions.model_validate(self.validate_options(options))
        return validated.data_num * 3

    async def run(
        self,
        *,
        options: dict[str, object],
        models: list[ModelConfig],
        runtime: RuntimeConfig,
        output_dir: Path,
        progress: ProgressCallback | None = None,
    ) -> list[Path]:
        validated = OpenPromptInjectionOptions.model_validate(self.validate_options(options))
        problems = self.validate_installation()
        if problems:
            detail = "\n  - ".join(problems)
            raise ValueError(f"Open-Prompt-Injection is not ready:\n  - {detail}")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = output_dir / self.info.id / stamp
        suffix = 2
        while run_dir.exists():
            run_dir = output_dir / self.info.id / f"{stamp}-{suffix}"
            suffix += 1
        run_dir.mkdir(parents=True)

        official_config_path = self.upstream_dir / "configs" / "model_configs" / "gpt_config.json"
        official_model_config = json.loads(official_config_path.read_text(encoding="utf-8"))
        decoding = official_model_config["params"]
        calls_per_model = self.estimate_model_calls(validated.model_dump())
        total_calls = calls_per_model * len(models)
        model_results = []

        for position, model in enumerate(models):
            if progress:
                progress("model", position, len(models), f"Preparing {model.model}")
            result = await asyncio.to_thread(
                self._run_model,
                validated,
                model,
                runtime,
                run_dir,
                float(decoding["temperature"]),
                int(decoding["max_output_tokens"]),
                progress,
                position * calls_per_model,
                total_calls,
            )
            model_results.append(result)

        manifest = {
            "framework": "RAGnarok",
            "benchmark": self.info.id,
            "benchmark_name": self.info.name,
            "upstream_url": UPSTREAM_URL,
            "upstream_commit": UPSTREAM_COMMIT,
            "qualification": "transport_only",
            "transport_profile": "official GPTAzure single-user-message mapping",
            "options": validated.model_dump(),
            "official_model_parameters": {
                "source": "configs/model_configs/gpt_config.json",
                "sha256": _sha256_file(official_config_path),
                "temperature": float(decoding["temperature"]),
                "max_output_tokens": int(decoding["max_output_tokens"]),
            },
            "models": [model.model_dump(mode="json") for model in models],
            "expected_model_calls": total_calls,
            "model_results": model_results,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        _write_json(run_dir / "run_manifest.json", manifest)
        return [run_dir]

    def _run_model(
        self,
        options: OpenPromptInjectionOptions,
        model: ModelConfig,
        runtime: RuntimeConfig,
        run_dir: Path,
        temperature: float,
        max_output_tokens: int,
        progress: ProgressCallback | None,
        progress_offset: int,
        progress_total: int,
    ) -> dict[str, Any]:
        import numpy as np

        model_dir = run_dir / _safe_name(model.id)
        model_dir.mkdir()
        requests_path = model_dir / "requests.jsonl"
        proxy = _ModelProxy(
            model,
            runtime,
            requests_path,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            progress=progress,
            progress_offset=progress_offset,
            progress_total=progress_total,
        )
        if progress:
            progress("warmup", 0, 1, f"{model.model}: loading model runtime")
        warm_up = proxy.warm_up()
        if progress:
            processor = warm_up.get("processor", "provider-managed processor")
            progress("warmup", 1, 1, f"{model.model}: ready on {processor} with one inference worker")

        with _working_directory(self.upstream_dir):
            official = _OfficialModules(self.upstream_dir)
            target_config_path = self.upstream_dir / "configs" / "task_configs" / f"{options.target_task}_config.json"
            injected_config_path = self.upstream_dir / "configs" / "task_configs" / f"{options.injected_task}_config.json"
            target_task = official.create_task(official.open_config(target_config_path), options.data_num)
            injected_task = official.create_task(
                official.open_config(injected_config_path), options.data_num, for_injection=True
            )
            attacker = official.create_attacker(options.attack_strategy, injected_task)
            target_app = official.create_app(target_task, proxy, defense=options.defense)

            target_responses: list[str] = []
            for index, (data_prompt, _ground_truth_label) in enumerate(target_app):
                proxy.set_context("target_task_baseline", index)
                target_responses.append(target_app.query(data_prompt, verbose=0, idx=index, total=len(target_app)))

            injected_responses: list[str] = []
            for index, (data_prompt, _ground_truth_label) in enumerate(attacker.task):
                proxy.set_context("injected_task_baseline", index)
                prompt = attacker.task.get_instruction() + "\nText: " + data_prompt
                injected_responses.append(proxy.query(prompt))

            attack_responses: list[str] = []
            for index, (data_prompt, _ground_truth_label) in enumerate(target_app):
                attacked_prompt = attacker.inject(data_prompt, index, target_task=target_task.task)
                proxy.set_context("combined_attack", index)
                attack_responses.append(
                    target_app.query(attacked_prompt, verbose=0, idx=index, total=len(target_app))
                )

            evaluator = official.create_evaluator(
                target_task_responses=target_responses,
                target_task=target_task,
                injected_task_responses=injected_responses,
                injected_task=attacker.task,
                attack_responses=attack_responses,
            )

            native_dir = model_dir / "native"
            native_dir.mkdir()
            np.savez(native_dir / "target_task_responses.npz", data=target_responses)
            np.savez(native_dir / "injected_task_responses.npz", data=injected_responses)
            np.savez(native_dir / "attack_responses.npz", data=attack_responses)
            metrics = {
                "PNA-T": evaluator.pna_t,
                "PNA-I": evaluator.pna_i,
                "ASV": evaluator.asv,
                "MR": evaluator.mr,
            }
            _write_json(native_dir / "metrics.json", metrics)

            proxy.flush()
            request_rows = [json.loads(line) for line in requests_path.read_text(encoding="utf-8").splitlines()]
            attack_prompts = {
                int(row["case_index"]): row["raw_prompt"]
                for row in request_rows if row["phase"] == "combined_attack"
            }
            normalized_dir = model_dir / "normalized"
            normalized_dir.mkdir()
            with (normalized_dir / "cases.jsonl").open("w", encoding="utf-8") as handle:
                for index, response in enumerate(attack_responses):
                    row = {
                        "benchmark_id": self.info.id,
                        "model_id": model.id,
                        "case_id": f"{options.target_task}:{options.injected_task}:{index}",
                        "attack_family": "direct_prompt_injection",
                        "prompt": attack_prompts.get(index, ""),
                        "response": response,
                        "target": options.target_task,
                        "payload": options.injected_task,
                        "baseline_response": target_responses[index] if index < len(target_responses) else None,
                        "reference_answer": None,
                        "adversarial_answer": injected_responses[index] if index < len(injected_responses) else None,
                        "retrieved_contexts": [],
                        "injected_contexts": [],
                        "official_evaluation": {"aggregate_metrics": metrics},
                        "error": None,
                    }
                    handle.write(json.dumps(row, ensure_ascii=False, default=_json_default) + "\n")

            processed_data = {}
            for task_name, task in (("target", target_task), ("injected", injected_task)):
                data_dir = Path(task.get_data_saving_path())
                for file in sorted(data_dir.glob("*.npz")):
                    processed_data[f"{task_name}:{file.name}"] = _sha256_file(file)

        model_result = {
            "model": model.model,
            "model_id": model.id,
            "provider_adapter": model.adapter,
            "calls": proxy.call_index,
            "errors": proxy.errors,
            "metrics": metrics,
            "processed_dataset_hashes": processed_data,
            "requests_file": str(requests_path.relative_to(run_dir)),
            "transport": {
                "inference_workers": 1,
                "connection_reused": True,
                "buffered_request_log": True,
                "warm_up": proxy.warm_up_metadata,
            },
        }
        proxy.close()
        return model_result
