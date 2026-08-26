# RAGnarok Engineering Guide for Coding Agents

This document is the operational source of truth for an LLM or coding agent modifying RAGnarok. Read it before changing code. Verify claims against the implementation and tests when behavior may have changed.

## Project purpose

RAGnarok is a provider-independent runner for pinned third-party LLM and RAG security benchmarks. It compares subject models and quantizations while preserving each benchmark's prompts, attack construction, native evaluation, and provenance.

The framework is responsible for:

- interactive and automated configuration;
- provider transport for subject, Judge, and attacker models;
- benchmark preparation and validation;
- serial subject inference with bounded parallel auxiliary work;
- interruption, checkpoint, resume, and deferred Judge queues;
- canonical normalization without replacing native evaluation;
- SQLite, JSONL, CSV, XLSX, and comparative PDF reporting.

The framework must not silently alter benchmark payloads, scoring rules, case order, decoding limits, or pinned revisions.

## Supported suite

Only adapters in `src/ragnarok/benchmarks/registry.py` are publicly registered:

| ID | Benchmark | Profiles | External Judge |
| --- | --- | --- | --- |
| `poisonedrag` | PoisonedRAG | 90 / 150 / 300 | No |
| `mpib` | MPIB | 120 / 300 / complete official test split | Yes |
| `spikee` | SPIKEE | 90 / 250 / 300 | Native evaluation |
| `agentdojo` | AgentDojo | 100 / 300 / 629 | Native security and utility checks |

`src/ragnarok/benchmarks/open_prompt_injection.py` and its tests remain in the tree, but the adapter is not registered. Do not describe it as supported or add it to the suite without an explicit product decision and a complete fidelity review.

## High-level execution flow

```text
CLI
  -> interactive wizard or automation.toml
  -> Pydantic configuration validation
  -> provider and benchmark preflight
  -> runner (one subject model at a time)
  -> benchmark adapter
  -> provider transport / native benchmark runtime
  -> native artifacts and native evaluation
  -> UniversalCase normalization
  -> ResultStore (SQLite is canonical)
  -> JSONL/CSV/XLSX reports
  -> optional comparative PDF generated from stored results
```

The loop in `run_experiment()` is model-first: one model completes all selected benchmarks before the next model is loaded. This avoids repeatedly loading the same quantization.

## Repository map

### Entry points and orchestration

- `src/ragnarok/cli.py`: Typer commands, resume prompt, terminal outcome, setup, run, automation, report, and preflight.
- `src/ragnarok/wizard.py`: interactive benchmark, model, provider, Judge, and credential selection.
- `src/ragnarok/config.py`: `ModelConfig`, `RuntimeConfig`, `BenchmarkSelection`, and `AppConfig` validation.
- `src/ragnarok/runner.py`: preflight, model-first scheduling, benchmark execution, normalization, suite state, ETA, resume, and final report generation.
- `src/ragnarok/automation.py`: TOML automation, model prefetch, disk reservation, cleanup ownership, and persistent jobs.
- `src/ragnarok/cloud.py`: cloud/headless preflight checks.
- `src/ragnarok/bootstrap.py`: dependency installation and concurrent benchmark preparation.
- `src/ragnarok/interrupts.py`: confirmed interruption behavior.
- `src/ragnarok/ui.py`: Rich alternate-screen UI and plain/server output.

### Benchmark layer

- `src/ragnarok/core/benchmark.py`: abstract `BenchmarkAdapter` contract and benchmark metadata.
- `src/ragnarok/benchmarks/registry.py`: the only authoritative supported-benchmark registry.
- `src/ragnarok/benchmarks/_runtime.py`: pinned-repository and shared adapter runtime utilities.
- `src/ragnarok/benchmarks/_judge_queue.py`: bounded RAM queue, disk spill, leasing, adaptive concurrency, heartbeat, and recovery.
- `src/ragnarok/benchmarks/poisonedrag.py`: official retrieval preparation and replay for NQ, HotpotQA, and MS MARCO.
- `src/ragnarok/benchmarks/mpib.py`: official prompt assembly, reconstruction qualification, subject reuse, Judge queue, and MPIB metrics.
- `src/ragnarok/benchmarks/spikee.py`: frozen official-seed profile and subprocess supervision.
- `src/ragnarok/benchmarks/spikee_target.py`: bounded provider transport used by the SPIKEE workspace.
- `src/ragnarok/benchmarks/agentdojo.py`: official tool environments, attacks, trajectories, utility checks, and security checks.

### Providers

- `src/ragnarok/models/base.py`: provider interface, retry policy, redaction, and provider factory.
- `src/ragnarok/models/ollama.py`: local or SSH-tunneled Ollama, warm-up, keep-alive, runtime metadata, and model lifecycle.
- `src/ragnarok/models/openai_compatible.py`: OpenAI, DeepSeek, OpenRouter, and compatible chat-completions APIs.
- `src/ragnarok/models/anthropic.py`: Anthropic transport.
- `src/ragnarok/models/custom_http.py`: explicitly mapped generic HTTP endpoints.
- `src/ragnarok/credentials.py`: OS keyring and `RAGNAROK_CREDENTIAL_<ID>` environment lookup.

