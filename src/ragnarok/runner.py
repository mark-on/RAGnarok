from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .benchmarks import benchmark_for
from .config import AppConfig, BenchmarkSelection, ModelConfig
from .core.benchmark import ProgressCallback
from .files import safe_name
from .interrupts import RunInterrupted
from .models import provider_for
from .reports import generate_reports, validate_report_dependency
from .results import ResultStore
from .results.schemas import UniversalCase
from .schemas import ChatMessage, ProviderRequest


def create_result_dir(config: AppConfig, *, prefix: str | None = None) -> tuple[str, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if prefix:
        base = f"{safe_name(prefix)}_{stamp}"
    elif len(config.models) == 1:
        base = f"{safe_name(config.models[0].id)}_{stamp}"
    else:
        base = f"group_{stamp}"
    suite_id = base
    path = config.output_dir / suite_id
    suffix = 2
    while path.exists():
        suite_id = f"{base}-{suffix}"
        path = config.output_dir / suite_id
        suffix += 1
    path.mkdir(parents=True)
    return suite_id, path


async def validate_run_configuration(
    config: AppConfig,
    *,
    progress: ProgressCallback | None = None,
    resume_suite: tuple[str, Path] | None = None,
) -> None:
    """Fail before billing starts when a benchmark, credential, endpoint, or model is unavailable."""

    problems: list[str] = []
    role_models: list[tuple[str, ModelConfig]] = [
        ("Subject", model)
        for model in config.models
        if _resume_subject_required(config, resume_suite, model)
    ]
    for selection in config.benchmarks:
        adapter = benchmark_for(selection.id)
        try:
            adapter.validate_options(selection.options)
        except Exception as exc:
            problems.append(f"{adapter.info.name} options: {exc}")
        problems.extend(
            f"{adapter.info.name}: {problem}"
            for problem in [*adapter.validate_installation(), *adapter.validate_prepared()]
        )
        if adapter.info.requires_judge and selection.judge is None:
            problems.append(f"{adapter.info.name}: a Judge model is required")
        if adapter.info.requires_attacker and selection.attacker is None:
            problems.append(f"{adapter.info.name}: an attacker model is required")
        if selection.judge is not None:
            role_models.append((f"{adapter.info.name} Judge", selection.judge))
        if selection.attacker is not None:
            role_models.append((f"{adapter.info.name} attacker", selection.attacker))
    if problems:
        raise ValueError("preflight failed:\n  - " + "\n  - ".join(dict.fromkeys(problems)))

    unique: dict[tuple[str, str, str, str], tuple[str, ModelConfig]] = {}
    for role, model in role_models:
        key = (model.adapter, model.base_url or "", model.endpoint or "", model.model)
        unique.setdefault(key, (role, model))

    async def check(position: int, role: str, model: ModelConfig) -> str | None:
        if progress:
            progress("preflight", position, len(unique), f"Checking {role}: {model.model}")
        provider = provider_for(model, config.runtime)
        try:
            ok, detail = await provider.check()
        finally:
            await provider.aclose()
        return None if ok else f"{role} {model.model}: {detail}"

    failures = await asyncio.gather(*(
        check(index, role, model)
        for index, (role, model) in enumerate(unique.values(), start=1)
    ))
    failures = [failure for failure in failures if failure]
    if failures:
        raise ValueError("model preflight failed:\n  - " + "\n  - ".join(failures))
    if progress:
        progress("preflight", len(unique), len(unique), "All models and benchmarks are ready")


def _pending_resume_ollama_models(
    config: AppConfig,
    suite: tuple[str, Path] | None,
) -> list[ModelConfig]:
    """Return Ollama models required by unfinished jobs in a persisted suite."""

    if suite is None:
        return []
    suite_id, suite_dir = suite
    store = ResultStore(suite_dir)
    required: dict[tuple[str, str], ModelConfig] = {}
    for subject in config.models:
        for selection in config.benchmarks:
            job_status = store.job_status(suite_id, selection.id, subject.id)
            if job_status == "complete":
                continue
            role_models = (
                (selection.judge, selection.attacker)
                if selection.id == "mpib" and job_status == "judging_deferred"
                else (subject, selection.judge, selection.attacker)
            )
            for model in role_models:
                if model is not None and model.adapter == "ollama":
                    base_url = (model.base_url or "http://localhost:11434").rstrip("/")
                    required.setdefault((base_url, model.model), model)
    return list(required.values())


def _resume_subject_required(
    config: AppConfig,
    suite: tuple[str, Path] | None,
    subject: ModelConfig,
) -> bool:
    if suite is None:
        return True
    suite_id, suite_dir = suite
    store = ResultStore(suite_dir)
    unfinished = [
        (selection.id, store.job_status(suite_id, selection.id, subject.id))
        for selection in config.benchmarks
        if store.job_status(suite_id, selection.id, subject.id) != "complete"
    ]
    return any(
        not (benchmark_id == "mpib" and status == "judging_deferred")
        for benchmark_id, status in unfinished
    )


async def _ensure_resume_ollama_models(
    config: AppConfig,
    suite: tuple[str, Path] | None,
    progress: ProgressCallback | None,
) -> None:
    """Restore missing Ollama models before validating a resumed run."""

    from .automation import OllamaModelManager

    managers: dict[str, OllamaModelManager] = {}
    for model in _pending_resume_ollama_models(config, suite):
        base_url = (model.base_url or "http://localhost:11434").rstrip("/")
        manager = managers.setdefault(base_url, OllamaModelManager(base_url, progress))
        await manager.ensure(model)


async def _warm_subject_model(
    model: ModelConfig,
    config: AppConfig,
    progress: ProgressCallback | None,
) -> None:
    if model.adapter != "ollama":
        return
    if progress:
        progress("warmup", 0, 1, f"Loading {model.model} into the Ollama runtime")
    provider = provider_for(model, config.runtime)
    request = ProviderRequest(
        conversation_messages=[ChatMessage(role="user", content="")],
        model=model.model,
        temperature=0,
        max_output_tokens=1,
        timeout=model.timeout_seconds,
    )
    try:
        metadata = await provider.warm_up(request)
    finally:
        await provider.aclose()
    if progress:
        processor = metadata.get("processor", "configured processor")
        progress("warmup", 1, 1, f"{model.model} ready on {processor}")


@dataclass
class _AdapterResultBatch:
    cases: list[UniversalCase] = field(default_factory=list)
    model_calls: list[dict] = field(default_factory=list)
    metrics: list[tuple[str, str, str, str, dict]] = field(default_factory=list)
    artifacts: list[tuple[str, str, str, str, Path]] = field(default_factory=list)


@dataclass(frozen=True)
class _EtaRolePlan:
    calls: int
    task_weight: float = 1.0
    parallelism: int = 1

    def remaining_work(self, completed_calls: int = 0, total_calls: int | None = None) -> float:
        remaining_calls = max((total_calls if total_calls is not None else self.calls) - completed_calls, 0)
        return remaining_calls * self.task_weight / max(self.parallelism, 1)


@dataclass(frozen=True)
class _EtaJobPlan:
    key: tuple[str, str]
    roles: dict[str, _EtaRolePlan]


@dataclass
class _EtaSample:
    completed: int
    observed_calls: int
    sampled_at: float


class _SuiteEtaProgress:
    """Estimate suite time from live task rates without touching model execution."""

    _MIN_OBSERVED_CALLS = 3
    _REFERENCE_TOKENS_PER_SECOND = 50.0
    _DEFAULT_SECONDS_PER_WORK = {
        "subject": 2.5,
        "judge": 1.5,
        "attacker": 2.5,
    }

    def __init__(self, callback: ProgressCallback, plans: list[_EtaJobPlan]):
        self.callback = callback
        self.plans = {plan.key: plan for plan in plans}
        self.active: _EtaJobPlan | None = None
        self.pending = set(self.plans)
        self.active_completed: dict[str, int] = {}
        self.samples: dict[str, _EtaSample] = {}
        self.seconds_per_work: dict[tuple[str, str], float] = {}
        self.role_seconds_per_work: dict[str, float] = {}
        self.rate_totals: dict[tuple[str, str], tuple[float, float]] = {}
        self.role_rate_totals: dict[str, tuple[float, float]] = {}
        self.tokens_per_second: dict[tuple[str, str], float] = {}
        self.observed_totals: dict[tuple[str, str], int] = {}
        self.last_tokens_per_second: float | None = None
        self._lock = threading.Lock()

    def start_job(self, key: tuple[str, str]) -> None:
        with self._lock:
            self.active = self.plans[key]
            self.active_completed = {role: 0 for role in self.active.roles}
            self.samples = {}

    def finish_job(self) -> None:
        with self._lock:
            if self.active is not None:
                self.pending.discard(self.active.key)
            self.active = None
            self.active_completed = {}
            self.samples = {}

    @staticmethod
    def _role_for_phase(phase: str) -> str | None:
        lowered = phase.lower()
        if "judge" in lowered:
            return "judge"
        if "attacker" in lowered:
            return "attacker"
        if "inference" in lowered:
            return "subject"
        return None

    @staticmethod
    def _rate_key(plan: _EtaJobPlan, role: str) -> tuple[str, str]:
        # Subject speed is quantization-specific. Shared remote role models can be
        # learned across subject jobs in the same suite.
        return (plan.key[0], role) if role == "subject" else ("shared", role)

    def _observe(
        self,
        plan: _EtaJobPlan,
        role: str,
        current: int,
        total: int,
        now: float,
        tokens_per_second: float | None,
    ) -> None:
        role_plan = plan.roles[role]
        effective_total = max(total, 1)
        self.observed_totals[(plan.key[1], role)] = effective_total
        current = min(max(current, 0), effective_total)
        self.active_completed[role] = max(self.active_completed.get(role, 0), current)
        sample = self.samples.get(role)
        if sample is None or current < sample.completed:
            # The first value is a resume-safe baseline. Work completed before this
            # process started is never divided into the current session's time.
            self.samples[role] = _EtaSample(current, 0, now)
        elif current > sample.completed:
            delta_calls = current - sample.completed
            elapsed = max(now - sample.sampled_at, 0.0)
            observed_calls = sample.observed_calls + delta_calls
            if elapsed > 0:
                work = delta_calls * role_plan.task_weight / max(role_plan.parallelism, 1)
                key = self._rate_key(plan, role)
                prior_seconds, prior_work = self.rate_totals.get(key, (0.0, 0.0))
                total_seconds = prior_seconds + elapsed
                total_work = prior_work + work
                self.rate_totals[key] = (total_seconds, total_work)
                self.seconds_per_work[key] = total_seconds / max(total_work, 1e-9)

                role_seconds, role_work = self.role_rate_totals.get(role, (0.0, 0.0))
                role_seconds += elapsed
                role_work += work
                self.role_rate_totals[role] = (role_seconds, role_work)
                self.role_seconds_per_work[role] = role_seconds / max(role_work, 1e-9)
            self.samples[role] = _EtaSample(current, observed_calls, now)
        if tokens_per_second and tokens_per_second > 0:
            key = self._rate_key(plan, role)
            previous_tps = self.tokens_per_second.get(key)
            self.tokens_per_second[key] = (
                tokens_per_second if previous_tps is None else previous_tps * 0.8 + tokens_per_second * 0.2
            )

    def _estimated_rate(self, plan: _EtaJobPlan, role: str) -> float:
        key = self._rate_key(plan, role)
        if key in self.seconds_per_work:
            return self.seconds_per_work[key]
        if role in self.role_seconds_per_work:
            return self.role_seconds_per_work[role]
        default = self._DEFAULT_SECONDS_PER_WORK.get(role, 2.5)
        tps = self.tokens_per_second.get(key)
        if tps:
            return default * self._REFERENCE_TOKENS_PER_SECOND / tps
        return default

    def _remaining_seconds(self) -> float | None:
        if not any(sample.observed_calls >= self._MIN_OBSERVED_CALLS for sample in self.samples.values()):
            return None
        remaining = 0.0
        for key in self.pending:
            plan = self.plans[key]
            for role, role_plan in plan.roles.items():
                completed = self.active_completed.get(role, 0) if plan is self.active else 0
                effective_total = self.observed_totals.get((plan.key[1], role), role_plan.calls)
                remaining += role_plan.remaining_work(completed, effective_total) * self._estimated_rate(plan, role)
        return max(remaining, 0.0)

    def update(
        self,
        phase: str,
        current: int,
        total: int | None,
        detail: str,
        stats: dict[str, object] | None = None,
    ) -> None:
        from .benchmarks.mpib import deferred_mpib_fatal_error
        fatal_error = deferred_mpib_fatal_error()
        if fatal_error is not None:
            raise fatal_error
        outgoing = dict(stats or {})
        role = self._role_for_phase(phase)
        with self._lock:
            raw_tps = outgoing.get("tokens_per_second")
            current_tps = float(raw_tps) if raw_tps is not None else None
            if current_tps is not None:
                self.last_tokens_per_second = current_tps
            if self.active is not None and role in self.active.roles and total:
                self._observe(self.active, role, current, total, time.perf_counter(), current_tps)
                outgoing["eta_seconds"] = self._remaining_seconds()
                outgoing["eta_label"] = "Suite ETA"
                if outgoing.get("tokens_per_second") is None and self.last_tokens_per_second is not None:
                    outgoing["tokens_per_second"] = self.last_tokens_per_second
        self.callback(phase, current, total, detail, outgoing or None)


def _suite_eta_plans(
    config: AppConfig,
    store: ResultStore,
    suite_id: str,
    *,
    resume: bool,
) -> list[_EtaJobPlan]:
    task_weights = {
        "spikee": {"subject": 1.0},
        "poisonedrag": {"subject": 0.55},
        "mpib": {"subject": 2.6, "judge": 1.0},
        "agentdojo": {"subject": 1.35},
    }
    plans: list[_EtaJobPlan] = []
    for model in config.models:
        for selection in config.benchmarks:
            if resume and store.job_status(suite_id, selection.id, model.id) == "complete":
                continue
            adapter = benchmark_for(selection.id)
            options = adapter.validate_options(selection.options)
            benchmark_weights = task_weights.get(selection.id, {})
            progress_estimator = getattr(adapter, "estimate_progress_units", None)
            subject_units = (
                progress_estimator(options)
                if callable(progress_estimator)
                else adapter.estimate_model_calls(options)
            )
            roles = {
                "subject": _EtaRolePlan(
                    max(subject_units, 1),
                    benchmark_weights.get("subject", 1.0),
                ),
            }
            if selection.judge is not None:
                judge_parallelism = 1 if selection.judge.adapter == "ollama" else config.runtime.judge_concurrency
                roles["judge"] = _EtaRolePlan(
                    max(adapter.estimate_judge_calls(options), 1),
                    benchmark_weights.get("judge", 1.0),
                    judge_parallelism,
                )
            if selection.attacker is not None:
                attacker_calls = adapter.estimate_attacker_calls(options)
                if attacker_calls:
                    roles["attacker"] = _EtaRolePlan(
                        attacker_calls,
                        benchmark_weights.get("attacker", 1.0),
                    )
            plans.append(_EtaJobPlan((model.id, selection.id), roles))
    return plans


def _jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _load_adapter_results(suite_id: str, run_dirs: list[Path]) -> _AdapterResultBatch:
    batch = _AdapterResultBatch()
    for run_dir in run_dirs:
        manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
        benchmark_id = manifest["benchmark"]
        for model_dir in (path for path in run_dir.iterdir() if path.is_dir()):
            case_path = model_dir / "normalized" / "cases.jsonl"
            if not case_path.is_file():
                continue
            model_id = model_dir.name
            for payload in _jsonl(case_path):
                payload["suite_id"] = suite_id
                case = UniversalCase.model_validate(payload)
                model_id = case.model_id
                batch.cases.append(case)
            request_logs = (
                ("subject", model_dir / "requests.jsonl"),
                ("judge", model_dir / "judge_requests.jsonl"),
                ("attacker", model_dir / "attacker_requests.jsonl"),
            )
            for role, request_path in request_logs:
                if not request_path.is_file():
                    continue
                for position, native in enumerate(_jsonl(request_path), start=1):
                    batch.model_calls.append({
                        "suite_id": suite_id,
                        "benchmark_id": benchmark_id,
                        "model_id": model_id,
                        "call_id": f"{role}:{native.get('call_index', position)}",
                        "phase": native.get("phase", role),
                        "model_role": role,
                        "case_index": native.get("case_index"),
                        "prompt": native.get("raw_prompt", native.get("prompt", "")),
                        "response": native.get("response", ""),
                        "provider": native.get("provider"),
                        "provider_model": native.get("model"),
                        "input_tokens": native.get("input_tokens"),
                        "output_tokens": native.get("output_tokens"),
                        "wall_duration_seconds": native.get("wall_duration_seconds"),
                        "runtime_metadata": native.get("runtime_metadata", {}),
                        "metadata": native.get("metadata", {}),
                        "error_type": native.get("error_type", ""),
                        "error_message": native.get("error_message", ""),
                    })
            metrics_path = model_dir / "native" / "metrics.json"
            if metrics_path.is_file():
                batch.metrics.append((
                    suite_id, benchmark_id, model_id, "official",
                    json.loads(metrics_path.read_text(encoding="utf-8")),
                ))
            batch.artifacts.append((suite_id, benchmark_id, model_id, "native_run", run_dir))
    return batch


def _set_job_statuses(
    store: ResultStore,
    suite_id: str,
    benchmark_id: str,
    models: list[ModelConfig],
    status: str,
    *,
    error: str | None = None,
) -> None:
    for model in models:
        store.set_job_status(suite_id, benchmark_id, model.id, status, error=error)


async def _execute_benchmark(
    *,
    config: AppConfig,
    selection: BenchmarkSelection,
    adapter,
    models: list[ModelConfig],
    suite_id: str,
    suite_dir: Path,
    store: ResultStore,
    progress: ProgressCallback | None,
    benchmark_index: int,
) -> list[Path]:
    try:
        options = adapter.validate_options(selection.options)
        if progress:
            progress("benchmark", benchmark_index, len(config.benchmarks), f"Running {adapter.info.name}")
        _set_job_statuses(store, suite_id, selection.id, models, "running")
        run_dirs = await adapter.run(
            options=options,
            models=models,
            runtime=config.runtime,
            output_dir=suite_dir / "artifacts" / "benchmarks",
            progress=progress,
            judge=selection.judge,
            attacker=selection.attacker,
        )
        batch = _load_adapter_results(suite_id, run_dirs)
        store.add_batch(
            cases=batch.cases,
            model_calls=batch.model_calls,
            metrics=batch.metrics,
            artifacts=batch.artifacts,
        )
        deferred = False
        for run_dir in run_dirs:
            manifest_path = run_dir / "run_manifest.json"
            if manifest_path.is_file():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                deferred = deferred or manifest.get("status") == "judging_deferred"
        _set_job_statuses(
            store,
            suite_id,
            selection.id,
            models,
            "judging_deferred" if deferred else "complete",
        )
        return run_dirs
    except RunInterrupted:
        _set_job_statuses(
            store, suite_id, selection.id, models, "cancelled", error="stop confirmed by user"
        )
        raise
    except Exception as exc:
        _set_job_statuses(store, suite_id, selection.id, models, "failed", error=str(exc))
        raise


def _write_suite_manifest(
    *,
    config: AppConfig,
    suite_id: str,
    suite_dir: Path,
    status: str,
    error: str | None,
    benchmark_errors: list[dict[str, str]],
    benchmark_runs: list[Path],
    store: ResultStore,
    resume: bool,
    execution: dict[str, object],
) -> None:
    serialized = config.model_dump(mode="json")
    manifest_path = suite_dir / "suite_manifest.json"
    prior_run_dirs: list[str] = []
    prior_judge_changes: list[dict] = []
    if manifest_path.is_file():
        try:
            prior_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            prior_run_dirs = list(prior_manifest.get("benchmark_run_dirs", []))
            prior_judge_changes = list(prior_manifest.get("judge_model_changes", []))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            prior_run_dirs = []
            prior_judge_changes = []
    current_run_dirs = [str(path.relative_to(suite_dir)) for path in benchmark_runs]
    manifest = {
        "framework": "RAGnarok",
        "suite_id": suite_id,
        "result_directory": str(suite_dir),
        "layout": "single_model" if len(config.models) == 1 else "model_group",
        "status": status,
        "error": error,
        "benchmark_errors": benchmark_errors,
        "created_at": execution["started_at_utc"],
        "execution": execution,
        "configuration": serialized,
        "configuration_sha256": hashlib.sha256(json.dumps(serialized, sort_keys=True).encode()).hexdigest(),
        "benchmark_run_dirs": list(dict.fromkeys([*prior_run_dirs, *current_run_dirs])),
        "jobs": store.jobs(),
        "resume_enabled": resume,
        "judge_model_changes": prior_judge_changes,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _load_or_create_execution(
    suite_id: str,
    suite_dir: Path,
    initial: dict[str, object] | None = None,
) -> tuple[dict[str, object], float]:
    manifest_path = suite_dir / "suite_manifest.json"
    if manifest_path.is_file():
        prior_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        execution = prior_manifest.get("execution", {})
    else:
        execution = {}
    if not execution.get("started_at_utc"):
        if initial:
            execution = {"suite_id": suite_id, "status": "running", **initial}
        else:
            utc_now = datetime.now(timezone.utc)
            local_now = datetime.now().astimezone()
            execution = {
                "suite_id": suite_id,
                "started_at_utc": utc_now.isoformat(),
                "started_at_local": local_now.isoformat(),
                "local_timezone": local_now.tzname(),
                "utc_offset": f"{local_now.strftime('%z')[:3]}:{local_now.strftime('%z')[3:]}",
                "status": "running",
            }
    started = datetime.fromisoformat(str(execution["started_at_utc"]))
    elapsed_before = max((datetime.now(timezone.utc) - started).total_seconds(), 0.0)
    return execution, time.perf_counter() - elapsed_before


def _finish_execution(
    execution: dict[str, object],
    suite_dir: Path,
    *,
    status: str,
    monotonic_origin: float,
) -> dict[str, object]:
    utc_now = datetime.now(timezone.utc)
    local_now = datetime.now().astimezone()
    execution.update({
        "status": status,
        "completed_at_utc": utc_now.isoformat(),
        "completed_at_local": local_now.isoformat(),
        "elapsed_seconds": max(time.perf_counter() - monotonic_origin, 0.0),
    })
    return execution


async def run_experiment(
    config: AppConfig,
    *,
    progress: ProgressCallback | None = None,
    suite: tuple[str, Path] | None = None,
    finalize: bool = True,
    resume: bool = False,
    execution_start: dict[str, object] | None = None,
    preflight: bool = False,
    warm_models: bool = False,
) -> list[Path]:
    validate_report_dependency()
    if resume:
        await _ensure_resume_ollama_models(config, suite, progress)
    if preflight:
        await validate_run_configuration(
            config,
            progress=progress,
            resume_suite=suite if resume else None,
        )
    suite_id, suite_dir = suite or create_result_dir(config)
    execution, monotonic_origin = _load_or_create_execution(suite_id, suite_dir, execution_start)
    store = ResultStore(suite_dir)
    suite_progress = (
        _SuiteEtaProgress(progress, _suite_eta_plans(config, store, suite_id, resume=resume))
        if progress
        else None
    )
    benchmark_runs: list[Path] = []
    benchmark_errors: list[dict[str, str]] = []
    status = "running"
    error: str | None = None
    try:
        # Complete every benchmark for one model before loading the next model.
        # This avoids reloading the same quantization once per benchmark.
        for model in config.models:
            pending_selections = [
                selection for selection in config.benchmarks
                if not (resume and store.job_status(suite_id, selection.id, model.id) == "complete")
            ]
            subject_required = _resume_subject_required(
                config, (suite_id, suite_dir) if resume else None, model
            )
            if warm_models and pending_selections and subject_required:
                await _warm_subject_model(model, config, progress)
            for index, selection in enumerate(config.benchmarks):
                if resume and store.job_status(suite_id, selection.id, model.id) == "complete":
                    continue
                adapter = benchmark_for(selection.id)
                if suite_progress:
                    suite_progress.start_job((model.id, selection.id))
                try:
                    new_runs = await _execute_benchmark(
                        config=config,
                        selection=selection,
                        adapter=adapter,
                        models=[model],
                        suite_id=suite_id,
                        suite_dir=suite_dir,
                        store=store,
                        progress=suite_progress.update if suite_progress else None,
                        benchmark_index=index,
                    )
                    benchmark_runs.extend(new_runs)
                except RunInterrupted:
                    raise
                except Exception as exc:
                    benchmark_errors.append({
                        "benchmark_id": selection.id,
                        "model_id": model.id,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    from .benchmarks._judge_queue import JudgePumpFatalError, JudgeQueueStorageError
                    if isinstance(exc, (JudgePumpFatalError, JudgeQueueStorageError)):
                        raise
                finally:
                    if suite_progress:
                        suite_progress.finish_job()
        from .benchmarks.mpib import finalize_deferred_mpib
        deferred_runs, pending_judgments = await finalize_deferred_mpib(timeout_seconds=600.0)
        if deferred_runs:
            benchmark_runs.extend(deferred_runs)
            refreshed = _load_adapter_results(suite_id, deferred_runs)
            store.add_batch(
                cases=refreshed.cases,
                model_calls=refreshed.model_calls,
                metrics=refreshed.metrics,
                artifacts=refreshed.artifacts,
            )
            for run_dir in deferred_runs:
                manifest_path = run_dir / "run_manifest.json"
                if not manifest_path.is_file():
                    continue
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                for item in manifest.get("models", []):
                    if isinstance(item, dict) and item.get("model_id"):
                        store.set_job_status(
                            suite_id,
                            "mpib",
                            str(item["model_id"]),
                            "judging_deferred" if pending_judgments else "complete",
                        )
        if pending_judgments:
            benchmark_errors.append({
                "benchmark_id": "mpib",
                "model_id": "deferred_judge_queues",
                "error": (
                    f"Judge provider unavailable; {pending_judgments} evaluations remain "
                    "on disk. Consider changing the provider or Judge model."
                ),
            })
        cases = store.cases()
        if not benchmark_runs and not cases:
            if len(benchmark_errors) == 1:
                raise RuntimeError(benchmark_errors[0]["error"])
            raise RuntimeError("all selected benchmarks failed; see suite_manifest.json")
        status = "partial" if benchmark_errors else "complete"
        if finalize:
            _finish_execution(
                execution,
                suite_dir,
                status=status,
                monotonic_origin=monotonic_origin,
            )
        report_paths = []
        if finalize and progress:
            progress(
                "report", 0, 1, "Generating XLSX, CSV, and JSON reports",
                {"eta_seconds": 0.0, "eta_label": "Suite ETA"},
            )
        if finalize:
            report_paths = generate_reports(
                suite_dir,
                cases,
                official_metrics=store.metrics(),
                model_calls=store.model_calls(),
                suite_status=status,
                execution=execution,
                configuration=config.model_dump(mode="json"),
                postprocess_workers=config.runtime.postprocess_workers,
            )
        if finalize and progress:
            progress(
                "report", 1, 1, "Reports and results are ready",
                {"eta_seconds": 0.0, "eta_label": "Suite ETA"},
            )
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
        if status != "running":
            _finish_execution(
                execution,
                suite_dir,
                status=status,
                monotonic_origin=monotonic_origin,
            )
        _write_suite_manifest(
            config=config,
            suite_id=suite_id,
            suite_dir=suite_dir,
            status=status,
            error=error,
            benchmark_errors=benchmark_errors,
            benchmark_runs=benchmark_runs,
            store=store,
            resume=resume,
            execution=execution,
        )
    return [suite_dir, *report_paths]
