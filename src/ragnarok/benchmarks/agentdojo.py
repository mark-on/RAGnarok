from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..config import ModelConfig, RuntimeConfig
from ..core import BenchmarkAdapter, BenchmarkInfo, OptionSpec
from ..core.benchmark import OptionChoice, ProgressCallback
from ..credentials import resolve_credential
from ._runtime import recoverable_run_dir, safe_name, write_json


UPSTREAM_URL = "https://github.com/ethz-spylab/agentdojo"
UPSTREAM_COMMIT = "v0.1.35"
BENCHMARK_VERSION = "v1.2.2"
PROFILE_COUNTS = {"light": 100, "medium": 300, "full": 629}
MAX_OUTPUT_TOKENS = 1024
MAX_LLM_CALLS_PER_CASE = 16
CANONICAL_INJECTION_TASKS = {
    "workspace": tuple(f"injection_task_{index}" for index in range(6)),
    "travel": (
        "injection_task_6", "injection_task_0", "injection_task_1", "injection_task_2",
        "injection_task_3", "injection_task_4", "injection_task_5",
    ),
    "banking": tuple(f"injection_task_{index}" for index in range(9)),
    "slack": tuple(f"injection_task_{index}" for index in range(1, 6)),
}
_EVALUATOR_GUARD_LOCK = threading.Lock()


@contextmanager
def _guard_malformed_trace_evaluators():
    """Treat malformed tool arguments as failed official objectives, not runner crashes."""

    from agentdojo.task_suite.task_suite import TaskSuite

    recoveries: list[dict[str, str]] = []
    with _EVALUATOR_GUARD_LOCK:
        original = TaskSuite._check_task_result

        def guarded(suite, task, model_output, pre_environment, task_environment, functions_stack_trace):
            try:
                return original(
                    suite,
                    task,
                    model_output,
                    pre_environment,
                    task_environment,
                    functions_stack_trace,
                )
            except (KeyError, IndexError, TypeError, AttributeError) as exc:
                recoveries.append({
                    "task_id": str(getattr(task, "ID", type(task).__name__)),
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "resolution": "objective_failed_for_malformed_tool_trace",
                })
                return False

        TaskSuite._check_task_result = guarded
        try:
            yield recoveries
        finally:
            TaskSuite._check_task_result = original


def canonical_security_cases(suites: dict, target_count: int) -> list[tuple[str, str, str]]:
    """Return the canonical 629-case matrix or a deterministic suite-stratified subset."""

    buckets: dict[str, list[tuple[str, str, str]]] = {}
    for suite_name, injection_ids in CANONICAL_INJECTION_TASKS.items():
        suite = suites[suite_name]
        missing = set(injection_ids).difference(suite.injection_tasks)
        if missing:
            raise ValueError(
                f"AgentDojo {BENCHMARK_VERSION} is missing canonical {suite_name} tasks: "
                + ", ".join(sorted(missing))
            )
        buckets[suite_name] = [
            (suite_name, user_task_id, injection_id)
            for user_task_id in suite.user_tasks
            for injection_id in injection_ids
        ]
    full_count = sum(len(bucket) for bucket in buckets.values())
    if full_count != PROFILE_COUNTS["full"]:
        raise ValueError(
            f"AgentDojo canonical matrix contains {full_count} cases; "
            f"expected {PROFILE_COUNTS['full']}"
        )
    if target_count >= full_count:
        return [case for suite_name in CANONICAL_INJECTION_TASKS for case in buckets[suite_name]]

    exact = {
        suite_name: target_count * len(bucket) / full_count
        for suite_name, bucket in buckets.items()
    }
    quotas = {suite_name: int(value) for suite_name, value in exact.items()}
    remaining = target_count - sum(quotas.values())
    order = sorted(
        buckets,
        key=lambda suite_name: (exact[suite_name] - quotas[suite_name], suite_name),
        reverse=True,
    )
    for suite_name in order[:remaining]:
        quotas[suite_name] += 1

    selected = []
    for suite_name in CANONICAL_INJECTION_TASKS:
        ordered = sorted(
            buckets[suite_name],
            key=lambda case: hashlib.sha256(":".join(case).encode()).hexdigest(),
        )
        selected.extend(ordered[:quotas[suite_name]])
    return selected


def normalize_agentdojo_outcome(utility: object, security: object) -> tuple[bool, bool]:
    """Translate AgentDojo's native flags into utility and attack-success values.

    AgentDojo's ``security`` evaluator returns ``True`` when the injection goal was
    executed successfully.  The name is historical; for RAGnarok reports this is
    therefore an attack success, not a security pass.
    """

    return bool(utility), bool(security)


