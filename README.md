<p align="center">
  <img src="assets/ragnarok-banner.png" alt="RAGnarok banner" width="900">
</p>

# RAGnarok

**One workflow. Multiple LLM security benchmarks.**

Evaluating LLM security across different benchmarks can quickly become complicated. Each benchmark comes with its own setup, execution pipeline, evaluation methods, and output formats.

Running multiple models across these benchmarks means dealing with different tools, interrupted evaluations, and results that aren't always directly comparable.

**RAGnarok was built to address this problem.**

RAGnarok is an open-source, modular Python framework that orchestrates LLM security evaluations through a unified interface.

Instead of replacing the original benchmarks, it integrates their execution workflows while preserving their native evaluation methods, metrics, and artifacts.

## What can RAGnarok do?

- **Multiple security benchmarks:** Run PoisonedRAG, SPIKEE, MPIB, and AgentDojo through a common workflow.
- **Flexible model providers:** Evaluate local or remote Ollama models, API-based models, and custom HTTP endpoints.
- **Experiment management:** Execute multiple model configurations, track progress, and resume interrupted evaluations.
- **Comparative reporting:** Collect results and generate CSV, XLSX, and PDF reports, including security and utility metrics where available.
- **Modular architecture:** Extend the framework with additional benchmarks through dedicated adapters.

## How it works

RAGnarok connects three main components:

**Benchmark adapters** preserve each benchmark's original execution and evaluation logic.

**Model provider adapters** manage communication with local models, remote inference services, and APIs.

**The runner** coordinates evaluations, executes one subject model at a time, and organizes the results for reporting.

The framework preserves native benchmark outputs alongside normalized records, allowing experiments to be analyzed without discarding their original evaluation details.

```mermaid
flowchart TB
    subgraph configuration["Configuration and preparation"]
        CLI["Interactive CLI or automation.toml"]
        CONFIG["Validated experiment configuration<br/>Benchmarks, profiles, models and roles"]
        SETUP["ragnarok setup<br/>Pinned sources and verified datasets"]
        CLI --> CONFIG
    end

    subgraph execution["Evaluation runtime"]
        RUNNER["Runner and preflight<br/>Model-first scheduling, checkpoint and resume"]
        BENCH["Benchmark adapters and native runtimes<br/>PoisonedRAG · MPIB · SPIKEE · AgentDojo"]
        PROVIDERS["Model provider adapters<br/>Ollama · OpenAI-compatible<br/>Anthropic · Custom HTTP"]
        RUNNER --> BENCH
        BENCH <-->|"Subject; Judge or attacker when required"| PROVIDERS
    end

    subgraph results["Evidence and reporting"]
        NATIVE["Native evaluation and artifacts<br/>Benchmark-owned metrics and evidence"]
        NORMALIZE["UniversalCase normalization<br/>Retains native evaluation evidence"]
        STORE[("SQLite<br/>Canonical combined results")]
        JSONL["JSONL<br/>Lossless portable export"]
        REPORTS["Derived reports<br/>CSV · JSON · XLSX · Optional comparative PDF"]
        NATIVE --> NORMALIZE --> STORE
        STORE --> JSONL
        STORE --> REPORTS
    end

    CONFIG --> RUNNER
    SETUP -.->|"Prepared assets validated before execution"| RUNNER
    BENCH --> NATIVE
```

The execution order is model-first. One model completes all selected benchmarks before the next model is loaded. Subject inference is always limited to one worker; this avoids loading or generating with multiple large models at the same time.

Solid arrows show execution, model communication, and result flow; the dotted arrow shows the setup prerequisite. Judge work may run with bounded concurrency and, for MPIB, can be deferred to a recoverable disk queue. Native evaluation remains authoritative, and reports consume stored results without calling models.

## Getting started

RAGnarok provides an interactive CLI for selecting benchmarks, configuring models, and running evaluations.

After installation and setup, you can start an evaluation with:

`ragnarok run`

See the installation and quick-start instructions below for supported environments and dependencies.

## Supported security benchmarks

