from __future__ import annotations

from ..core import BenchmarkAdapter
from .poisonedrag import PoisonedRAGAdapter
from .mpib import MPIBAdapter
from .agentdojo import AgentDojoAdapter
from .spikee import SPIKEEAdapter


_BENCHMARKS: dict[str, BenchmarkAdapter] = {
    "poisonedrag": PoisonedRAGAdapter(),
    "mpib": MPIBAdapter(),
    "agentdojo": AgentDojoAdapter(),
    "spikee": SPIKEEAdapter(),
}


def available_benchmarks() -> tuple[BenchmarkAdapter, ...]:
    return tuple(_BENCHMARKS.values())


def benchmark_for(benchmark_id: str) -> BenchmarkAdapter:
    try:
        return _BENCHMARKS[benchmark_id]
    except KeyError as exc:
        available = ", ".join(sorted(_BENCHMARKS))
        raise ValueError(f"unknown benchmark {benchmark_id!r}; available: {available}") from exc