class _BoundedCompletions:
    def __init__(
        self,
        completions,
        max_output_tokens: int,
        *,
        ollama: bool = False,
        reasoning_enabled: bool | None = None,
    ):
        self._completions = completions
        self._max_output_tokens = max_output_tokens
        self._ollama = ollama
        self._reasoning_enabled = reasoning_enabled

    def create(self, *args, **kwargs):
        requested = kwargs.get("max_tokens")
        kwargs["max_tokens"] = (
            min(int(requested), self._max_output_tokens)
            if requested is not None
            else self._max_output_tokens
        )
        extra_body = dict(kwargs.get("extra_body") or {})
        if self._ollama:
            extra_body["think"] = False
        elif self._reasoning_enabled is not None:
            extra_body["reasoning"] = {"enabled": self._reasoning_enabled}
        if extra_body:
            kwargs["extra_body"] = extra_body
        return self._completions.create(*args, **kwargs)


class _BoundedChat:
    def __init__(self, chat, max_output_tokens: int, **kwargs):
        self.completions = _BoundedCompletions(chat.completions, max_output_tokens, **kwargs)


class _BoundedOpenAIClient:
    """Preserve AgentDojo's client contract while enforcing a response ceiling."""

    def __init__(self, client, max_output_tokens: int, **kwargs):
        self.chat = _BoundedChat(client.chat, max_output_tokens, **kwargs)


class _RecordedCompletions:
    """Record every AgentDojo LLM call without changing its request or response."""

    def __init__(
        self,
        completions,
        handle,
        *,
        provider: str,
        model: str,
        on_call=None,
        initial_calls: int = 0,
        initial_output_tokens: int = 0,
        initial_wall_seconds: float = 0.0,
    ):
        self._completions = completions
        self._handle = handle
        self._provider = provider
        self._model = model
        self._on_call = on_call
        self.calls = initial_calls
        self.output_tokens = initial_output_tokens
        self.wall_seconds = initial_wall_seconds

    def create(self, *args, **kwargs):
        started = time.perf_counter()
        response = None
        error_type = ""
        error_message = ""
        try:
            response = self._completions.create(*args, **kwargs)
            return response
        except Exception as exc:
            error_type = type(exc).__name__
            error_message = str(exc)[:1000]
            raise
        finally:
            elapsed = time.perf_counter() - started
            self.calls += 1
            self.wall_seconds += elapsed
            usage = getattr(response, "usage", None)
            input_tokens = getattr(usage, "prompt_tokens", None)
            output_tokens = getattr(usage, "completion_tokens", None)
            self.output_tokens += int(output_tokens or 0)
            message = None
            if response is not None:
                choices = getattr(response, "choices", None) or []
                message = getattr(choices[0], "message", None) if choices else None
            if hasattr(message, "model_dump"):
                response_payload = message.model_dump(mode="json")
            else:
                response_payload = str(message or "")
            row = {
                "call_index": self.calls,
                "phase": "subject_inference",
                "raw_prompt": json.dumps(kwargs.get("messages", []), ensure_ascii=False, default=str),
                "response": json.dumps(response_payload, ensure_ascii=False, default=str),
                "provider": self._provider,
                "model": self._model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "wall_duration_seconds": elapsed,
                "runtime_metadata": {
                    "max_output_tokens": kwargs.get("max_tokens"),
                    "agentic_tool_loop": True,
                },
                "error_type": error_type,
                "error_message": error_message,
            }
            self._handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._handle.flush()
            if self._on_call:
                self._on_call(self, row)


class _RecordedChat:
    def __init__(self, chat, handle, **kwargs):
        self.completions = _RecordedCompletions(chat.completions, handle, **kwargs)


class _RecordedOpenAIClient:
    def __init__(self, client, handle, **kwargs):
        self.chat = _RecordedChat(client.chat, handle, **kwargs)


