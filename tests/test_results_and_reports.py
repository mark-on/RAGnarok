from pathlib import Path
import json

import pytest

from ragnarok.reports import generate_reports
from ragnarok.results import ResultStore
from ragnarok.results.schemas import UniversalCase
from ragnarok.config import config_from_data
from ragnarok.interrupts import RunInterrupted
from ragnarok.runner import run_experiment, validate_run_configuration


def _case(tmp_path: Path, *, model_id: str = "model", dataset: str = "nq", success: bool = True) -> UniversalCase:
    return UniversalCase(
        suite_id="suite", benchmark_id="poisonedrag", model_id=model_id, case_id=f"case-{dataset}",
        attack_family="knowledge_corruption", prompt="Question with retrieved contexts", response="False answer",
        reference_answer="True answer", adversarial_answer="False answer", injected_contexts=["poison"],
        official_evaluation={
            "attack_success": success,
            "dataset": dataset,
            "poisoned_contexts_in_top_k": 4,
            "top_k": 5,
        },
    )


def test_store_exports_sqlite_and_jsonl(tmp_path: Path):
    store = ResultStore(tmp_path)
    case = _case(tmp_path)
    store.add_cases([case])
    assert store.db_path.is_file()
    assert (tmp_path / "data" / "cases.jsonl").is_file()
    assert (tmp_path / "data" / "metrics.jsonl").is_file()


def test_batched_store_writes_match_individual_operations(tmp_path: Path):
    case = _case(tmp_path)
    call = {
        "suite_id": "suite", "benchmark_id": "poisonedrag", "model_id": "model",
        "call_id": "subject:1", "response": "False answer",
    }
    metric = ("suite", "poisonedrag", "model", "official", {"ASR": 1.0})
    artifact = ("suite", "poisonedrag", "model", "native_run", tmp_path / "native")

    individual = ResultStore(tmp_path / "individual")
    individual.add_cases([case])
    individual.add_model_calls([call])
    individual.add_metrics(*metric)
    individual.add_artifact(*artifact)

    batched = ResultStore(tmp_path / "batched")
    batched.add_batch(cases=[case], model_calls=[call], metrics=[metric], artifacts=[artifact])

    for filename in ("cases.jsonl", "model_calls.jsonl", "metrics.jsonl"):
        assert (batched.universal_dir / filename).read_text(encoding="utf-8") == (
            individual.universal_dir / filename
        ).read_text(encoding="utf-8")
    assert batched.cases() == individual.cases()
    assert batched.metrics() == individual.metrics()


def test_rejudge_replaces_stale_canonical_judge_calls(tmp_path: Path):
    store = ResultStore(tmp_path)
    common = {
        "suite_id": "suite",
        "benchmark_id": "mpib",
        "model_id": "subject",
        "model_role": "judge",
    }
    store.add_model_calls([
        {**common, "call_id": f"judge:{index}", "provider_model": "old-judge"}
        for index in range(1, 4)
    ] + [{
        "suite_id": "suite",
        "benchmark_id": "mpib",
        "model_id": "subject",
        "model_role": "subject",
        "call_id": "subject:1",
        "provider_model": "subject-model",
    }])

    store.add_model_calls([
        {**common, "call_id": f"judge:{index}", "provider_model": "new-judge"}
        for index in range(1, 3)
    ])

    calls = store.model_calls()
    judge_calls = [row for row in calls if row["model_role"] == "judge"]
    assert len(judge_calls) == 2
    assert {row["provider_model"] for row in judge_calls} == {"new-judge"}
    assert any(row["model_role"] == "subject" for row in calls)


def test_xlsx_reports_are_generated_from_stored_results(tmp_path: Path):
    cases = [_case(tmp_path, dataset="nq"), _case(tmp_path, dataset="hotpotqa", success=False)]
    paths = generate_reports(tmp_path, cases, execution={"started_at_utc": "2026-08-17T19:42:08+00:00"})
    assert paths == [tmp_path / "report.xlsx"]
    assert all(path.read_bytes().startswith(b"PK") for path in paths)
    assert (tmp_path / "cases.csv").is_file()
    assert (tmp_path / "summary.csv").is_file()
    assert (tmp_path / "metrics.json").is_file()
    assert "Start here" in (tmp_path / "README.txt").read_text(encoding="utf-8")
    assert (tmp_path / "report.json").is_file()
    report_data = json.loads((tmp_path / "report.json").read_text())
    assert report_data["models"]["model"]["attack_success_rate"] == 0.5
    assert report_data["execution"]["started_at_utc"] == "2026-08-17T19:42:08+00:00"
    assert any(row["axis"] == "Dataset" and row["category"] == "NQ" for row in report_data["taxonomy"])


def test_spikee_report_uses_native_attack_taxonomy(tmp_path: Path):
    case = UniversalCase(
        suite_id="suite",
        benchmark_id="spikee",
        model_id="model",
        case_id="spikee-1",
        attack_family="direct_prompt_injection",
        prompt="prompt",
        response="response",
        official_evaluation={
            "attack_success": True,
            "native_result": {
                "instruction_type": "data-exfil-markdown",
                "jailbreak_type": "new-instructions",
                "injected": "true",
            },
        },
    )
    generate_reports(tmp_path, [case])
    taxonomy = json.loads((tmp_path / "report.json").read_text())["taxonomy"]

    assert any(
        row["axis"] == "Security objective" and row["category"] == "Data exfiltration"
        for row in taxonomy
    )
    assert any(
        row["axis"] == "Entry vector" and row["category"] == "Injected application content"
        for row in taxonomy
    )


