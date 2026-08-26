from pathlib import Path

import pytest

from ragnarok.benchmarks import available_benchmarks, benchmark_for
from ragnarok.config import config_from_data


def test_registry_contains_supported_benchmarks():
    adapters = available_benchmarks()
    assert [adapter.info.id for adapter in adapters] == [
        "poisonedrag", "mpib", "agentdojo", "spikee",
    ]
    assert benchmark_for("poisonedrag") is adapters[0]
    with pytest.raises(ValueError, match="unknown benchmark"):
        benchmark_for("open_prompt_injection")


def test_unknown_benchmark_fails_closed():
    with pytest.raises(ValueError, match="unknown benchmark"):
        benchmark_for("not_real")


def test_application_configuration_has_no_shared_rag_fields(tmp_path: Path):
    config = config_from_data(
        {
            "benchmarks": [{
                "id": "poisonedrag",
            }],
            "models": [{"id": "test", "adapter": "ollama", "model": "test-model"}],
        },
        tmp_path,
    )
    assert config.output_dir == (tmp_path / "outputs").resolve()
    assert not hasattr(config, "rag")
    assert not hasattr(config, "dataset")
    assert not hasattr(config, "system_prompt_path")


def test_legacy_single_benchmark_config_is_upgraded(tmp_path: Path):
    config = config_from_data({
        "benchmark": {"id": "poisonedrag"},
        "models": [{"id": "m", "adapter": "ollama", "model": "m"}],
    }, tmp_path)
    assert [item.id for item in config.benchmarks] == ["poisonedrag"]
