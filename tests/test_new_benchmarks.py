import asyncio
import hashlib
import json

import pytest

from ragnarok.benchmarks.mpib import (
    JUDGE_SYSTEM_PROMPT,
    MPIBAdapter,
    MPIBOptions,
    _AdaptiveJudgeLimiter,
    assemble_blind_prompt,
    deterministic_stratified_subset,
    _materialize_official_split,
    _reconstruction_summary,
)
from ragnarok.config import ModelConfig, RuntimeConfig, config_from_data
from ragnarok.benchmarks._runtime import ModelCallError


def test_mpib_profiles_and_judge_contract():
    adapter = MPIBAdapter()
    assert adapter.info.requires_judge is True
    assert adapter.estimate_model_calls({"profile": "light"}) == 120
    assert adapter.estimate_model_calls({"profile": "medium"}) == 300
    assert adapter.estimate_judge_calls({"profile": "medium"}) == 300
    assert "Return ONLY a valid JSON object" in JUDGE_SYSTEM_PROMPT
    assert hashlib.sha256(JUDGE_SYSTEM_PROMPT.encode()).hexdigest()


@pytest.mark.asyncio
async def test_mpib_judge_concurrency_decreases_on_failure_and_recovers_gradually():
    limiter = _AdaptiveJudgeLimiter(4, success_window=1)

    for expected in (3, 2, 1):
        await limiter.acquire()
        await limiter.release(False)
        assert limiter.limit == expected

    # Recovery thresholds are at least four successful calls per active worker.
    for _ in range(4):
        await limiter.acquire()
        await limiter.release(True)
    assert limiter.limit == 2
    for _ in range(8):
        await limiter.acquire()
        await limiter.release(True)
    assert limiter.limit == 3
    for _ in range(12):
        await limiter.acquire()
        await limiter.release(True)
    assert limiter.limit == 4
    assert limiter.audit()["minimum_reached"] == 1


def test_mpib_blind_prompt_masks_internal_document_roles():
    prompt = assemble_blind_prompt({
        "user_query": "Question",
        "contexts": [{"role": "poisoned_update", "text": "Context"}],
    })
    assert "clinical_update" in prompt
    assert "poisoned_update" not in prompt
    assert "<USER_QUERY>\nQuestion\n</USER_QUERY>" in prompt


def test_mpib_subset_is_deterministic_and_stratified():
    rows = [
        {"sample_id": f"{vector}-{index}", "vector": vector, "scenario": "S1"}
        for vector in ("V0", "V1", "V2")
        for index in range(10)
    ]
    first = deterministic_stratified_subset(rows, 9)
    second = deterministic_stratified_subset(rows, 9)
    assert first == second
    assert {row["vector"] for row in first} == {"V0", "V1", "V2"}


def test_mpib_materializes_mixed_null_and_structured_contexts_without_schema_inference(tmp_path):
    source = tmp_path / "official.jsonl"
    destination = tmp_path / "cache" / "test.jsonl"
    rows = [
        {"sample_id": "null-context", "vector": "V0", "contexts": None},
        {
            "sample_id": "structured-context",
            "vector": "V2",
            "contexts": [{
                "doc_id": "doc-1",
                "role": "poisoned_update",
                "text": "[REDACTED_PAYLOAD]",
                "template_id": "template-1",
                "reconstruction_hook": {"features": {"lines": 1}},
            }],
        },
    ]
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    count = _materialize_official_split(source, destination, {"template-1": "restored payload"})

    materialized = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    assert count == 2
    assert materialized[0]["contexts"] is None
    assert materialized[1]["contexts"][0]["text"] == "restored payload"
    assert materialized[1]["contexts"][0]["reconstruction_hook"]["features"]["lines"] == 1


def test_mpib_uses_official_structural_mock_when_registry_is_unavailable(tmp_path):
    source = tmp_path / "official.jsonl"
    destination = tmp_path / "cache" / "test.jsonl"
    source.write_text(json.dumps({
        "sample_id": "v2",
        "vector": "V2",
        "contexts": [{
            "role": "poisoned_update",
            "text": "prefix [REDACTED_PAYLOAD] suffix",
            "rule_family_id": "OverrideRule",
            "template_id": "restricted-template",
            "reconstruction_hook": {"features": {"words": 3}},
        }],
    }) + "\n", encoding="utf-8")

    _materialize_official_split(source, destination, {})

    row = json.loads(destination.read_text(encoding="utf-8"))
    document = row["contexts"][0]
    assert document["text"] == (
        "[ADVERSARIAL_PAYLOAD_PLACEHOLDER: OverrideRule] (payload) (payload) (payload)"
    )
    assert document["reconstruction_status"] == "structural_mock"
    assert _reconstruction_summary(destination) == {
        "restored_fidelity": 0,
        "structural_mock": 1,
        "unresolved_redactions": 0,
    }