Provider adapter identifiers accepted by `ModelConfig` are `ollama`, `openai`, `anthropic`, and `custom_http`. OpenRouter is configured through the `openai` adapter and its OpenAI-compatible base URL. Reasoning is disabled by default for OpenRouter unless explicitly overridden.

### Results and reports

- `src/ragnarok/results/schemas.py`: canonical `UniversalCase` schema.
- `src/ragnarok/results/store.py`: canonical SQLite store, persistent job states, and lossless JSONL exports.
- `src/ragnarok/reports.py`: CSV, JSON, XLSX, metrics, taxonomy, and per-model/group reports.
- `src/ragnarok/pdf_report.py`: single or comparative PDF generated only from stored canonical results.
- `src/ragnarok/taxonomy.py`: benchmark coverage and known-gap mapping.

Native artifacts remain authoritative. Normalized records exist to compare benchmarks; they must retain native evidence in `official_evaluation` and must not invent a replacement score.

## Core invariants

Preserve these unless the user explicitly changes the research protocol:

1. `subject_concurrency` is schema-locked to `1`.
2. A second subject inference must never run in parallel with the first.
3. Judge, downloads, setup preparation, and report work may be parallelized within configured bounds.
4. `ragnarok setup` downloads and prepares assets; `ragnarok run` fails closed when assets are missing or stale.
5. Every registered benchmark pins its upstream release or commit and validates it.
6. Model/provider adapters may replace transport, not benchmark semantics.
7. Subject, Judge, and attacker calls are logged and accounted for separately.
8. Secrets never enter manifests, reports, exception text, or committed files.
9. SQLite is the canonical combined store. JSONL is the portable lossless export. CSV/XLSX/PDF are derived views.
10. Resume reuses the frozen configuration and completed work. It must not repeat successful subject inference unnecessarily.
11. A benchmark failure may make the suite `partial`; fatal queue-storage failures stop execution to prevent lost evaluations.
12. MPIB Judge work may be deferred to disk, but queued cases must remain idempotent and recoverable.
13. The terminal must end with an explicit `Completed` or `Error: ...` outcome.
14. Generated outputs, caches, credentials, model weights, and benchmark workspaces stay ignored by Git.

## Adapter output contract

Each adapter's `run()` returns one or more native run directories. Every run directory must contain `run_manifest.json`. Each evaluated model directory must use this structure:

```text
<run-dir>/<model-id>/
  requests.jsonl                 # subject calls, when applicable
  judge_requests.jsonl           # Judge calls, when applicable
  attacker_requests.jsonl        # attacker calls, when applicable
  native/metrics.json            # official/native metrics
  normalized/cases.jsonl         # UniversalCase-compatible records
```

`runner._load_adapter_results()` reads this exact contract. A new adapter that writes a different structure will appear to complete but will not populate canonical results.

At minimum, every normalized case needs:

- suite, benchmark, model, and case identifiers;
- attack family;
- exact prompt and response;
- relevant target, payload, contexts, and references;
- benchmark-owned results inside `official_evaluation`;
- an explicit error when inference or evaluation failed.

## Commands

Install for development:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

Linux equivalent:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

Main CLI commands:

```text
ragnarok setup [--workers N] [--plain]
ragnarok benchmarks
ragnarok run [--plain]
ragnarok report [--run NAME ...] [--output PATH]
ragnarok preflight --file automation.toml
ragnarok auto --file automation.toml [--dry-run] [--plain]
```

`automation.toml` intentionally ships with every example model disabled. Enable and verify exact registry tags before using `preflight` or `auto`.

For a remote Ollama server exposed through SSH, the framework normally still uses `http://127.0.0.1:11434`; the SSH client forwards that local address to the remote Ollama service. Treat this as an Ollama provider, not a custom HTTP provider.

## Test strategy

The normal test suite is offline and uses fixtures, fakes, monkeypatching, and temporary directories. It must not require API keys, model downloads, a live Ollama process, or paid inference.

Run the complete suite before handing off a non-trivial change:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Portable form:

```bash
python -m pytest -q
```

At the time this guide was written, the expected baseline was 124 passing tests. Treat the current collected test count as authoritative when tests are deliberately added or removed.

Use focused tests while iterating:

| Change area | Minimum focused tests |
| --- | --- |
| CLI/setup | `tests/test_bootstrap.py tests/test_framework.py` |
| Wizard/providers | `tests/test_wizard.py tests/test_provider_optimization.py` |
| Runner/results/reports | `tests/test_results_and_reports.py tests/test_resume.py` |
| ETA/UI/interrupts | `tests/test_suite_eta.py tests/test_ui.py tests/test_interrupts.py` |
| MPIB/Judge resilience | `tests/test_new_benchmarks.py tests/test_judge_queue.py` |
| PoisonedRAG | `tests/test_poisonedrag.py` |
| SPIKEE/AgentDojo/automation | `tests/test_automation.py` |
| PDF | `tests/test_pdf_report.py` plus rendered-page visual inspection |

