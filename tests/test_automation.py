from pathlib import Path
import os

import pytest

from ragnarok.automation import load_automation
from ragnarok.benchmarks.agentdojo import (
    MAX_LLM_CALLS_PER_CASE,
    AgentDojoAdapter,
    _BoundedOpenAIClient,
    _RecordedOpenAIClient,
    _guard_malformed_trace_evaluators,
    canonical_security_cases,
    normalize_agentdojo_outcome,
)
from ragnarok.benchmarks.spikee import MAX_OUTPUT_TOKENS, SPIKEEAdapter, SPIKEEOptions
from ragnarok.benchmarks.spikee_target import RAGnarokLLMTarget
from ragnarok.config import ModelConfig, RuntimeConfig
from ragnarok.interrupts import RunInterrupted
from ragnarok.results import ResultStore


def test_automation_file_filters_disabled_models(tmp_path: Path):
    path = tmp_path / "automation.toml"
    path.write_text(
        """
version = 1
[automation]
output_dir = "results"
[[benchmarks]]
id = "poisonedrag"
[benchmarks.options]
profile = "light"
[[models]]
id = "disabled"
adapter = "ollama"
model = "unused"
enabled = false
[[models]]
id = "enabled"
adapter = "ollama"
model = "example:q4"
enabled = true
""",
        encoding="utf-8",
    )
    configuration = load_automation(path, tmp_path)
    assert [model.id for model in configuration.models] == ["enabled"]
    assert configuration.automation.output_dir == (tmp_path / "results").resolve()