def test_role_models_are_frozen_in_benchmark_configuration(tmp_path):
    role_model = {"id": "judge", "adapter": "ollama", "model": "judge-model", "temperature": 0}
    config = config_from_data({
        "benchmarks": [{"id": "mpib", "options": {"profile": "light"}, "judge": role_model}],
        "models": [{"id": "subject", "adapter": "ollama", "model": "subject-model"}],
    }, tmp_path)
    assert config.benchmarks[0].judge.model == "judge-model"
    assert config.benchmarks[0].judge.temperature == 0


def test_runtime_keeps_subject_serial_and_bounds_parallel_work():
    runtime = RuntimeConfig(judge_concurrency=8, postprocess_workers=3)
    assert runtime.subject_concurrency == 1
    assert runtime.judge_concurrency == 8
    assert runtime.postprocess_workers == 3
    with pytest.raises(ValueError):
        RuntimeConfig(subject_concurrency=2)


def test_mpib_parallelizes_remote_judge_but_never_subject(monkeypatch, tmp_path):
    class FakeSession:
        subject_active = 0
        subject_max = 0
        judge_active = 0
        judge_max = 0

        def __init__(self, model, _runtime, _log_path, *, role, total, progress):
            self.model = model
            self.role = role
            self.total = total

        async def generate_async(self, **_kwargs):
            if self.role == "subject_inference":
                FakeSession.subject_active += 1
                FakeSession.subject_max = max(FakeSession.subject_max, FakeSession.subject_active)
                await asyncio.sleep(0.001)
                FakeSession.subject_active -= 1
                return "subject response"
            FakeSession.judge_active += 1
            FakeSession.judge_max = max(FakeSession.judge_max, FakeSession.judge_active)
            await asyncio.sleep(0.05)
            FakeSession.judge_active -= 1
            return json.dumps({
                "harm_types": [],
                "severity": 2,
                "attack_success": True,
                "refusal_correctness": "not_refusal",
                "rationale_short": "test",
            })

        def summary(self, *, concurrency=1):
            return {"inference_workers": concurrency}

        async def aclose(self):
            return None

    monkeypatch.setattr("ragnarok.benchmarks.mpib.ModelCallSession", FakeSession)
    rows = [
        {
            "sample_id": f"case-{index}",
            "vector": "V1",
            "scenario": "test",
            "user_query": "query",
            "contexts": [],
        }
        for index in range(8)
    ]
    result = asyncio.run(MPIBAdapter()._run_model_async(
        ModelConfig(id="subject", adapter="ollama", model="subject"),
        ModelConfig(id="judge", adapter="openai", model="judge", credential_id="judge"),
        RuntimeConfig(judge_concurrency=3),
        rows,
        MPIBOptions(profile="light"),
        tmp_path,
        None,
    ))

    assert FakeSession.subject_max == 1
    assert FakeSession.judge_max == 2
    assert result["subject"]["inference_workers"] == 1
    assert result["judge"]["inference_workers"] == 2
    cases = [json.loads(line) for line in (tmp_path / "subject" / "normalized" / "cases.jsonl").read_text().splitlines()]
    assert [case["case_id"] for case in cases] == [f"case-{index}" for index in range(8)]


def test_mpib_runs_all_local_subject_calls_before_local_judge(monkeypatch, tmp_path):
    events = []

    class FakeSession:
        def __init__(self, model, _runtime, _log_path, *, role, total, progress):
            self.model = model
            self.role = role

        async def generate_async(self, **kwargs):
            events.append(self.role)
            if self.role == "subject_inference":
                assert kwargs["stop_sequences"] == ["<|im_end|>", "<|endoftext|>"]
                return "subject response"
            return json.dumps({
                "harm_types": [],
                "severity": 0,
                "attack_success": False,
                "refusal_correctness": "not_refusal",
                "rationale_short": "test",
            })

        def summary(self, *, concurrency=1):
            return {"inference_workers": concurrency}

        async def aclose(self):
            return None

    monkeypatch.setattr("ragnarok.benchmarks.mpib.ModelCallSession", FakeSession)
    rows = [
        {"sample_id": f"case-{index}", "vector": "V1", "scenario": "test", "user_query": "q", "contexts": []}
        for index in range(3)
    ]
    asyncio.run(MPIBAdapter()._run_model_async(
        ModelConfig(id="subject", adapter="ollama", model="subject"),
        ModelConfig(id="judge", adapter="ollama", model="judge"),
        RuntimeConfig(), rows, MPIBOptions(profile="light"), tmp_path, None,
    ))

    assert events == ["subject_inference"] * 3 + ["judge_inference"] * 3


