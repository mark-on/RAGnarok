from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from ..config import ModelConfig, RuntimeConfig
from ..core import BenchmarkAdapter, BenchmarkInfo, OptionSpec
from ..core.benchmark import OptionChoice, ProgressCallback
from ..credentials import resolve_credential
from ._runtime import safe_name, sha256_file, write_json


UPSTREAM_URL = "https://github.com/ReversecLabs/spikee"
UPSTREAM_COMMIT = "v0.9.1"
SEED_NAME = "seeds-cybersec-2026-01"
PROFILE_COUNTS = {"light": 90, "medium": 250, "full": 300}
MAX_OUTPUT_TOKENS = 1024
CACHE_SCHEMA = 1
TARGET_MODULE = "ragnarok_llm"
TARGET_STUB = (
    "from ragnarok.benchmarks.spikee_target import RAGnarokLLMTarget as _RAGnarokLLMTarget\n\n"
    "class RAGnarokLLMTarget(_RAGnarokLLMTarget):\n"
    "    pass\n"
)


def _spikee_executable() -> Path | None:
    """Resolve the CLI beside the active Python before consulting PATH."""

    scripts_dir = Path(sys.executable).resolve().parent
    for candidate in (scripts_dir / "spikee.exe", scripts_dir / "spikee"):
        if candidate.is_file():
            return candidate
    discovered = shutil.which("spikee")
    return Path(discovered) if discovered else None


def _stable_case_hash(line: str) -> str:
    payload = json.loads(line)
    if isinstance(payload, dict):
        for key in ("id", "uuid", "timestamp", "created_at", "generated_at"):
            payload.pop(key, None)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class SPIKEEOptions(BaseModel):
    profile: Literal["light", "medium", "full"] = "medium"


