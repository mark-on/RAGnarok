from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import subprocess
from pathlib import Path

from . import __version__
from .config import AppConfig, configuration_hash
from .schemas import utc_now


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.pdf")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(hash_file(path).encode())
    return digest.hexdigest()


def git_commit(root: Path) -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return "unavailable"


def dependency_versions() -> dict[str, str]:
    result = {}
    for package in ("typer", "questionary", "keyring", "pydantic", "PyYAML", "httpx", "pandas", "numpy", "sentence-transformers", "pypdf", "matplotlib", "reportlab"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = "not-installed"
    return result


def build_manifest(config: AppConfig, model_id: str, run_id: str, index_fingerprint: str, root: Path) -> dict:
    system_prompt = config.evaluation.system_prompt_path.read_bytes()
    model = next(item for item in config.models if item.id == model_id)
    return {
        "experiment_id": config.project.experiment_id,
        "model_run_id": run_id,
        "framework_version": __version__,
        "git_commit": git_commit(root),
        "dataset_hash": hash_file(config.dataset.path),
        "knowledge_base_hash": hash_tree(config.dataset.knowledge_base_dir),
        "pdf_extraction_configuration": config.pdf_extraction.model_dump(mode="json"),
        "embedding_model": config.rag.embedding_model,
        "chunking_configuration": {"chunk_size": config.rag.chunk_size, "chunk_overlap": config.rag.chunk_overlap},
        "retrieval_configuration": {"top_k": config.rag.top_k, "index_fingerprint": index_fingerprint},
        "system_prompt_hash": hashlib.sha256(system_prompt).hexdigest(),
        "inference_model": {"id": model.id, "adapter": model.adapter, "model": model.model, "temperature": model.temperature, "max_output_tokens": model.max_output_tokens},
        "judge_model": ({"adapter": config.judge.model.adapter, "model": config.judge.model.model} if config.judge.enabled and config.judge.model else None),
        "configuration_hash": configuration_hash(config),
        "dependency_versions": dependency_versions(),
        "start_time": utc_now(), "end_time": None,
        "host": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
    }


def write_manifest(path: Path, manifest: dict) -> None:
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