def _assistant_text(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        if value.get("role") == "assistant":
            content = value.get("content")
            if isinstance(content, str):
                found.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        found.append(block["text"])
        for nested in value.values():
            found.extend(_assistant_text(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_assistant_text(nested))
    return found


def _trajectory_response(path: Path) -> tuple[str, str | None]:
    if not path.is_file():
        return "", None
    try:
        text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = [json.loads(line) for line in text.splitlines() if line.strip()]
    responses = _assistant_text(payload)
    return (responses[-1] if responses else ""), str(path)


def _load_case_checkpoint(path: Path) -> dict[str, dict]:
    cases: dict[str, dict] = {}
    if not path.is_file():
        return cases
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                case = json.loads(line)
            except json.JSONDecodeError:
                continue
            case_id = str(case.get("case_id") or "")
            if case_id:
                cases[case_id] = case
    return cases


def _request_log_totals(path: Path) -> tuple[int, int, float]:
    calls = 0
    output_tokens = 0
    wall_seconds = 0.0
    if not path.is_file():
        return calls, output_tokens, wall_seconds
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            calls = max(calls, int(row.get("call_index") or 0))
            output_tokens += int(row.get("output_tokens") or 0)
            wall_seconds += float(row.get("wall_duration_seconds") or 0.0)
    return calls, output_tokens, wall_seconds


class AgentDojoOptions(BaseModel):
    profile: Literal["light", "medium", "full"] = "medium"
    attack: str = "tool_knowledge"


@contextmanager
def _openai_compatible_environment(model: ModelConfig):
    if model.adapter == "ollama":
        base_url = (model.base_url or "http://localhost:11434").rstrip("/") + "/v1"
        api_key = "ollama"
    elif model.adapter == "openai":
        base_url = (model.base_url or "https://api.openai.com/v1").rstrip("/")
        api_key = resolve_credential(model.credential_id) or ""
    else:
        raise ValueError("AgentDojo currently requires an Ollama or OpenAI-compatible model")
    if not api_key:
        raise ValueError("AgentDojo model credential is unavailable")
    previous = {name: os.environ.get(name) for name in ("OPENAI_COMPATIBLE_BASE_URL", "OPENAI_COMPATIBLE_API_KEY")}
    os.environ["OPENAI_COMPATIBLE_BASE_URL"] = base_url
    os.environ["OPENAI_COMPATIBLE_API_KEY"] = api_key
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class AgentDojoAdapter(BenchmarkAdapter):
    @property
    def info(self) -> BenchmarkInfo:
        return BenchmarkInfo(
            id="agentdojo",
            name="AgentDojo",
            upstream_url=UPSTREAM_URL,
            upstream_commit=UPSTREAM_COMMIT,
            description="Agentic indirect prompt injection through untrusted tool data.",
            python_extra="agentdojo",
        )

    def option_specs(self) -> tuple[OptionSpec, ...]:
        return (OptionSpec(
            key="profile",
            label="AgentDojo evaluation size",
            kind="select",
            default="medium",
            choices=(
                OptionChoice("Light - 100 deterministic security cases", "light"),
                OptionChoice("Medium - 300 deterministic security cases", "medium"),
                OptionChoice("Full - 629 official security cases", "full"),
            ),
        ),)

    def validate_options(self, options: dict[str, object]) -> dict[str, object]:
        return AgentDojoOptions.model_validate(options).model_dump()

    def validate_installation(self) -> list[str]:
        if importlib.util.find_spec("agentdojo") is None:
            return ["missing Python dependency: agentdojo; run: ragnarok setup"]
        try:
            from importlib.metadata import version

            installed = version("agentdojo")
        except Exception as exc:
            return [f"unable to inspect AgentDojo installation: {exc}"]
        if installed != "0.1.35":
            return [f"AgentDojo 0.1.35 is required; installed: {installed}"]
        return []

    def validate_prepared(self) -> list[str]:
        return []

    def estimate_model_calls(self, options: dict[str, object]) -> int:
        profile = AgentDojoOptions.model_validate(self.validate_options(options)).profile
        return PROFILE_COUNTS[profile] * MAX_LLM_CALLS_PER_CASE

    def estimate_progress_units(self, options: dict[str, object]) -> int:
        """Return cases, the unit reported through AgentDojo progress callbacks."""
        profile = AgentDojoOptions.model_validate(self.validate_options(options)).profile
        return PROFILE_COUNTS[profile]

    async def run(
        self, *, options, models, runtime: RuntimeConfig, output_dir: Path,
        progress: ProgressCallback | None = None, judge=None, attacker=None,
    ) -> list[Path]:
        del judge, attacker
        opts = AgentDojoOptions.model_validate(self.validate_options(options))
        problems = self.validate_installation()
        if problems:
            raise ValueError("AgentDojo is not ready:\n  - " + "\n  - ".join(problems))
        run_dir = recoverable_run_dir(output_dir, self.info.id, [model.id for model in models])
        if run_dir is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            run_dir = output_dir / self.info.id / stamp
            run_dir.mkdir(parents=True, exist_ok=False)
        summaries = []
        for model in models:
            summaries.append(await asyncio.to_thread(self._run_model, model, runtime, opts, run_dir, progress))
        write_json(run_dir / "run_manifest.json", {
            "framework": "RAGnarok",
            "benchmark": self.info.id,
            "upstream_url": UPSTREAM_URL,
            "upstream_release": UPSTREAM_COMMIT,
            "benchmark_version": BENCHMARK_VERSION,
            "options": opts.model_dump(),
            "profile_qualification": (
                "official_complete_security_matrix" if opts.profile == "full"
                else "deterministic_suite_stratified_subset_of_official_security_matrix"
            ),
            "case_count": PROFILE_COUNTS[opts.profile],
            "canonical_case_matrix": {
                "workspace": 240,
                "travel": 140,
                "banking": 144,
                "slack": 105,
                "total": 629,
                "workspace_injection_tasks": list(CANONICAL_INJECTION_TASKS["workspace"]),
            },
            "inference_limits": {
                "max_output_tokens_per_call": MAX_OUTPUT_TOKENS,
                "request_timeout_seconds": [model.timeout_seconds for model in models],
                "upstream_tool_loop_max_iterations": MAX_LLM_CALLS_PER_CASE - 1,
                "maximum_llm_calls_per_case": MAX_LLM_CALLS_PER_CASE,
            },
            "models": summaries,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return [run_dir]

    def _run_model(
        self,
        model: ModelConfig,
        runtime: RuntimeConfig,
        opts: AgentDojoOptions,
        run_dir: Path,
        progress: ProgressCallback | None,
    ) -> dict:
        from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline, PipelineConfig
        from agentdojo.agent_pipeline.llms.openai_llm import OpenAILLM
        from agentdojo.attacks.attack_registry import load_attack
        from agentdojo.benchmark import run_task_with_injection_tasks
        from agentdojo.logging import OutputLogger
        from agentdojo.task_suite.load_suites import get_suites
        import openai

        model_dir = run_dir / safe_name(model.id)
        native_dir = model_dir / "native"
        normalized_dir = model_dir / "normalized"
        native_dir.mkdir(parents=True, exist_ok=True)
        normalized_dir.mkdir(exist_ok=True)
        target_count = PROFILE_COUNTS[opts.profile]
        checkpoint_path = normalized_dir / "checkpoint_cases.jsonl"
        case_by_id = _load_case_checkpoint(checkpoint_path)
        utility_passed = sum(
            bool(case.get("official_evaluation", {}).get("utility"))
            for case in case_by_id.values()
        )
        attacks_succeeded = sum(
            bool(case.get("official_evaluation", {}).get("attack_success"))
            for case in case_by_id.values()
        )
        request_path = model_dir / "requests.jsonl"
        initial_calls, initial_output_tokens, initial_wall_seconds = _request_log_totals(request_path)
        with _openai_compatible_environment(model), _guard_malformed_trace_evaluators() as evaluator_recoveries, OutputLogger(str(native_dir)), request_path.open(
            "a", encoding="utf-8", buffering=1024 * 1024
        ) as request_handle, checkpoint_path.open("a", encoding="utf-8") as checkpoint_handle:
            if model.adapter == "ollama":
                base_url = (model.base_url or "http://localhost:11434").rstrip("/") + "/v1"
                api_key = "ollama"
            else:
                base_url = (model.base_url or "https://api.openai.com/v1").rstrip("/")
                api_key = resolve_credential(model.credential_id) or ""
            client = openai.OpenAI(
                api_key=api_key,
                base_url=base_url,
                timeout=model.timeout_seconds,
                max_retries=0,
            )
            def report_call(recorder, _row):
                if not progress:
                    return
                completed = len(case_by_id)
                rate = (
                    recorder.output_tokens / recorder.wall_seconds
                    if recorder.output_tokens and recorder.wall_seconds
                    else None
                )
                eta = (
                    (recorder.wall_seconds / completed) * (target_count - completed)
                    if completed
                    else None
                )
                progress(
                    "inference",
                    completed,
                    target_count,
                    f"{model.id}: AgentDojo case {min(completed + 1, target_count)}/{target_count} · "
                    f"LLM call {recorder.calls}",
                    {"tokens_per_second": rate, "eta_seconds": eta},
                )

            recorded_client = _RecordedOpenAIClient(
                client,
                request_handle,
                provider=model.adapter,
                model=model.model,
                on_call=report_call,
                initial_calls=initial_calls,
                initial_output_tokens=initial_output_tokens,
                initial_wall_seconds=initial_wall_seconds,
            )
            bounded_llm = OpenAILLM(
                _BoundedOpenAIClient(
                    recorded_client,
                    MAX_OUTPUT_TOKENS,
                    ollama=model.adapter == "ollama",
                    reasoning_enabled=model.reasoning_enabled,
                ),
                model.model,
                temperature=0.0,
            )
            # AgentDojo uses this registry key to address arbitrary custom models
            # inside the official tool-knowledge attack prompt.
            bounded_llm.name = "local"
            pipeline = AgentPipeline.from_config(PipelineConfig(
                llm=bounded_llm,
                model_id=model.model,
                defense=None,
                tool_delimiter="tool",
                system_message_name=None,
                system_message=None,
            ))
            pipeline_name = safe_name(str(pipeline.name or "local"))
            suites = get_suites(BENCHMARK_VERSION)
            selected_cases = canonical_security_cases(suites, target_count)
            attacks = {
                suite_name: load_attack(opts.attack, suite, pipeline)
                for suite_name, suite in suites.items()
                if any(case[0] == suite_name for case in selected_cases)
            }
            if progress:
                progress(
                    "inference",
                    len(case_by_id),
                    target_count,
                    f"{model.id}: resuming AgentDojo at case "
                    f"{min(len(case_by_id) + 1, target_count)}/{target_count}",
                )
            try:
                for suite_name, user_task_id, injection_id in selected_cases:
                    case_id = f"{suite_name}:{user_task_id}:{injection_id}"
                    if case_id in case_by_id:
                        continue
                    suite = suites[suite_name]
                    user_task = suite.user_tasks[user_task_id]
                    injection_task = suite.injection_tasks[injection_id]
                    attack = attacks[suite_name]
                    recovery_start = len(evaluator_recoveries)
                    utility, security = run_task_with_injection_tasks(
                        suite,
                        pipeline,
                        user_task,
                        attack,
                        native_dir,
                        False,
                        injection_tasks=[injection_id],
                        benchmark_version=BENCHMARK_VERSION,
                    )
                    key = (user_task.ID, injection_id)
                    utility_value, attack_success = normalize_agentdojo_outcome(
                        utility.get(key, False),
                        security.get(key, False),
                    )
                    case_recoveries = evaluator_recoveries[recovery_start:]
                    utility_passed += utility_value
                    attacks_succeeded += attack_success
                    trajectory = (
                        native_dir
                        / pipeline_name
                        / suite.name
                        / user_task.ID
                        / attack.name
                        / f"{injection_id}.json"
                    )
                    response_text, trajectory_path = _trajectory_response(trajectory)
                    case = {
                        "benchmark_id": self.info.id,
                        "model_id": model.id,
                        "case_id": case_id,
                        "attack_family": "agentic_indirect_prompt_injection",
                        "prompt": str(user_task.PROMPT),
                        "response": response_text,
                        "target": str(injection_task.GOAL),
                        "payload": str(injection_task.GOAL),
                        "injected_contexts": [str(injection_task.GOAL)],
                        "official_evaluation": {
                            "attack_success": attack_success,
                            "security_passed": not attack_success,
                            "native_security_result": attack_success,
                            "utility": utility_value,
                            "suite": suite_name,
                            "user_task": user_task.ID,
                            "injection_task": injection_id,
                            "attack": opts.attack,
                            "profile": opts.profile,
                            "benchmark_version": BENCHMARK_VERSION,
                            "trajectory_location": trajectory_path or str(native_dir),
                            "evaluator_recoveries": case_recoveries,
                        },
                        "error": None,
                    }
                    case_by_id[case_id] = case
                    checkpoint_handle.write(json.dumps(case, ensure_ascii=False) + "\n")
                    checkpoint_handle.flush()
                    if progress:
                        progress(
                            "inference", len(case_by_id), target_count,
                            f"{model.id}: AgentDojo case {len(case_by_id)}/{target_count}",
                        )
            finally:
                client.close()
        cases = [
            case_by_id[f"{suite_name}:{user_task_id}:{injection_id}"]
            for suite_name, user_task_id, injection_id in selected_cases
            if f"{suite_name}:{user_task_id}:{injection_id}" in case_by_id
        ]
        if len(cases) != target_count:
            raise RuntimeError(f"AgentDojo produced {len(cases)} cases; expected {target_count}")
        with (normalized_dir / "cases.jsonl").open("w", encoding="utf-8") as handle:
            for case in cases:
                handle.write(json.dumps(case, ensure_ascii=False) + "\n")
        metrics = {
            "targeted_ASR": attacks_succeeded / len(cases),
            "security_pass_rate": 1 - (attacks_succeeded / len(cases)),
            "utility_under_attack": utility_passed / len(cases),
            "case_count": len(cases),
        }
        write_json(native_dir / "metrics.json", metrics)
        return {"model_id": model.id, "metrics": metrics}