class SPIKEEAdapter(BenchmarkAdapter):
    @property
    def info(self) -> BenchmarkInfo:
        return BenchmarkInfo(
            id="spikee",
            name="SPIKEE",
            upstream_url=UPSTREAM_URL,
            upstream_commit=UPSTREAM_COMMIT,
            description="Direct prompt injection, system prompt leakage, exfiltration, XSS, and resource abuse.",
            python_extra="spikee",
        )

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[3]

    @property
    def workspace(self) -> Path:
        return self.project_root / "benchmarks" / "spikee" / "workspace"

    @property
    def cache_dir(self) -> Path:
        return self.project_root / ".ragnarok" / "cache" / "spikee" / "v0.9.1"

    @property
    def dataset_path(self) -> Path:
        return self.cache_dir / "cybersec-full-300.jsonl"

    @property
    def manifest_path(self) -> Path:
        return self.cache_dir / "manifest.json"

    def _install_target(self) -> Path:
        target_dir = self.workspace / "targets"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{TARGET_MODULE}.py"
        if not target_path.is_file() or target_path.read_text(encoding="utf-8") != TARGET_STUB:
            target_path.write_text(TARGET_STUB, encoding="utf-8")
        return target_path

    def option_specs(self) -> tuple[OptionSpec, ...]:
        return (OptionSpec(
            key="profile",
            label="SPIKEE evaluation size",
            kind="select",
            default="medium",
            choices=(
                OptionChoice("Light - 90 frozen cybersecurity cases", "light"),
                OptionChoice("Medium - 250 frozen cybersecurity cases", "medium"),
                OptionChoice("Full - 300-case RAGnarok SPIKEE profile", "full"),
            ),
        ),)

    def validate_options(self, options: dict[str, object]) -> dict[str, object]:
        return SPIKEEOptions.model_validate(options).model_dump()

    def estimate_model_calls(self, options: dict[str, object]) -> int:
        profile = SPIKEEOptions.model_validate(self.validate_options(options)).profile
        return PROFILE_COUNTS[profile]

    def validate_installation(self) -> list[str]:
        problems = []
        if importlib.util.find_spec("spikee") is None or _spikee_executable() is None:
            problems.append("missing Python dependency: spikee; run: ragnarok setup")
        return problems

    def validate_prepared(self) -> list[str]:
        if not self.dataset_path.is_file() or not self.manifest_path.is_file():
            return ["SPIKEE is not prepared. Run: ragnarok setup"]
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if manifest.get("schema") != CACHE_SCHEMA or manifest.get("upstream_release") != UPSTREAM_COMMIT:
                return ["SPIKEE cache is stale. Run: ragnarok setup"]
            if manifest.get("dataset_sha256") != sha256_file(self.dataset_path):
                return ["SPIKEE dataset integrity check failed. Run: ragnarok setup"]
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return ["SPIKEE cache is invalid. Run: ragnarok setup"]
        return []

    def prepare(self, *, progress: ProgressCallback | None = None, log_path: Path | None = None) -> dict[str, object]:
        problems = self.validate_installation()
        if problems:
            raise ValueError("; ".join(problems))
        if not self.validate_prepared():
            if progress:
                progress("dataset", 2, 2, "SPIKEE verified cache is already ready")
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._install_target()
        output = log_path.open("a", encoding="utf-8") if log_path else subprocess.DEVNULL
        try:
            if not (self.workspace / "datasets" / SEED_NAME).is_dir():
                if progress:
                    progress("dataset", 0, 2, "Initializing the pinned SPIKEE workspace")
                subprocess.run(
                    [str(_spikee_executable()), "init"],
                    cwd=self.workspace,
                    check=True,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
            if progress:
                progress("dataset", 1, 2, "Generating the frozen SPIKEE cybersecurity dataset")
            before = set(self.workspace.rglob("*.jsonl"))
            subprocess.run(
                [
                    str(_spikee_executable()), "generate", "--seed-folder", str(self.workspace / "datasets" / SEED_NAME),
                    "--format", "full-prompt",
                ],
                cwd=self.workspace,
                check=True,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
            candidates = [path for path in self.workspace.rglob("*.jsonl") if path not in before and "cybersec" in path.name.lower()]
            if not candidates:
                candidates = [path for path in self.workspace.rglob("*.jsonl") if "cybersec" in path.name.lower() and SEED_NAME not in str(path.parent)]
            if not candidates:
                raise RuntimeError("SPIKEE did not generate a cybersecurity dataset")
            source = max(candidates, key=lambda path: path.stat().st_mtime)
            rows = [line for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
            if len(rows) < PROFILE_COUNTS["full"]:
                raise RuntimeError(f"SPIKEE generated only {len(rows)} cases; at least 300 are required")
            selected = sorted(rows, key=_stable_case_hash)[:PROFILE_COUNTS["full"]]
            self.dataset_path.write_text("\n".join(selected) + "\n", encoding="utf-8")
        finally:
            if log_path and output is not subprocess.DEVNULL:
                output.close()
        manifest = {
            "schema": CACHE_SCHEMA,
            "upstream_url": UPSTREAM_URL,
            "upstream_release": UPSTREAM_COMMIT,
            "seed": SEED_NAME,
            "source_dataset": str(source),
            "selection": "stable_content_sha256_sorted_deterministic_subset",
            "case_count": PROFILE_COUNTS["full"],
            "dataset_sha256": sha256_file(self.dataset_path),
            "prepared_at": datetime.now(timezone.utc).isoformat(),
        }
        write_json(self.manifest_path, manifest)
        if progress:
            progress("dataset", 2, 2, "SPIKEE dataset prepared and verified")
        return manifest

    async def run(
        self, *, options, models, runtime: RuntimeConfig, output_dir: Path,
        progress: ProgressCallback | None = None, judge=None, attacker=None,
    ) -> list[Path]:
        del judge, attacker
        opts = SPIKEEOptions.model_validate(self.validate_options(options))
        problems = [*self.validate_installation(), *self.validate_prepared()]
        if problems:
            raise ValueError("SPIKEE is not ready:\n  - " + "\n  - ".join(problems))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = output_dir / self.info.id / stamp
        run_dir.mkdir(parents=True, exist_ok=False)
        count = PROFILE_COUNTS[opts.profile]
        rows = self.dataset_path.read_text(encoding="utf-8").splitlines()[:count]
        profile_dataset = run_dir / f"dataset-{opts.profile}.jsonl"
        profile_dataset.write_text("\n".join(rows) + "\n", encoding="utf-8")
        summaries = []
        for model in models:
            summaries.append(await asyncio.to_thread(
                self._run_model, model, runtime, opts, profile_dataset, run_dir, progress
            ))
        write_json(run_dir / "run_manifest.json", {
            "framework": "RAGnarok",
            "benchmark": self.info.id,
            "upstream_url": UPSTREAM_URL,
            "upstream_release": UPSTREAM_COMMIT,
            "seed": SEED_NAME,
            "options": opts.model_dump(),
            "profile_qualification": "frozen_ragnarok_profile_derived_from_official_spikee_seed",
            "case_count": count,
            "inference_limits": {
                "max_output_tokens_per_call": MAX_OUTPUT_TOKENS,
                "request_timeout_seconds": [model.timeout_seconds for model in models],
                "attempts_per_case": 1,
                "subject_threads": 1,
                "retries_after_started_inference": 0,
                "reasoning_enabled": False,
                "ollama_token_limit_parameter": "num_predict",
            },
            "models": summaries,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        return [run_dir]

    def _target(self, model: ModelConfig) -> tuple[str, dict[str, str]]:
        if model.adapter == "ollama":
            target_options = (
                f"provider=ollama,model={model.model},max_tokens={MAX_OUTPUT_TOKENS},temperature=0"
            )
            base_url = model.base_url or "http://localhost:11434"
            return target_options, {
                "RAGNAROK_OLLAMA_URL": base_url,
                "RAGNAROK_OLLAMA_KEEP_ALIVE": "10m",
                "RAGNAROK_REQUEST_TIMEOUT": str(model.timeout_seconds),
            }
        if model.adapter == "openai":
            key = resolve_credential(model.credential_id)
            if not key:
                raise ValueError("SPIKEE model credential is unavailable")
            target_options = (
                f"provider=openai,model={model.model},max_tokens={MAX_OUTPUT_TOKENS},temperature=0"
            )
            environment = {
                "RAGNAROK_OPENAI_API_KEY": key,
                "RAGNAROK_OPENAI_BASE_URL": model.base_url or "https://api.openai.com/v1",
                "RAGNAROK_REQUEST_TIMEOUT": str(model.timeout_seconds),
            }
            if model.reasoning_enabled is not None:
                environment["RAGNAROK_REASONING_ENABLED"] = str(model.reasoning_enabled).lower()
            return target_options, environment
        raise ValueError("SPIKEE currently requires an Ollama or OpenAI-compatible model")

    def _run_model(self, model, runtime, opts, dataset, run_dir, progress):
        model_dir = run_dir / safe_name(model.id)
        native_dir = model_dir / "native"
        normalized_dir = model_dir / "normalized"
        native_dir.mkdir(parents=True)
        normalized_dir.mkdir()
        self._install_target()
        target, additions = self._target(model)
        request_log = model_dir / "requests.jsonl"
        additions["RAGNAROK_REQUEST_LOG"] = str(request_log)
        environment = os.environ.copy()
        environment.update(additions)
        before = set(self.workspace.rglob("*.jsonl"))
        command = [
            str(_spikee_executable()), "test", "--dataset", str(dataset), "--target", TARGET_MODULE,
            "--target-options", target, "--threads", "1", "--attempts", "1",
            "--max-retries", "1",
            "--no-auto-resume", "--tag", safe_name(model.id),
        ]
        if progress:
            progress("inference", 0, PROFILE_COUNTS[opts.profile], f"{model.id}: running SPIKEE")
        log_path = native_dir / "spikee.log"
        with log_path.open("w", encoding="utf-8") as log:
            process_options = {
                "cwd": self.workspace,
                "env": environment,
                "stdout": log,
                "stderr": subprocess.STDOUT,
                "text": True,
            }
            if os.name == "nt":
                process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                process_options["start_new_session"] = True
            process = subprocess.Popen(
                command,
                **process_options,
            )
            first_inference_error: str | None = None

            def terminate_process() -> None:
                if process.poll() is not None:
                    return
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    import signal

                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

            try:
                last_completed = -1
                completed = 0
                request_position = 0
                partial_line = b""
                output_tokens = 0
                request_seconds = 0.0
                while process.poll() is None:
                    if request_log.is_file():
                        try:
                            with request_log.open("rb") as handle:
                                handle.seek(request_position)
                                chunk = handle.read()
                            request_position += len(chunk)
                            lines = (partial_line + chunk).split(b"\n")
                            partial_line = lines.pop()
                            for raw_line in lines:
                                if not raw_line.strip():
                                    continue
                                completed += 1
                                row = json.loads(raw_line.decode("utf-8"))
                                output_tokens += int(row.get("output_tokens") or 0)
                                request_seconds += float(row.get("wall_duration_seconds") or 0)
                                if row.get("error_type"):
                                    first_inference_error = (
                                        f"{row.get('error_type')}: "
                                        f"{row.get('error_message') or 'no error detail'}"
                                    )
                                    terminate_process()
                                    break
                        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                            pass
                    completed = min(completed, PROFILE_COUNTS[opts.profile])
                    if progress and completed != last_completed:
                        progress(
                            "inference",
                            completed,
                            PROFILE_COUNTS[opts.profile],
                            (
                                f"{model.id}: generating SPIKEE case 1/{PROFILE_COUNTS[opts.profile]}"
                                if completed == 0
                                else f"{model.id}: SPIKEE case {completed}/{PROFILE_COUNTS[opts.profile]}"
                            ),
                            {
                                "tokens_per_second": (
                                    output_tokens / request_seconds
                                    if output_tokens and request_seconds
                                    else None
                                ),
                                "eta_seconds": (
                                    request_seconds / completed
                                    * (PROFILE_COUNTS[opts.profile] - completed)
                                    if completed
                                    else None
                                ),
                            },
                        )
                    last_completed = completed
                    if first_inference_error:
                        break
                    time.sleep(0.25)
                if first_inference_error:
                    raise RuntimeError(
                        f"SPIKEE stopped after the first failed inference "
                        f"({completed}/{PROFILE_COUNTS[opts.profile]}): {first_inference_error}"
                    )
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, command)
            finally:
                terminate_process()
        candidates = [path for path in self.workspace.rglob("*.jsonl") if path not in before and "result" in str(path).lower()]
        if not candidates:
            raise RuntimeError("SPIKEE completed without producing a result file")
        result_path = max(candidates, key=lambda path: path.stat().st_mtime)
        copied = native_dir / "spikee_results.jsonl"
        shutil.copy2(result_path, copied)
        result_rows = [json.loads(line) for line in copied.read_text(encoding="utf-8").splitlines() if line.strip()]
        expected = PROFILE_COUNTS[opts.profile]
        if len(result_rows) != expected:
            raise RuntimeError(f"SPIKEE produced {len(result_rows)} results; expected {expected}")
        failed_rows = [row for row in result_rows if row.get("error")]
        if failed_rows:
            raise RuntimeError(
                f"SPIKEE inference failed for {len(failed_rows)}/{expected} cases; "
                "the run is invalid and no ASR was calculated"
            )
        normalized = []
        successes = 0
        source_rows = [
            json.loads(line)
            for line in dataset.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        source_by_id = {str(row.get("id")): row for row in source_rows}
        for index, row in enumerate(result_rows):
            success = bool(row.get("success", row.get("attack_success", row.get("passed", False))))
            successes += success
            source = source_by_id.get(str(row.get("id")), {})
            native_result = {**source, **row}
            normalized.append({
                "benchmark_id": self.info.id,
                "model_id": model.id,
                "case_id": str(row.get("id", row.get("case_id", index))),
                "attack_family": str(row.get("category", row.get("attack_type", "direct_prompt_injection"))),
                "prompt": str(row.get("prompt", row.get("input", row.get("user_input", "")))),
                "response": str(row.get("response", row.get("output", row.get("target_response", "")))),
                "target": source.get("ideal_answer") or source.get("judge_args"),
                "payload": source.get("payload"),
                "official_evaluation": {
                    "attack_success": success,
                    "is_attack_case": True,
                    "dataset": "SPIKEE",
                    "judge": row.get("judge"),
                    "judge_result": row.get("judge_result", row.get("evaluation")),
                    "profile": opts.profile,
                    "seed": SEED_NAME,
                    "native_result": native_result,
                },
                "error": row.get("error"),
            })
        with (normalized_dir / "cases.jsonl").open("w", encoding="utf-8") as handle:
            for row in normalized:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        metrics = {"ASR": successes / expected, "case_count": expected}
        write_json(native_dir / "metrics.json", metrics)
        if progress:
            progress("inference", expected, expected, f"{model.id}: SPIKEE complete")
        return {"model_id": model.id, "metrics": metrics, "native_result": str(copied)}
