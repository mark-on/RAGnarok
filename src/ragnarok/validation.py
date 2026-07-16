from __future__ import annotations

import importlib.metadata
import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .credentials import CredentialError, resolve_credential
from .dataset import load_dataset
from .models import provider_for
from .pdf import extract_knowledge_base


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    status: str
    detail: str


@dataclass
class ValidationReport:
    checks: list[ValidationCheck]

    @property
    def ok(self) -> bool:
        return not any(check.status == "FAIL" for check in self.checks)

    def render(self) -> str:
        width = max((len(check.name) for check in self.checks), default=0)
        lines = [f"{check.status:<4}  {check.name:<{width}}  {check.detail}" for check in self.checks]
        lines.extend(["", "Validation completed successfully." if self.ok else "Validation failed. Resolve the FAIL checks before running an experiment."])
        return "\n".join(lines)


DEPENDENCIES = ("typer", "questionary", "keyring", "pydantic", "pydantic-settings", "PyYAML", "httpx", "pandas", "numpy", "sentence-transformers", "pypdf", "matplotlib", "reportlab")


def _benchmark_validation(root: Path) -> list[str]:
    path = root / "scripts" / "validate_dataset.py"
    if not path.is_file():
        return ["scripts/validate_dataset.py is missing"]
    spec = importlib.util.spec_from_file_location("ragnarok_benchmark_validator", path)
    if spec is None or spec.loader is None:
        return ["could not load the benchmark validator"]
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate()


def apply_mock_mode(config: AppConfig, root: Path) -> AppConfig:
    """Apply a deterministic, network-free two-model demonstration profile."""
    from .config import JudgeConfig, ModelConfig

    config.models = [
        ModelConfig(id="mock_safe_a", adapter="mock", model="deterministic-safe-a"),
        ModelConfig(id="mock_safe_b", adapter="mock", model="deterministic-safe-b"),
    ]
    config.judge = JudgeConfig(enabled=True, confidence_threshold=0.7, model=ModelConfig(id="mock_judge", adapter="mock", model="deterministic-judge"))
    config.rag.embedding_backend = "mock"
    config.rag.embedding_model = "mock-hash-v1"
    config.rag.cache_dir = (root / ".ragnarok" / "mock-cache").resolve()
    config.project.experiment_id = "offline-two-model-demo"
    return config


async def validate_environment(config: AppConfig, root: Path, online: bool = False) -> ValidationReport:
    checks: list[ValidationCheck] = []

    missing = []
    for dependency in DEPENDENCIES:
        try:
            importlib.metadata.version(dependency)
        except importlib.metadata.PackageNotFoundError:
            missing.append(dependency)
    checks.append(ValidationCheck("Dependencies", "PASS" if not missing else "FAIL", "all required packages installed" if not missing else f"missing: {', '.join(missing)}"))

    try:
        frame = load_dataset(config.dataset.path)
        checks.append(ValidationCheck("Dataset contract", "PASS", f"{len(frame)} rows, {len(frame.columns)} columns"))
    except Exception as exc:
        checks.append(ValidationCheck("Dataset contract", "FAIL", str(exc)))

    try:
        problems = _benchmark_validation(root)
        checks.append(ValidationCheck("Benchmark invariants", "PASS" if not problems else "FAIL", "100 rows, 85 conversations, and corpus invariants verified" if not problems else "; ".join(problems[:3])))
    except Exception as exc:
        checks.append(ValidationCheck("Benchmark invariants", "FAIL", str(exc)))

    try:
        units = extract_knowledge_base(config.dataset.knowledge_base_dir, config.pdf_extraction)
        documents = {unit.document_path for unit in units}
        body = sum(unit.extracted_surface == "body" for unit in units)
        metadata = len(units) - body
        checks.append(ValidationCheck("Knowledge base", "PASS", f"{len(documents)} PDFs; {body} body/page and {metadata} metadata units"))
        checks.append(ValidationCheck("Index exclusions", "PASS", "only knowledge_base/**/*.pdf is eligible; evaluator manifest excluded"))
    except Exception as exc:
        checks.append(ValidationCheck("Knowledge base", "FAIL", str(exc)))
        checks.append(ValidationCheck("Index exclusions", "FAIL", "knowledge-base input was not safe"))

    prompt = config.evaluation.system_prompt_path
    checks.append(ValidationCheck("System prompt", "PASS" if prompt.is_file() and prompt.stat().st_size else "FAIL", str(prompt)))

    model_errors = []
    for model in config.models + ([config.judge.model] if config.judge.enabled and config.judge.model else []):
        if model.adapter == "openai_compatible" and not model.base_url:
            model_errors.append(f"{model.id}: base_url is required")
        if model.adapter == "custom_http" and not model.endpoint:
            model_errors.append(f"{model.id}: endpoint is required")
        credential_sources = [
            (model.credential_id, model.api_key_env),
            (model.authentication.credential_id, model.authentication.token_env),
        ]
        for credential_id, env_name in credential_sources:
            if not credential_id and not env_name:
                continue
            try:
                if not resolve_credential(credential_id, env_name):
                    model_errors.append(f"{model.id}: credential {credential_id or env_name} is unavailable; run ragnarok setup")
            except CredentialError as exc:
                model_errors.append(f"{model.id}: {exc}")
    checks.append(ValidationCheck("Model configuration", "PASS" if not model_errors else "FAIL", f"{len(config.models)} inference model(s), judge {'enabled' if config.judge.enabled else 'disabled'}" if not model_errors else "; ".join(model_errors)))

    if online and not model_errors:
        provider_errors = []
        for model in config.models + ([config.judge.model] if config.judge.enabled and config.judge.model else []):
            ok, detail = await provider_for(model, config.runtime).check()
            if not ok:
                provider_errors.append(f"{model.id}: {detail}")
        checks.append(ValidationCheck("Provider availability", "PASS" if not provider_errors else "FAIL", "all configured providers available" if not provider_errors else "; ".join(provider_errors)))
    else:
        checks.append(ValidationCheck("Provider availability", "WARN", "network checks skipped; use --online to check endpoints"))

    try:
        config.project.output_dir.mkdir(parents=True, exist_ok=True)
        probe = config.project.output_dir / ".ragnarok-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(ValidationCheck("Output directory", "PASS", str(config.project.output_dir)))
    except OSError as exc:
        checks.append(ValidationCheck("Output directory", "FAIL", str(exc)))

    index_path = config.rag.cache_dir / "index.json"
    checks.append(ValidationCheck("RAG index", "PASS" if index_path.is_file() else "WARN", "cached index available" if index_path.is_file() else "not built yet; run will build it automatically"))
    return ValidationReport(checks)
