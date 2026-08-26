from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ragnarok.cli import app
from ragnarok.pdf_report import _security_utility_score, discover_report_runs, generate_pdf_report_bundle
from ragnarok.results import ResultStore
from ragnarok.results.schemas import UniversalCase


def _suite(root: Path, name: str, model_id: str, model_name: str, success: bool) -> Path:
    suite = root / "outputs" / name
    suite.mkdir(parents=True)
    manifest = {
        "suite_id": name,
        "status": "complete",
        "created_at": "2026-08-26T10:00:00+00:00",
        "execution": {"started_at_local": "2026-08-26T12:00:00+02:00"},
        "configuration": {
            "models": [{"id": model_id, "model": model_name, "adapter": "ollama"}],
            "benchmarks": [{"id": "poisonedrag", "options": {"profile": "full"}}],
        },
    }
    (suite / "suite_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    store = ResultStore(suite)
    store.add_cases([
        UniversalCase(
            suite_id=name,
            benchmark_id="poisonedrag",
            model_id=model_id,
            case_id="nq-1",
            attack_family="knowledge_corruption",
            prompt="Question",
            response="Answer",
            official_evaluation={"attack_success": success, "dataset": "nq"},
        )
    ])
    return suite


def test_discovers_and_combines_single_runs_into_pdf(tmp_path: Path):
    _suite(tmp_path, "qwen_q8", "qwen_q8", "qwen2.5:7b-instruct-q8_0", True)
    _suite(tmp_path, "qwen_q4", "qwen_q4", "qwen2.5:7b-instruct-q4_K_M", False)
    runs = discover_report_runs(tmp_path / "outputs")

    pdf, csv, manifest = generate_pdf_report_bundle(runs, tmp_path / "combined")

    assert pdf.read_bytes().startswith(b"%PDF")
    from pypdf import PdfReader
    pages = PdfReader(str(pdf)).pages
    first_page = pages[0].extract_text() or ""
    assert "Quantization Security Report" in first_page
    assert "Overall results" in first_page
    assert "TOTAL CASES" in first_page
    assert "SUBS" in first_page
    assert "Security-Utility Balance Score" in first_page
    assert "SecurityScore" in first_page
    assert "ASR means Attack Success Rate" in first_page
    assert "Security outcome by benchmark" in (pages[1].extract_text() or "")
    assert "Legend colors are placed" not in "".join(page.extract_text() or "" for page in pages)
    assert csv.is_file()
    report_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    assert report_manifest["cases"] == 2
    assert len(report_manifest["source_runs"]) == 2
    assert report_manifest["security_utility_scores"]


def test_security_utility_score_is_bounded_and_penalizes_low_utility():
    assert _security_utility_score(0.0, 1.0) == 100.0
    assert _security_utility_score(1.0, 1.0) == 0.0
    assert _security_utility_score(0.4, 0.1) < _security_utility_score(0.4, 0.5)


def test_report_command_supports_repeatable_run_options(monkeypatch, tmp_path: Path):
    _suite(tmp_path, "qwen_q8", "qwen_q8", "qwen2.5:7b-instruct-q8_0", True)
    _suite(tmp_path, "qwen_q4", "qwen_q4", "qwen2.5:7b-instruct-q4_K_M", False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (tmp_path / "benchmarks").mkdir()

    result = CliRunner().invoke(
        app,
        ["report", "--run", "qwen_q8", "--run", "qwen_q4", "--output", "pdf-output"],
    )

    assert result.exit_code == 0, result.output
    assert "Completed" in result.output
    assert (tmp_path / "pdf-output" / "report.pdf").is_file()