@pytest.mark.asyncio
async def test_run_preflight_fails_before_creating_results(monkeypatch, tmp_path: Path):
    class Info:
        name = "Fake"
        requires_judge = False
        requires_attacker = False

    class Adapter:
        info = Info()

        def validate_options(self, options):
            return options

        def validate_installation(self):
            return []

        def validate_prepared(self):
            return []

    class Provider:
        async def check(self):
            return False, "model is unavailable"

        async def aclose(self):
            return None

    monkeypatch.setattr("ragnarok.runner.benchmark_for", lambda _id: Adapter())
    monkeypatch.setattr("ragnarok.runner.provider_for", lambda *_args: Provider())
    config = config_from_data({
        "benchmarks": [{"id": "fake"}],
        "models": [{"id": "model", "adapter": "ollama", "model": "missing"}],
        "output_dir": str(tmp_path / "outputs"),
    }, tmp_path)

    with pytest.raises(ValueError, match="model is unavailable"):
        await validate_run_configuration(config)
    assert not (tmp_path / "outputs").exists()


def test_group_report_contains_model_subfolders(tmp_path: Path):
    cases = [_case(tmp_path, model_id="fp16"), _case(tmp_path, model_id="q4", success=False)]
    paths = generate_reports(tmp_path, cases)
    assert paths[0] == tmp_path / "report.xlsx"
    assert (tmp_path / "models" / "fp16" / "cases.csv").is_file()
    assert (tmp_path / "models" / "q4" / "metrics.json").is_file()
    assert "report.xlsx" in (tmp_path / "README.txt").read_text(encoding="utf-8")
    report_data = json.loads((tmp_path / "report.json").read_text())
    assert report_data["quantization_comparison"][0]["paired_cases"] == 1


def test_report_separates_subject_and_judge_performance(tmp_path: Path):
    calls = [
        {
            "model_id": "q4", "benchmark_id": "mpib", "model_role": "subject",
            "provider_model": "qwen:q4", "input_tokens": 100, "output_tokens": 20,
            "wall_duration_seconds": 2.0, "error_type": "",
        },
        {
            "model_id": "q4", "benchmark_id": "mpib", "model_role": "judge",
            "provider_model": "deepseek-v4-flash", "input_tokens": 200, "output_tokens": 10,
            "wall_duration_seconds": 1.0, "error_type": "",
        },
    ]
    generate_reports(tmp_path, [_case(tmp_path, model_id="q4")], model_calls=calls)
    performance = json.loads((tmp_path / "report.json").read_text())["performance"]
    assert [row["role"] for row in performance] == ["judge", "subject"]
    assert next(row for row in performance if row["role"] == "subject")["output_tokens_per_second"] == 10


@pytest.mark.asyncio
async def test_suite_runner_normalizes_and_reports(monkeypatch, tmp_path: Path):
    class FakeAdapter:
        def validate_options(self, options):
            return options

        async def run(self, *, models, output_dir, **_kwargs):
            run_dir = output_dir / "fake" / "run"
            model_dir = run_dir / models[0].id
            (model_dir / "native").mkdir(parents=True)
            (model_dir / "normalized").mkdir()
            (run_dir / "run_manifest.json").write_text(json.dumps({"benchmark": "fake"}), encoding="utf-8")
            (model_dir / "native" / "metrics.json").write_text('{"score": 1}', encoding="utf-8")
            payload = _case(tmp_path).model_dump(exclude={"suite_id"}) | {"benchmark_id": "fake"}
            (model_dir / "normalized" / "cases.jsonl").write_text(json.dumps(payload) + "\n", encoding="utf-8")
            return [run_dir]

    monkeypatch.setattr("ragnarok.runner.benchmark_for", lambda _id: FakeAdapter())
    config = config_from_data({
        "benchmarks": [{"id": "fake"}],
        "models": [{"id": "model", "adapter": "ollama", "model": "unused"}],
        "output_dir": str(tmp_path / "outputs"),
    }, tmp_path)
    outputs = await run_experiment(config)
    result_dir = outputs[0]
    assert result_dir.parent == tmp_path / "outputs"
    assert result_dir.name.startswith("model_")
    assert (result_dir / "results.sqlite").is_file()
    assert (result_dir / "report.xlsx").is_file()
    assert (result_dir / "cases.csv").is_file()
    manifest = json.loads((result_dir / "suite_manifest.json").read_text())
    assert manifest["status"] == "complete"
    assert manifest["execution"]["started_at_utc"]
    assert manifest["execution"]["completed_at_utc"]
    assert manifest["execution"]["elapsed_seconds"] >= 0


@pytest.mark.asyncio
async def test_cancelled_run_writes_a_cancelled_manifest(monkeypatch, tmp_path: Path):
    class InterruptedAdapter:
        def validate_options(self, options):
            return options

        async def run(self, **_kwargs):
            raise RunInterrupted("stop confirmed by user")

    monkeypatch.setattr("ragnarok.runner.benchmark_for", lambda _id: InterruptedAdapter())
    config = config_from_data({
        "benchmarks": [{"id": "fake"}],
        "models": [{"id": "model", "adapter": "ollama", "model": "unused"}],
        "output_dir": str(tmp_path / "outputs"),
    }, tmp_path)
    with pytest.raises(RunInterrupted):
        await run_experiment(config)

    result_dir = next((tmp_path / "outputs").iterdir())
    manifest = json.loads((result_dir / "suite_manifest.json").read_text())
    assert manifest["status"] == "cancelled"
    assert manifest["jobs"][0]["status"] == "cancelled"
