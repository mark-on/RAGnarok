from pathlib import Path
from types import SimpleNamespace
import json

from typer.testing import CliRunner

import ragnarok.cli as cli
from ragnarok.benchmarks import available_benchmarks
from ragnarok.bootstrap import (
    BootstrapReport,
    benchmark_extras,
    bootstrap_commands,
    bootstrap_environment,
    find_project_root,
    project_requirements,
)


def test_bootstrap_is_driven_by_registered_benchmark_metadata():
    benchmarks = available_benchmarks()
    root = Path(__file__).resolve().parents[1]
    assert benchmark_extras(benchmarks) == ("agentdojo", "mpib", "poisonedrag", "spikee")
    commands = bootstrap_commands(root, benchmarks, python_executable="test-python")
    assert commands[0] == (
        "git", "submodule", "update", "--init", "--recursive",
        "benchmarks/poisonedrag/upstream",
    )
    assert commands[1] == ("test-python", "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")
    assert commands[2][:4] == ("test-python", "-m", "pip", "install")
    assert "beir>=2.0" in commands[2]
    assert "datasets==4.8.4" in commands[2]
    assert "agentdojo==0.1.35" in commands[2]
    assert "spikee[ollama]==0.9.1" in commands[2]
    assert "-e" not in commands[2]
    assert ".[poisonedrag]" not in commands[2]


def test_project_requirements_include_core_and_registered_extra():
    root = Path(__file__).resolve().parents[1]
    requirements = project_requirements(root, ("poisonedrag",))
    assert "typer>=0.12" in requirements
    assert "xlsxwriter>=3.2" in requirements
    assert "beir>=2.0" in requirements
    assert len(requirements) == len(set(requirements))


def test_bootstrap_runs_every_command_from_project_root(tmp_path):
    source_root = Path(__file__).resolve().parents[1]
    (tmp_path / "pyproject.toml").write_bytes((source_root / "pyproject.toml").read_bytes())
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    report = bootstrap_environment(
        tmp_path,
        available_benchmarks(),
        runner=runner,
        python_executable="test-python",
    )
    assert len(calls) == 3
    assert all(call[1] == {"cwd": tmp_path, "check": True} for call in calls)
    assert "beir>=2.0" in report.requirements


def test_find_project_root_locates_checkout():
    expected = Path(__file__).resolve().parents[1]
    assert find_project_root(expected / "src" / "ragnarok") == expected


def test_setup_cli_reports_success_without_real_install(monkeypatch, tmp_path):
    benchmark = SimpleNamespace(
        info=SimpleNamespace(name="Test benchmark"),
        validate_installation=lambda: [],
        validate_prepared=lambda: [],
        prepare=lambda **_kwargs: {},
    )
    monkeypatch.setattr(cli, "find_project_root", lambda _path: tmp_path)
    monkeypatch.setattr(cli, "available_benchmarks", lambda: (benchmark,))
    monkeypatch.setattr(
        cli,
        "bootstrap_environment",
        lambda _root, _benchmarks, **_kwargs: BootstrapReport(tmp_path, "test-python", ("test",), ("dependency",)),
    )
    monkeypatch.setattr(cli, "torch_backend_summary", lambda: "CPU")

    result = CliRunner().invoke(cli.app, ["setup", "--plain"])

    assert result.exit_code == 0
    assert "completed" in result.stdout.lower()
    manifest = json.loads((tmp_path / ".ragnarok" / "setup_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "ready"
    assert manifest["benchmarks"]["test_benchmark"]["status"] == "ready"


def test_interactive_setup_requests_and_stores_huggingface_token(monkeypatch, tmp_path):
    prepared = {"ready": False}

    def prepare(**_kwargs):
        prepared["ready"] = True
        return {}

    benchmark = SimpleNamespace(
        info=SimpleNamespace(id="mpib", name="MPIB"),
        validate_installation=lambda: [],
        validate_prepared=lambda: [] if prepared["ready"] else ["MPIB is not prepared"],
        prepare=prepare,
    )
    stored = {}
    monkeypatch.setattr(cli, "find_project_root", lambda _path: tmp_path)
    monkeypatch.setattr(cli, "available_benchmarks", lambda: (benchmark,))
    monkeypatch.setattr(cli, "store_credential", lambda credential_id, secret: stored.update({credential_id: secret}))
    monkeypatch.setattr(
        cli,
        "bootstrap_environment",
        lambda _root, _benchmarks, **_kwargs: BootstrapReport(tmp_path, "test-python", ("mpib",), ("dependency",)),
    )
    monkeypatch.setattr(cli, "torch_backend_summary", lambda: "CPU")

    result = CliRunner().invoke(cli.app, ["setup"], input="hf_test_token\n")

    assert result.exit_code == 0
    assert stored == {"huggingface": "hf_test_token"}
    assert cli.os.environ["HF_TOKEN"] == "hf_test_token"
    cli.os.environ.pop("HF_TOKEN", None)
    assert "Hugging Face token" in result.output


def test_setup_collects_benchmark_failure_without_third_party_traceback(monkeypatch, tmp_path):
    def fail_prepare(**_kwargs):
        raise RuntimeError("upstream preparation failed")

    benchmark = SimpleNamespace(
        info=SimpleNamespace(id="broken", name="Broken benchmark"),
        validate_installation=lambda: [],
        validate_prepared=lambda: ["cache missing"],
        prepare=fail_prepare,
    )
    monkeypatch.setattr(cli, "find_project_root", lambda _path: tmp_path)
    monkeypatch.setattr(cli, "available_benchmarks", lambda: (benchmark,))
    monkeypatch.setattr(
        cli,
        "bootstrap_environment",
        lambda _root, _benchmarks, **_kwargs: BootstrapReport(tmp_path, "test-python", (), ()),
    )

    result = CliRunner().invoke(cli.app, ["setup", "--plain"])

    assert result.exit_code == 1
    assert "setup incomplete" in result.output
    assert "Traceback" not in result.output
    manifest = json.loads((tmp_path / ".ragnarok" / "setup_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["dependencies"]["status"] == "ready"
    assert manifest["benchmarks"]["broken"]["status"] == "failed"