def test_automation_file_requires_an_enabled_model(tmp_path: Path):
    path = tmp_path / "automation.toml"
    path.write_text(
        """
[[benchmarks]]
id = "poisonedrag"
[[models]]
id = "disabled"
adapter = "ollama"
model = "unused"
enabled = false
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="enabled model"):
        load_automation(path, tmp_path)


def test_checkpoint_jobs_are_persistent(tmp_path: Path):
    store = ResultStore(tmp_path)
    store.set_job_status("suite", "poisonedrag", "q4", "complete")
    reopened = ResultStore(tmp_path)
    assert reopened.job_status("suite", "poisonedrag", "q4") == "complete"


def test_new_benchmark_profile_counts():
    agentdojo = AgentDojoAdapter()
    spikee = SPIKEEAdapter()
    assert agentdojo.estimate_model_calls({"profile": "light"}) == 100 * MAX_LLM_CALLS_PER_CASE
    assert agentdojo.estimate_model_calls({"profile": "full"}) == 629 * MAX_LLM_CALLS_PER_CASE
    assert spikee.estimate_model_calls({"profile": "light"}) == 90
    assert spikee.estimate_model_calls({"profile": "full"}) == 300


def test_agentdojo_profiles_cover_the_canonical_629_case_matrix():
    from collections import Counter
    from agentdojo.task_suite.load_suites import get_suites

    suites = get_suites("v1.2.2")
    full = canonical_security_cases(suites, 629)
    light = canonical_security_cases(suites, 100)
    medium = canonical_security_cases(suites, 300)

    assert Counter(case[0] for case in full) == {
        "workspace": 240, "travel": 140, "banking": 144, "slack": 105,
    }
    assert len(light) == 100 and {case[0] for case in light} == set(suites)
    assert len(medium) == 300 and {case[0] for case in medium} == set(suites)
    assert light == canonical_security_cases(suites, 100)


def test_agentdojo_native_security_flag_means_attack_success():
    assert normalize_agentdojo_outcome(True, True) == (True, True)
    assert normalize_agentdojo_outcome(False, False) == (False, False)


def test_agentdojo_malformed_tool_arguments_fail_the_objective_without_crashing():
    from agentdojo.functions_runtime import FunctionCall
    from agentdojo.task_suite.load_suites import get_suites

    suite = get_suites("v1.2.2")["slack"]
    task = suite.user_tasks["user_task_11"]
    environment = task.init_environment(suite.load_and_inject_default_environment({}))
    pre_environment = environment.model_copy(deep=True)
    trace = [FunctionCall(
        function="invite_user_to_slack",
        args={"user_email": {"value": "Bob"}},
    )]

    with _guard_malformed_trace_evaluators() as recoveries:
        result = suite._check_task_result(task, [], pre_environment, environment, trace)

    assert result is False
    assert recoveries == [{
        "task_id": "user_task_11",
        "error_type": "KeyError",
        "error_message": "'user'",
        "resolution": "objective_failed_for_malformed_tool_trace",
    }]


def test_spikee_ollama_target_has_a_bounded_response():
    adapter = SPIKEEAdapter()
    target, environment = adapter._target(ModelConfig(
        id="remote",
        adapter="ollama",
        model="qwen3.5:0.8b",
        base_url="http://localhost:11434",
    ))

    assert target == (
        f"provider=ollama,model=qwen3.5:0.8b,max_tokens={MAX_OUTPUT_TOKENS},temperature=0"
    )
    assert environment["RAGNAROK_OLLAMA_URL"] == "http://localhost:11434"
    assert environment["RAGNAROK_OLLAMA_KEEP_ALIVE"] == "10m"
    assert environment["RAGNAROK_REQUEST_TIMEOUT"] == "120"


def test_spikee_direct_target_maps_limits_to_ollama(monkeypatch, tmp_path: Path):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "message": {"content": "bounded response"},
                "prompt_eval_count": 10,
                "eval_count": 5,
            }

    class Session:
        def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Response()

        def close(self):
            return None

    request_log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("RAGNAROK_OLLAMA_URL", "http://remote:11434")
    monkeypatch.setenv("RAGNAROK_REQUEST_LOG", str(request_log))
    target = RAGnarokLLMTarget()
    target.session = Session()

    response = target.process_input(
        "prompt",
        target_options="provider=ollama,model=qwen,max_tokens=321,temperature=0",
    )
    target.close()

    assert response == "bounded response"
    assert captured["url"] == "http://remote:11434/api/chat"
    assert captured["json"]["think"] is False
    assert captured["json"]["options"]["num_predict"] == 321
    row = __import__("json").loads(request_log.read_text(encoding="utf-8"))
    assert row["output_tokens"] == 5
    assert row["runtime_metadata"]["max_output_tokens_enforced_as"] == "num_predict"


def test_spikee_target_signature_accepts_cli_text_content():
    from spikee.utilities.hinting import validate_content_signature

    target = RAGnarokLLMTarget()
    try:
        assert validate_content_signature("prompt", target.process_input, "input_text")
        assert validate_content_signature("system", target.process_input, "system_message")
    finally:
        target.close()


def test_agentdojo_client_enforces_max_tokens():
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return "response"

    client = type("Client", (), {
        "chat": type("Chat", (), {"completions": Completions()})(),
    })()
    bounded = _BoundedOpenAIClient(client, 1024)

    assert bounded.chat.completions.create(model="model") == "response"
    assert captured["max_tokens"] == 1024
    bounded.chat.completions.create(model="model", max_tokens=9999)
    assert captured["max_tokens"] == 1024


def test_agentdojo_ollama_client_disables_thinking():
    captured = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return "response"

    client = type("Client", (), {
        "chat": type("Chat", (), {"completions": Completions()})(),
    })()
    bounded = _BoundedOpenAIClient(client, 1024, ollama=True)

    bounded.chat.completions.create(model="model")

    assert captured["max_tokens"] == 1024
    assert captured["extra_body"]["think"] is False


def test_agentdojo_installed_pipeline_accepts_custom_local_model():
    from agentdojo.agent_pipeline.agent_pipeline import AgentPipeline, PipelineConfig
    from agentdojo.agent_pipeline.llms.openai_llm import OpenAILLM
    from agentdojo.attacks.attack_registry import load_attack
    from agentdojo.task_suite.load_suites import get_suites

    client = type("Client", (), {
        "chat": type("Chat", (), {"completions": object()})(),
    })()
    llm = OpenAILLM(_BoundedOpenAIClient(client, 1024, ollama=True), "model")
    llm.name = "local"
    pipeline = AgentPipeline.from_config(PipelineConfig(
        llm=llm,
        model_id="model",
        defense=None,
        tool_delimiter="tool",
        system_message_name=None,
        system_message=None,
    ))
    suite = next(iter(get_suites("v1.2.2").values()))

    attack = load_attack("tool_knowledge", suite, pipeline)

    assert pipeline.name == "local"
    assert attack.name == "tool_knowledge"


def test_agentdojo_calls_are_recorded_for_performance_reports(tmp_path: Path):
    class Usage:
        prompt_tokens = 12
        completion_tokens = 3

    class Message:
        def model_dump(self, **_kwargs):
            return {"role": "assistant", "content": "ok"}

    class Response:
        usage = Usage()
        choices = [type("Choice", (), {"message": Message()})()]

    class Completions:
        def create(self, **_kwargs):
            return Response()

    client = type("Client", (), {
        "chat": type("Chat", (), {"completions": Completions()})(),
    })()
    path = tmp_path / "requests.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        recorded = _RecordedOpenAIClient(
            client, handle, provider="ollama", model="model"
        )
        recorded.chat.completions.create(messages=[{"role": "user", "content": "test"}])

    row = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert row["input_tokens"] == 12
    assert row["output_tokens"] == 3
    assert row["phase"] == "subject_inference"


def test_spikee_subprocess_is_terminated_when_run_is_cancelled(tmp_path: Path, monkeypatch):
    from ragnarok.benchmarks import spikee as spikee_module

    class TemporarySPIKEEAdapter(SPIKEEAdapter):
        @property
        def project_root(self) -> Path:
            return tmp_path

    class Process:
        pid = 12345
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = -1
            return self.returncode

        def kill(self):
            self.returncode = -1

    process = Process()
    terminated = []
    monkeypatch.setattr(spikee_module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        spikee_module.subprocess,
        "run",
        lambda *args, **kwargs: terminated.append(args[0]),
    )
    monkeypatch.setattr(spikee_module.time, "sleep", lambda _seconds: None)
    if os.name != "nt":
        monkeypatch.setattr(spikee_module.os, "killpg", lambda *args: terminated.append(args))

    adapter = TemporarySPIKEEAdapter()
    adapter.workspace.mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    progress_calls = 0

    def cancel_after_process_starts(*_args, **_kwargs):
        nonlocal progress_calls
        progress_calls += 1
        if progress_calls > 1:
            raise RunInterrupted()

    with pytest.raises(RunInterrupted):
        adapter._run_model(
            ModelConfig(id="model", adapter="ollama", model="model"),
            RuntimeConfig(retries=0),
            SPIKEEOptions(profile="light"),
            dataset,
            run_dir,
            cancel_after_process_starts,
        )

    assert process.returncode == -1
    assert terminated
