from contextlib import contextmanager
from types import SimpleNamespace

import torch

import ragnarok.benchmarks.poisonedrag as poisonedrag
from ragnarok.benchmarks.poisonedrag import (
    PoisonedRAGAdapter,
    PoisonedRAGOptions,
    _attack_success_mean,
    select_retrieval_device,
)


def test_poisonedrag_official_defaults():
    adapter = PoisonedRAGAdapter()
    options = adapter.validate_options({})
    assert options == {"profile": "medium", "repeat_times": 10, "queries_per_repeat": 10}
    assert adapter.option_specs()[0].key == "profile"
    assert adapter.estimate_model_calls(options) == 150
    assert adapter.estimate_model_calls({"profile": "light"}) == 90
    assert adapter.estimate_model_calls({"profile": "full"}) == 300


def test_poisonedrag_asr_uses_the_cases_actually_evaluated():
    assert _attack_success_mean([1] * 9) == 1.0
    assert _attack_success_mean([1, 0, 1, 0]) == 0.5
    assert _attack_success_mean([]) is None


def test_poisonedrag_is_pinned():
    adapter = PoisonedRAGAdapter()
    assert adapter.upstream_dir.is_dir()
    problems = adapter.validate_installation()
    assert not any("official source must be pinned" in problem for problem in problems)
    assert not any("CUDA is required" in problem for problem in problems)


class _FakeCuda:
    def __init__(self, available: bool, name: str = "test accelerator"):
        self.available = available
        self.name = name

    def is_available(self):
        return self.available

    def get_device_name(self, index):
        assert index == 0
        return self.name


def test_retrieval_device_selects_nvidia_cuda():
    torch = SimpleNamespace(cuda=_FakeCuda(True, "NVIDIA RTX"), version=SimpleNamespace(hip=None))
    selected = select_retrieval_device(torch)
    assert (selected.backend, selected.torch_device, selected.name) == ("cuda", "cuda:0", "NVIDIA RTX")


def test_retrieval_device_selects_amd_rocm():
    torch = SimpleNamespace(cuda=_FakeCuda(True, "AMD Radeon"), version=SimpleNamespace(hip="7.0"))
    selected = select_retrieval_device(torch)
    assert (selected.backend, selected.torch_device, selected.name) == ("rocm", "cuda:0", "AMD Radeon")


def test_retrieval_device_falls_back_to_cpu():
    torch = SimpleNamespace(cuda=_FakeCuda(False), version=SimpleNamespace(hip=None))
    selected = select_retrieval_device(torch)
    assert (selected.backend, selected.torch_device, selected.name) == ("cpu", "cpu", "CPU")


def test_retrieval_is_prepared_once_for_all_official_datasets(tmp_path, monkeypatch):
    class FakeModel:
        def eval(self):
            return self

        def to(self, _device):
            return self

    class FakeTokenizer:
        def __call__(self, values, **_kwargs):
            rows = values if isinstance(values, list) else [values]
            encoded = [[float(len(value) % 11 + 1), float(len(value) % 7 + 1)] for value in rows]
            return {"input_ids": torch.tensor(encoded)}

    incorrect = {
        f"case-{index}": {
            "id": f"case-{index}",
            "question": f"question {index}",
            "correct answer": "correct",
            "incorrect answer": "incorrect",
            "adv_texts": [f"attack {position}" for position in range(5)],
        }
        for index in range(100)
    }
    rankings = {
        f"case-{index}": {f"doc-{position}": float(100 - position) for position in range(5)}
        for index in range(100)
    }
    corpus = {f"doc-{position}": {"text": f"clean context {position}"} for position in range(5)}

    class FakeUtils:
        @staticmethod
        def setup_seeds(_seed):
            return None

        @staticmethod
        def load_models(_model):
            model = FakeModel()
            return model, model, FakeTokenizer(), lambda _model, inputs: inputs["input_ids"].float()

    class FakeAttacker:
        def __init__(self, _args, **_kwargs):
            pass

        def get_attack(self, targets):
            return [[f"{target['query']}. attack {position}" for position in range(5)] for target in targets]

    @contextmanager
    def fake_official_imports(_upstream):
        prompts = SimpleNamespace(wrap_prompt=lambda question, contexts, prompt_id: f"{prompt_id}:{question}:{'|'.join(contexts)}")
        yield FakeUtils, SimpleNamespace(Attacker=FakeAttacker), prompts

    monkeypatch.setattr(poisonedrag, "_official_imports", fake_official_imports)
    monkeypatch.setattr(
        poisonedrag,
        "select_retrieval_device",
        lambda _torch: poisonedrag.RetrievalDevice("cpu", "cpu", "CPU"),
    )

    upstream = tmp_path / "benchmarks" / "poisonedrag" / "upstream"
    for dataset in poisonedrag.OFFICIAL_DATASETS:
        data_dir = upstream / "datasets" / dataset
        data_dir.mkdir(parents=True)
        with (data_dir / "corpus.jsonl").open("w", encoding="utf-8") as handle:
            for document_id, document in corpus.items():
                handle.write(__import__("json").dumps({"_id": document_id, **document}) + "\n")
        (upstream / "datasets" / f"{dataset}.zip").write_bytes(b"verified archive")
        adversarial = upstream / "results" / "adv_targeted_results" / f"{dataset}.json"
        ranking = upstream / "results" / "beir_results" / f"{dataset}-contriever.json"
        adversarial.parent.mkdir(parents=True, exist_ok=True)
        ranking.parent.mkdir(parents=True, exist_ok=True)
        adversarial.write_text(__import__("json").dumps(incorrect), encoding="utf-8")
        ranking.write_text(__import__("json").dumps(rankings), encoding="utf-8")

    monkeypatch.setattr(PoisonedRAGAdapter, "project_root", property(lambda _self: tmp_path))
    adapter = PoisonedRAGAdapter()
    monkeypatch.setattr(adapter, "validate_installation", lambda: [])
    manifest = adapter.prepare()
    cases = []
    for dataset in poisonedrag.OFFICIAL_DATASETS:
        cases.extend(__import__("json").loads((adapter.cache_dir / f"{dataset}.json").read_text(encoding="utf-8")))

    assert len(cases) == 300
    assert {case["dataset"] for case in cases} == {"nq", "hotpotqa", "msmarco"}
    assert manifest["backend"] == "cpu"
    assert {name: artifact["cases"] for name, artifact in manifest["artifacts"].items()} == {
        "nq": 100,
        "hotpotqa": 100,
        "msmarco": 100,
    }
    assert adapter.validate_prepared() == []
    assert not any((upstream / "datasets" / f"{dataset}.zip").exists() for dataset in poisonedrag.OFFICIAL_DATASETS)