Also run:

```powershell
git diff --check
ragnarok --help
```

### Testing a real installation

Do not run paid or long-lived integration tests unless the user explicitly authorizes them.

For an authorized end-to-end pilot:

1. Run `ragnarok setup` and require a successful setup manifest.
2. Run `ragnarok benchmarks` and require every selected adapter to report `ready`.
3. Verify the provider separately, for example `GET /api/tags` for Ollama.
4. Select one small real model, one benchmark, and `Light`.
5. Use `ragnarok run --plain` on headless/logging systems.
6. Confirm an explicit terminal completion state.
7. Inspect `suite_manifest.json`; status must be `complete` or an understood `partial`.
8. Compare expected and stored case counts.
9. Inspect subject and Judge call logs separately.
10. Open the XLSX or PDF and verify that metrics match stored native results.

Never use the existence of a report alone as proof that all benchmarks completed.

## How to add or modify a benchmark

1. Read the upstream paper, repository, license, dataset terms, and official evaluator.
2. Pin an immutable release or commit.
3. Implement `BenchmarkAdapter` completely: metadata, options, validation, call estimates, preparation, prepared-state validation, and async execution.
4. Add the optional dependency group in `pyproject.toml`; set `BenchmarkInfo.python_extra` to the same key.
5. Register the adapter only after it is complete.
6. Keep upstream code and prepared data outside `src/`; use ignored workspace/cache directories.
7. Write native artifacts and normalized cases using the adapter output contract.
8. Record every protocol deviation or hardware adapter in the native and suite manifests.
9. Add deterministic tests for profile counts, pin validation, prompt construction, native evaluation, normalization, errors, resume, and reporting.
10. Run the full suite and an explicitly authorized small real-model pilot.

When adapting hardware or transport, demonstrate that prompts, retrieved documents, attack payloads, scoring, and case selection remain unchanged.

## How to add or modify a provider

1. Extend `ModelProvider` and implement `generate()` plus a meaningful `check()`.
2. Reuse one persistent `httpx` client.
3. Return `ProviderResult`; do not throw raw provider payloads into reports.
4. Redact credentials and bound error text.
5. Distinguish safe retryable failures from timeouts that may conceal a still-running inference.
6. Respect benchmark-supplied temperature, output-token limits, stop sequences, and response schemas.
7. Keep the subject concurrency at one.
8. Record token counts, wall duration, provider runtime metadata, and model identity when available.
9. Register the provider in `provider_for()` and update the `ModelConfig.adapter` literal.
10. Add tests for success, malformed responses, authentication, transient errors, non-retryable errors, timeout behavior, and cleanup.

## Resume and queue safety

Treat resume and Judge queues as data-integrity features, not UI conveniences.

- A completed job is skipped on resume.
- A Judge-only MPIB resume must not require or repeat the subject model.
- Partial JSONL tails must not invalidate earlier complete records.
- Queue insertion and completion are idempotent.
- RAM buffering is bounded; overflow is written to disk.
- Leased queue items return after lease expiry.
- Provider concurrency decreases after failures and recovers gradually after successful responses.
- Persistent provider failure must leave recoverable queue state and a clear manifest error.

Any change in these areas requires both `tests/test_resume.py` and `tests/test_judge_queue.py`.

## Reporting rules

- Reports consume stored results only; they never call a model or Judge.
- ASR denominators exclude cases whose native evaluator marks them invalid or unevaluated.
- Utility must remain separate from ASR.
- SUBS is the equal-weight harmonic mean of `SecurityScore = 100 * (1 - ASR)` and `UtilityScore = 100 * Utility`.
- Quantization comparisons must identify the baseline and preserve model labels.
- Automatically generated interpretation must state only facts derivable from stored data.
- Do not claim causality from a single run or from unpaired aggregate differences.
- Keep benchmark-specific pages and source-run provenance.
- For PDF changes, render every affected page and visually check wrapping, clipping, colors, legends, tables, headers, and footers.

## Security and privacy

- Never commit `.env`, `.ragnarok/`, `outputs/`, benchmark workspaces, model weights, API keys, Hugging Face tokens, or `benchmarks/mpib/payload_registry.json`.
- Resolve credentials through the OS keyring or `RAGNAROK_CREDENTIAL_<ID>`.
- Do not print credentials in debug logs or HTTP exceptions.
- Prefer SSH local port forwarding for remote Ollama. Do not expose port 11434 publicly without authentication and network restrictions.
- Treat benchmark payloads and model outputs as untrusted data.
- Do not weaken fail-closed pin, hash, dataset, or prepared-cache checks to make a run pass.

## Change checklist

Before completing a change:

- Identify the affected invariants and native benchmark contract.
- Add or update focused tests first or alongside the change.
- Preserve unrelated user changes.
- Confirm no generated or secret files are staged.
- Run focused tests.
- Run the full test suite for non-trivial changes.
- Run `git diff --check`.
- If reporting changed, inspect the rendered artifact visually.
- If execution changed, verify manifest status, job state, case count, and role-specific call logs.
- Summarize behavioral changes, tests run, and any remaining qualification.

