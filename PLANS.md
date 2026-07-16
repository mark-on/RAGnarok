# RAGnarok evaluation framework implementation plan

1. Add installable `ragnarok` packaging, validated YAML configuration, lifecycle state, dependency checks, and the requested Typer CLI surface.
2. Build a strictly local PDF extraction and RAG layer with separate body/metadata channels, evaluator-artifact exclusions, deterministic chunking, pluggable embeddings, persistent caching, NumPy ranking, and retrieval evidence.
3. Add one asynchronous provider contract with mock, Ollama, OpenAI-compatible, and configurable custom-HTTP adapters, including bounded retries, timeouts, health checks, and secret-safe errors.
4. Execute models sequentially by conversation, preserve only within-conversation history, checkpoint atomically, resume without overwriting completed work, and isolate every model's output tree.
5. Evaluate responses with deterministic rules plus an optional structured LLM judge, resolve final labels conservatively, and calculate security, retrieval, utility, and operational metrics.
6. Generate per-model Markdown/PDF reports and a cross-model comparison, then verify the implementation with unit tests and a fully offline two-model mock experiment.

The existing 17-column CSV, PDFs, taxonomy, dataset card, generator, validator, tests, and evaluator-only attack manifest remain unchanged.

## Simplified user workflow

The public CLI consists of `setup`, `validate`, `run`, and `status`. `run` automatically performs preflight validation, provider checks, index creation/loading, inference, evaluation, reporting, and comparison. `run --mock --quick` is the built-in offline smoke test; low-level diagnostic commands remain available but hidden from normal help.
