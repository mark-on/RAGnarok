from __future__ import annotations

import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .core import BenchmarkAdapter


@dataclass(frozen=True)
class BootstrapReport:
    project_root: Path
    python_executable: str
    extras: tuple[str, ...]
    requirements: tuple[str, ...]


CommandRunner = Callable[..., subprocess.CompletedProcess]

_BENCHMARK_SUBMODULES = {
    "poisonedrag": "benchmarks/poisonedrag/upstream",
}


def find_project_root(start: Path) -> Path:
    """Find a source checkout containing pyproject.toml and the benchmark tree."""

    candidates = [start.resolve(), Path(__file__).resolve().parents[2]]
    visited: set[Path] = set()
    for candidate in candidates:
        for path in (candidate, *candidate.parents):
            if path in visited:
                continue
            visited.add(path)
            if (path / "pyproject.toml").is_file() and (path / "benchmarks").is_dir():
                return path
    raise FileNotFoundError(
        "RAGnarok source checkout not found. Run this command from the cloned repository."
    )


def benchmark_extras(benchmarks: Sequence[BenchmarkAdapter]) -> tuple[str, ...]:
    return tuple(sorted({item.info.python_extra for item in benchmarks if item.info.python_extra}))


def project_requirements(root: Path, extras: Sequence[str]) -> tuple[str, ...]:
    """Resolve core and benchmark requirements without reinstalling RAGnarok.

    Installing ``.[extra]`` from inside ``ragnarok.exe`` makes pip uninstall the
    executable that Windows currently has open. Reading the same declarations
    from pyproject.toml installs the dependency union without touching the
    running console script.
    """

    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    groups = project.get("optional-dependencies", {})
    missing = [extra for extra in extras if extra not in groups]
    if missing:
        raise ValueError(f"missing optional dependency groups in pyproject.toml: {', '.join(missing)}")
    ordered = [*project.get("dependencies", [])]
    for extra in extras:
        ordered.extend(groups[extra])
    return tuple(dict.fromkeys(ordered))


def bootstrap_commands(
    root: Path,
    benchmarks: Sequence[BenchmarkAdapter],
    *,
    python_executable: str = sys.executable,
) -> tuple[tuple[str, ...], ...]:
    extras = benchmark_extras(benchmarks)
    requirements = project_requirements(root, extras)
    submodules = tuple(
        _BENCHMARK_SUBMODULES[benchmark.info.id]
        for benchmark in benchmarks
        if benchmark.info.id in _BENCHMARK_SUBMODULES
    )
    return (
        ("git", "submodule", "update", "--init", "--recursive", *submodules),
        (python_executable, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"),
        (python_executable, "-m", "pip", "install", *requirements),
    )


def bootstrap_environment(
    root: Path,
    benchmarks: Sequence[BenchmarkAdapter],
    *,
    runner: CommandRunner = subprocess.run,
    python_executable: str = sys.executable,
    progress: Callable[[str, int, int | None, str], None] | None = None,
    log_path: Path | None = None,
) -> BootstrapReport:
    extras = benchmark_extras(benchmarks)
    requirements = project_requirements(root, extras)
    commands = bootstrap_commands(root, benchmarks, python_executable=python_executable)
    log_handle = None
    try:
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("a", encoding="utf-8")
        labels = ("Official submodules", "Python tooling", "Benchmark dependencies")
        for index, (command, label) in enumerate(zip(commands, labels, strict=True)):
            if progress:
                progress("dependencies", index, len(commands), label)
            kwargs = {"cwd": root, "check": True}
            if log_handle is not None:
                kwargs.update({"stdout": log_handle, "stderr": subprocess.STDOUT, "text": True})
            runner(list(command), **kwargs)
        if progress:
            progress("dependencies", len(commands), len(commands), "Dependencies installed")
    finally:
        if log_handle is not None:
            log_handle.close()
    return BootstrapReport(
        project_root=root,
        python_executable=python_executable,
        extras=extras,
        requirements=requirements,
    )


def torch_backend_summary() -> str:
    try:
        import torch
    except ImportError:
        return "PyTorch unavailable"
    if torch.cuda.is_available():
        name = torch.cuda.get_device_name(0)
        if getattr(torch.version, "hip", None):
            return f"AMD/ROCm — {name}"
        return f"NVIDIA/CUDA — {name}"
    return "CPU — no compatible PyTorch accelerator detected"
