from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import questionary
import pytest

from ragnarok.benchmarks._runtime import recoverable_run_dir
from ragnarok.benchmarks.agentdojo import _load_case_checkpoint
from ragnarok.benchmarks.mpib import (
    _best_prior_subject_log,
    _recoverable_mpib_run_dir,
    _successful_responses,
)
from ragnarok.config import ModelConfig
from ragnarok.cli import _latest_incomplete_session, _require_complete_suite, _resume_last_session
from ragnarok.config import config_from_data
from ragnarok.results import ResultStore
from ragnarok.runner import (
    _ensure_resume_ollama_models,
    _pending_resume_ollama_models,
    _resume_subject_required,
)


def _write_suite(root: Path, name: str, status: str, created_at: datetime) -> Path:
    suite = root / "outputs" / name
    suite.mkdir(parents=True)
    manifest = {
        "suite_id": name,
        "status": status,
        "created_at": created_at.isoformat(),
        "configuration": {
            "benchmarks": [{"id": "spikee", "options": {"profile": "light"}}],
            "models": [{"id": "model", "adapter": "ollama", "model": "model:latest"}],
            "output_dir": str(root / "outputs"),
        },
    }
    (suite / "suite_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return suite


def test_only_latest_suite_is_offered_for_resume(tmp_path: Path):
    now = datetime.now(timezone.utc)
    partial = _write_suite(tmp_path, "partial", "partial", now)
    assert _latest_incomplete_session(tmp_path / "outputs")[0] == partial

    _write_suite(tmp_path, "complete", "complete", now + timedelta(seconds=1))
    assert _latest_incomplete_session(tmp_path / "outputs") is None


def test_declining_resume_deletes_only_the_incomplete_suite(monkeypatch, tmp_path: Path):
    suite = _write_suite(tmp_path, "partial", "partial", datetime.now(timezone.utc))

    class Answer:
        def ask(self):
            return False

    monkeypatch.setattr(questionary, "select", lambda *args, **kwargs: Answer())
    assert _resume_last_session(tmp_path) is None
    assert not suite.exists()
    assert (tmp_path / "outputs").is_dir()


def test_accepting_resume_reuses_the_frozen_configuration(monkeypatch, tmp_path: Path):
    suite = _write_suite(tmp_path, "partial", "partial", datetime.now(timezone.utc))

    class Answer:
        def ask(self):
            return True

    monkeypatch.setattr(questionary, "select", lambda *args, **kwargs: Answer())
    config, selected_suite = _resume_last_session(tmp_path)
    assert selected_suite == ("partial", suite)
    assert config.models[0].model == "model:latest"


def test_call_logs_and_native_run_are_recoverable(tmp_path: Path):
    run_dir = tmp_path / "mpib" / "20260101T000000Z"
    model_dir = run_dir / "model"
    model_dir.mkdir(parents=True)
    log_path = model_dir / "requests.jsonl"
    rows = [
        {"call_index": 1, "response": "kept", "error_type": "", "metadata": {"sample_id": "one"}},
        {"call_index": 2, "response": "", "error_type": "ConnectError", "metadata": {"sample_id": "two"}},
    ]
    log_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    assert recoverable_run_dir(tmp_path, "mpib", ["model"]) == run_dir
    assert _successful_responses(log_path) == {"one": "kept"}


def test_mpib_prefers_authoritative_deferred_queue_over_failed_resume(tmp_path: Path):
    benchmark_dir = tmp_path / "mpib"
    deferred = benchmark_dir / "20260101T000000Z"
    failed_resume = benchmark_dir / "20260102T000000Z"
    for run_dir in (deferred, failed_resume):
        model_dir = run_dir / "subject"
        model_dir.mkdir(parents=True)
        (model_dir / "judge_requests.jsonl").write_text(
            json.dumps({"model": "judge-a", "response": "{}"}) + "\n",
            encoding="utf-8",
        )
    (deferred / "run_manifest.json").write_text(json.dumps({
        "status": "judging_deferred",
        "judge": {"configuration": {"model": "judge-a"}},
    }), encoding="utf-8")

    assert _recoverable_mpib_run_dir(tmp_path, ["subject"], "judge-a") == deferred
    assert _recoverable_mpib_run_dir(tmp_path, ["subject"], "judge-b") is None


def test_mpib_finds_complete_prior_subject_log_for_rejudge(tmp_path: Path):
    prior = tmp_path / "mpib" / "old" / "subject"
    current = tmp_path / "mpib" / "new"
    prior.mkdir(parents=True)
    current.mkdir(parents=True)
    rows = [
        {
            "call_index": index,
            "response": f"response-{index}",
            "error_type": "",
            "model": "llama:quantized",
            "metadata": {"sample_id": f"case-{index}"},
        }
        for index in range(1, 4)
    ]
    log_path = prior / "requests.jsonl"
    log_path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    source, responses = _best_prior_subject_log(
        current,
        ModelConfig(id="subject", adapter="ollama", model="llama:quantized"),
        {"case-1", "case-2", "case-3"},
    )

    assert source == log_path
    assert responses == {
        "case-1": "response-1",
        "case-2": "response-2",
        "case-3": "response-3",
    }


def test_agentdojo_checkpoint_ignores_an_incomplete_last_line(tmp_path: Path):
    path = tmp_path / "checkpoint_cases.jsonl"
    path.write_text('{"case_id":"one","response":"kept"}\n{"case_id":', encoding="utf-8")
    assert _load_case_checkpoint(path) == {"one": {"case_id": "one", "response": "kept"}}


def test_partial_suite_is_reported_as_an_error(tmp_path: Path):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "suite_manifest.json").write_text(json.dumps({
        "status": "partial",
        "benchmark_errors": [{"benchmark_id": "agentdojo", "error": "KeyError: user"}],
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match="agentdojo: KeyError: user"):
        _require_complete_suite(suite)

    (suite / "suite_manifest.json").write_text(
        json.dumps({"status": "complete", "benchmark_errors": []}), encoding="utf-8"
    )
    _require_complete_suite(suite)


@pytest.mark.asyncio
async def test_resume_downloads_only_ollama_models_needed_by_unfinished_jobs(monkeypatch, tmp_path: Path):
    suite = tmp_path / "outputs" / "suite"
    store = ResultStore(suite)
    store.set_job_status("suite", "mpib", "subject", "complete")
    store.set_job_status("suite", "agentdojo", "subject", "failed")
    config = config_from_data({
        "benchmarks": [
            {
                "id": "mpib",
                "judge": {"id": "old-judge", "adapter": "ollama", "model": "judge:latest"},
            },
            {"id": "agentdojo"},
        ],
        "models": [
            {
                "id": "subject",
                "adapter": "ollama",
                "model": "llama3.2:3b-instruct-q2_K",
                "base_url": "http://remote:11434",
            }
        ],
        "output_dir": str(tmp_path / "outputs"),
    }, tmp_path)

    required = _pending_resume_ollama_models(config, ("suite", suite))
    assert [model.model for model in required] == ["llama3.2:3b-instruct-q2_K"]

    ensured = []

    class FakeManager:
        def __init__(self, base_url, progress):
            assert base_url == "http://remote:11434"

        async def ensure(self, model):
            ensured.append(model.model)

    monkeypatch.setattr("ragnarok.automation.OllamaModelManager", FakeManager)
    await _ensure_resume_ollama_models(config, ("suite", suite), None)
    assert ensured == ["llama3.2:3b-instruct-q2_K"]


def test_judge_only_mpib_resume_does_not_require_subject_model(tmp_path: Path):
    suite = tmp_path / "outputs" / "suite"
    store = ResultStore(suite)
    store.set_job_status("suite", "mpib", "subject", "judging_deferred")
    config = config_from_data({
        "benchmarks": [{
            "id": "mpib",
            "judge": {
                "id": "judge",
                "adapter": "openai",
                "model": "deepseek/deepseek-v4-flash-0731",
                "credential_id": "openrouter",
            },
        }],
        "models": [{
            "id": "subject",
            "adapter": "ollama",
            "model": "llama3.2:3b-instruct-q2_K",
        }],
        "output_dir": str(tmp_path / "outputs"),
    }, tmp_path)

    assert _pending_resume_ollama_models(config, ("suite", suite)) == []
    assert _resume_subject_required(config, ("suite", suite), config.models[0]) is False