def test_mpib_rejudge_reuses_all_prior_subject_responses(monkeypatch, tmp_path):
    prior_model_dir = tmp_path / "old" / "subject"
    prior_model_dir.mkdir(parents=True)
    rows = [
        {
            "sample_id": f"case-{index}",
            "vector": "V1",
            "scenario": "test",
            "user_query": "query",
            "contexts": [],
        }
        for index in range(3)
    ]
    (prior_model_dir / "requests.jsonl").write_text(
        "".join(
            json.dumps({
                "call_index": index + 1,
                "response": f"saved response {index}",
                "error_type": "",
                "model": "subject-model",
                "metadata": {"sample_id": row["sample_id"]},
            }) + "\n"
            for index, row in enumerate(rows)
        ),
        encoding="utf-8",
    )

    class FakeSession:
        subject_calls = 0

        def __init__(self, model, _runtime, _log_path, *, role, total, progress):
            self.model = model
            self.role = role

        async def generate_async(self, **_kwargs):
            if self.role == "subject_inference":
                FakeSession.subject_calls += 1
                raise AssertionError("saved Subject response was not reused")
            return json.dumps({
                "harm_types": [],
                "severity": 0,
                "attack_success": False,
                "refusal_correctness": "not_refusal",
                "rationale_short": "test",
            })

        def summary(self, *, concurrency=1):
            return {"inference_workers": concurrency}

        async def aclose(self):
            return None

    monkeypatch.setattr("ragnarok.benchmarks.mpib.ModelCallSession", FakeSession)
    run_dir = tmp_path / "new"
    run_dir.mkdir()
    result = asyncio.run(MPIBAdapter()._run_model_async(
        ModelConfig(id="subject", adapter="ollama", model="subject-model"),
        ModelConfig(id="judge", adapter="openai", model="new-judge", credential_id="judge"),
        RuntimeConfig(),
        rows,
        MPIBOptions(profile="light"),
        run_dir,
        None,
    ))

    assert FakeSession.subject_calls == 0
    assert result["status"] == "complete"
    assert json.loads(
        (run_dir / "subject" / "subject_reuse.json").read_text(encoding="utf-8")
    )["reused_responses"] == 3


def test_mpib_preserves_subject_work_when_remote_judge_fails(monkeypatch, tmp_path):
    subject_calls = 0

    class FakeSession:
        def __init__(self, model, _runtime, _log_path, *, role, total, progress):
            self.model = model
            self.role = role

        async def generate_async(self, **_kwargs):
            nonlocal subject_calls
            if self.role == "subject_inference":
                subject_calls += 1
                await asyncio.sleep(0.01)
                return "subject response"
            raise ModelCallError("judge_inference", 1, "ConnectError", "judge unavailable")

        def summary(self, *, concurrency=1):
            return {"inference_workers": concurrency}

        async def aclose(self):
            return None

    monkeypatch.setattr("ragnarok.benchmarks.mpib.ModelCallSession", FakeSession)
    rows = [
        {"sample_id": f"case-{index}", "vector": "V1", "scenario": "test", "user_query": "q", "contexts": []}
        for index in range(20)
    ]

    from ragnarok.benchmarks import mpib as mpib_module
    real_pump = mpib_module.AdaptiveJudgePump
    monkeypatch.setattr(
        mpib_module,
        "AdaptiveJudgePump",
        lambda *args, **kwargs: real_pump(
            *args,
            **kwargs,
            outage_seconds=0.02,
            heartbeat_seconds=0.01,
        ),
    )

    async def execute():
        result = await MPIBAdapter()._run_model_async(
            ModelConfig(id="subject", adapter="ollama", model="subject"),
            ModelConfig(id="judge", adapter="openai", model="judge", credential_id="judge"),
            RuntimeConfig(judge_concurrency=4), rows, MPIBOptions(profile="light"), tmp_path, None,
        )
        await mpib_module.finalize_deferred_mpib(timeout_seconds=0)
        return result

    result = asyncio.run(execute())
    assert subject_calls == len(rows)
    assert result["status"] == "judging_deferred"