| Benchmark | Security area | Evaluation sizes | Evaluation method |
| --- | --- | --- | --- |
| [PoisonedRAG](https://github.com/sleeepeer/PoisonedRAG) | Retrieved-knowledge poisoning across NQ, HotpotQA, and MS MARCO | 90 / 150 / 300 | Official attack-success evaluation |
| [MPIB](https://github.com/jhlee0619/mpib-eval) | Direct and indirect prompt injection in retrieval contexts | 120 / 300 / complete test split | Official structured LLM Judge prompt |
| [SPIKEE](https://github.com/ReversecLabs/spikee) | Prompt leakage, exfiltration, XSS-style output, and resource abuse | 90 / 250 / 300 | Native dataset evaluation |
| [AgentDojo](https://github.com/ethz-spylab/agentdojo) | Agentic indirect injection, unauthorized actions, and tool misuse | 100 / 300 / 629 | Official utility and security checks |

Reduced profiles are deterministic subsets. Full profiles preserve the complete declared RAGnarok evaluation matrix for each integration. The selected profile, upstream revision, model configuration, Judge configuration, and relevant hashes are frozen in the run manifest.

## Installation

Python 3.11 or newer is required. Python 3.12 is recommended for compatibility with the pinned third-party dependencies.

### Windows

```powershell
git clone --recurse-submodules https://github.com/mark-on/RAGnarok.git
cd RAGnarok
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
ragnarok setup
```

### Linux

```bash
git clone --recurse-submodules https://github.com/mark-on/RAGnarok.git
cd RAGnarok
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
ragnarok setup
```

`ragnarok setup` installs registered optional dependencies and prepares benchmark assets in parallel. Prepared caches are verified and reused on later setup runs. Dataset downloads and retrieval preparation happen during setup, not during normal evaluation.

Benchmark datasets, prepared caches, model weights, generated outputs, and credentials are intentionally not included in this repository. PoisonedRAG setup downloads the official NQ, HotpotQA, and MS MARCO archives (about 2.2 GB compressed in total, with additional space required while preparing them). MPIB setup downloads the immutable v1.1 dataset revision used by the framework.

MPIB is access-gated by its authors. Setup may request a Hugging Face token after the user accepts the dataset terms. Credentials are stored through the operating-system credential store and are not written to result manifests. Make sure the machine has enough free disk space and network access before starting setup.

For containerized installations, see the included [Dockerfile](Dockerfile). Remote Ollama configuration is described in the Model providers section below.

## Quick start

Check benchmark readiness:

```powershell
ragnarok benchmarks
```

Start an interactive evaluation:

```powershell
ragnarok run
```

The wizard asks for:

1. One or more benchmarks.
2. A shared Light, Medium, or Full evaluation size.
3. One or more subject models.
4. A Judge model when the selected benchmark requires one.
5. A final confirmation before inference begins.

Use plain output for servers, redirected logs, or terminals without full interactive rendering:

```powershell
ragnarok run --plain
```

When an incomplete suite is detected, RAGnarok offers to resume the frozen session. Completed model/benchmark jobs are skipped.

## Model providers

RAGnarok supports:

- Ollama, running locally or through an SSH tunnel to a remote GPU;
- OpenAI-compatible APIs, including OpenAI, DeepSeek, and OpenRouter;
- Anthropic;
- explicitly configured custom HTTP endpoints.

For a remote Ollama server forwarded through SSH, configure RAGnarok as normal Ollama at:

```text
http://127.0.0.1:11434
```

The framework sees a local endpoint while inference is performed by the remote GPU. Datasets, benchmark code, results, and reports can remain on the local computer; only model inputs and outputs cross the tunnel.

Headless credentials can be supplied through variables named `RAGNAROK_CREDENTIAL_<ID>`. For example, credential ID `deepseek` resolves from `RAGNAROK_CREDENTIAL_DEEPSEEK`.

## Results

Every suite is isolated under `outputs/`:

```text
outputs/<model-or-group>_<UTC-run-id>/
  suite_manifest.json
  results.sqlite
  report.xlsx
  report.json
  cases.csv
  summary.csv
  metrics.json
  data/
    cases.jsonl
    model_calls.jsonl
    metrics.jsonl
  artifacts/
    benchmarks/
      <benchmark>/<native-run>/...
```

SQLite is the canonical combined store. JSONL is the lossless portable export. CSV, XLSX, and PDF files are derived views. Native benchmark files remain available for auditing individual cases.

The reports include, where supported by the selected datasets:

- Attack Success Rate and resistance;
- legitimate-task utility;
- Security-Utility Balance Score;
- attack objectives and techniques;
- direct, indirect RAG, and agentic security tracks;
- retrieval-security analysis;
- subject and Judge performance;
- tokens per second and execution duration;
- model and quantization comparisons;
- source-run provenance and known coverage gaps.

## Comparative PDF reports

Generate a PDF from stored results without repeating inference or Judge calls:

```powershell
ragnarok report
```

Select one run for a single-model report or multiple runs for a quantization comparison. For non-interactive use:

```powershell
ragnarok report --run RUN_Q8 --run RUN_Q4 --output outputs/reports/qwen-comparison
```

The report bundle contains `report.pdf`, `combined_results.csv`, and `report_manifest.json`.

## Automated execution

`ragnarok auto` reads a TOML plan and processes a model queue while keeping subject concurrency at one.

```powershell
ragnarok preflight --file automation.toml
ragnarok auto --file automation.toml --dry-run
ragnarok auto --file automation.toml
```

The repository template keeps example models disabled until exact Ollama tags are verified. Automation can prefetch future models, reserve disk space, remove only models it downloaded itself, checkpoint completed jobs, and optionally synchronize results to durable storage.

## Adding a new dataset with AI

A developer can give an AI coding agent the new dataset, paper, or official repository and ask it to implement the adapter using the existing framework conventions. Start with `src/ragnarok/core/benchmark.py`, the registered adapters, and their offline tests.

An effective request is:

```text
Read the BenchmarkAdapter contract and existing adapters and tests.
Analyze the official <DATASET OR BENCHMARK> repository,
paper, license, prompts, splits, and evaluator. Propose an integration plan that
preserves the native protocol. Then implement a pinned BenchmarkAdapter, setup
validation, UniversalCase normalization, native artifacts, resume behavior, and
offline tests. Do not replace the official evaluator or silently modify payloads.
Run the focused tests and the complete test suite before handing off the change.
```

### What the AI can automate

An AI coding agent can:

- inspect the upstream dataset schema and evaluator;
- map setup and runtime dependencies;
- create a new `BenchmarkAdapter`;
- add deterministic Light, Medium, and Full profiles;
- connect subject, Judge, or attacker roles to existing providers;
- convert native cases into `UniversalCase` records;
- preserve native metrics and artifacts;
- add resume and failure handling;
- build unit and integration tests;
- extend taxonomy and comparative reports;
- update documentation and dependency metadata.

### What still requires human validation

AI assistance does not make an integration automatically equivalent to the original benchmark. A researcher or maintainer must verify:

- dataset and model licenses;
- access restrictions and redistribution terms;
- the exact upstream revision;
- prompts, splits, payloads, decoding parameters, and scoring rules;
- whether a Judge substitution changes numerical comparability;
- whether hardware or provider adapters alter retrieval or generation;
- the scientific wording used to describe fidelity and limitations.

The adapter should be added to `src/ragnarok/benchmarks/registry.py` only after these checks and the complete test suite pass.

## Manual adapter checklist

To integrate a benchmark without an AI agent:

1. Implement `BenchmarkAdapter` from `src/ragnarok/core/benchmark.py`.
2. Pin the official source release or commit.
3. Define and validate evaluation profiles.
4. Add the dependency extra to `pyproject.toml`.
5. Prepare all runtime assets during `ragnarok setup`.
6. Write native outputs and `normalized/cases.jsonl`.
7. Preserve official metrics in `native/metrics.json` and `official_evaluation`.
8. Add the adapter to the registry.
9. Add deterministic tests for prompts, subsets, evaluation, errors, and resume.
10. Run an authorized small real-model pilot before a full evaluation.

## Development and testing

Run the complete offline suite:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Portable form:

```bash
python -m pytest -q
```

The tests do not require paid APIs, live Ollama, or model downloads. They cover setup, providers, benchmark fidelity, Judge queues, interruption, resume, outputs, ETA, UI, reports, and PDF generation.

Before submitting a change:

```powershell
git diff --check
ragnarok --help
python -m pytest -q
```

Use a paid API or real GPU only for an explicitly authorized end-to-end pilot.

## Reproducibility and safety

- Registered benchmark sources and releases are pinned.
- Missing or stale assets fail closed with a setup instruction.
- Subject, Judge, and attacker calls remain separately identifiable.
- API keys and restricted MPIB payload registries are excluded from Git and reports.
- The framework does not silently repair or replace upstream behavior.
- Reports never call a model or recompute native evaluation.
- A lower ASR is not interpreted as better security when utility collapses.
- Single-run differences are reported descriptively and are not presented as causal proof.

## License

RAGnarok's original code and documentation are released under the [MIT License](LICENSE). You may use, copy, modify, merge, publish, distribute, sublicense, and sell the software, provided that the copyright and permission notice are preserved.

Third-party benchmarks, datasets, model weights, packages, and Git submodules are not relicensed by this project. They retain their own licenses, access requirements, usage restrictions, and attribution rules.
