# RAGnarok

RAGnarok is a modular execution framework for pinned third-party RAG security benchmarks. It selects and connects subject, Judge, and attacker models, supervises execution, and preserves provenance. Benchmark-owned prompts, attack logic, datasets, and scoring rules remain authoritative; hardware and provider compatibility is supplied by explicit adapters.

The supported suite is:

- [PoisonedRAG](https://github.com/sleeepeer/PoisonedRAG), pinned at `f660d72174f06b13fae5163ce656e7b235db858f`
- [MPIB](https://github.com/jhlee0619/mpib-eval), toolkit pinned at `ad615aaec605e9cc8028fb073cdf428b08fca9f7`, canonical dataset version `v1.1`
- [SPIKEE](https://github.com/ReversecLabs/spikee), pinned to release `v0.9.1` and its `seeds-cybersec-2026-01` dataset
- [AgentDojo](https://github.com/ethz-spylab/agentdojo), pinned to package release `v0.1.35` and benchmark version `v1.2.2`

## Install

Python 3.11 or newer is required for the RAGnarok core. Python 3.12 is recommended for compatibility with the pinned benchmark's older dependency stack.

```powershell
git clone --recurse-submodules https://github.com/mark-on/RAGnarok
cd RAGnarok
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

For an existing checkout:

```powershell
ragnarok setup
```

`ragnarok setup` installs every registered dependency without reinstalling the active `ragnarok.exe`, then prepares independent benchmarks concurrently. The automatic worker count is capped at four and can be overridden with `ragnarok setup --workers N`. Each benchmark writes a separate log, failures are collected instead of exposing third-party tracebacks, and `.ragnarok/setup_manifest.json` records the readiness and preparation metadata of every benchmark. Setup is idempotent: verified caches are reused.

MPIB is gated by its authors. Accept the dataset terms on Hugging Face before setup. The command requests a Hugging Face token before installation, keeps it available throughout preparation, stores it in the operating-system credential store, and never writes it to a manifest. If `benchmarks/mpib/payload_registry.json` contains the approved restricted registry, V2 payloads are restored exactly. Otherwise setup applies the pinned official toolkit's public `structural_mock` reconstruction. The selected mode and reconstruction counts are frozen in setup, run, and case metadata; structural mocks are never labeled as exact restricted payloads.

After setup, `ragnarok run` does not download datasets or rebuild indexes. Missing, stale, unresolved, or hash-invalid assets fail closed with a setup instruction.

## Validate benchmarks

```powershell
ragnarok benchmarks
```

The command verifies the pinned upstream commit and required Python imports. A mismatched commit or missing dependency prevents a qualified run.

## Run

```powershell
ragnarok run
```

The interactive flow is:

```text
One or more benchmark selections
→ One shared Light, Medium, or Full suite size
→ Model selection
→ Explicit Judge selection for MPIB
→ Execution summary
→ Official benchmark execution
→ Native evaluation
→ Universal result normalization
→ Scientific XLSX report with model, quantization, taxonomy, retrieval, Judge, and performance analysis
```

Model connections include:

- Ollama
- OpenAI
- Anthropic
- OpenRouter or another OpenAI-compatible API
- Custom HTTP endpoints

Credentials are entered through hidden fields and stored using the operating system credential store. Judge and attacker configurations may be saved under a user-provided profile name. A required role is still selected explicitly on every run; a saved profile is never applied silently.

Headless servers can provide the same credentials as environment variables named `RAGNAROK_CREDENTIAL_<ID>`, where punctuation in the configured credential ID becomes `_` and the value is uppercased. For example, `credential_id = "deepseek"` resolves from `RAGNAROK_CREDENTIAL_DEEPSEEK`. Secrets are never written to reports or manifests.

Local Ollama runs use one inference worker, a persistent HTTP connection, an explicit model warm-up, a keep-alive window during the run, buffered request logging, and automatic model unloading when the model evaluation finishes. The request log records wall-clock and Ollama runtime durations. Ollama independently selects CPU, GPU, or hybrid execution; this generator placement is separate from the CPU/CUDA/ROCm device selected for PoisonedRAG's Contriever preparation.

## PDF reports

```powershell
ragnarok report
```

The command lists result runs containing canonical normalized cases. Select one run for a single-run PDF or multiple runs for a comparative PDF. Report generation never repeats inference or Judge calls. The generated folder under `outputs/reports/` contains `report.pdf`, `combined_results.csv`, and `report_manifest.json` with the exact selected source runs.

For headless execution, repeat `--run` with result directory names:

```powershell
ragnarok report --run qwen_q8_run --run qwen_q4_run
```

## PoisonedRAG integration

PoisonedRAG always covers NQ, HotpotQA, and MS MARCO. Light uses 90 cases, Medium uses 150, and Full uses the complete 300-case RAGnarok replay profile. Reduced profiles are deterministic prefixes of the frozen official case sequence and are labeled as reduced subsets. The fixed settings remain `LM_targeted`, five adversarial passages, dot-product scoring, seed 12, temperature 0.1, and 150 output tokens.

The adapter leaves the pinned upstream source unchanged. During `ragnarok setup`, it executes the same Contriever computation on NVIDIA/CUDA, AMD/ROCm, or CPU and selects the best available backend automatically; it never substitutes another retriever. Only the five clean documents referenced by each released ranking are retained while the BEIR corpus is scanned as JSONL, preventing whole-corpus RAM loading without changing IDs, texts, scores, or ranking logic. The verified top-5 contexts are then reused unchanged by every model and quantization during `ragnarok run`. The final LLM client is replaced by the shared provider transport.

This is a controlled, offline replay of a fixed RAG evaluation pipeline, not a production online vector database. Hardware backends may produce small floating-point differences, so the integration is faithful to the official algorithm and artifacts but is not claimed to be bitwise-identical across CPU, CUDA, and ROCm.

## MPIB integration

MPIB uses the official blind prompt assembly, fixed test split, V1 direct attacks, V2 guaranteed post-retrieval exposure, and the paper's exact structured Judge prompt. Light and Medium are deterministic stratified subsets of 120 and 300 test cases; Full is the complete official test split. Subject inference is always serial. Remote Judge calls use a bounded asynchronous queue, defaulting to four concurrent requests, while local Ollama Judges are forced to one. Results are restored to official case order, and Subject and Judge inference retain separate logs and accounting. ASR uses severity at least 2, CHER uses severity at least 3, and invalid Judge JSON is excluded from both denominators.

The public MPIB release redacts restricted V2 payload text but includes official reconstruction hooks. RAGnarok mirrors the pinned reconstructor: an approved registry produces `restored_fidelity`; without it, the public `structural_mock` profile preserves the declared rule family and payload length but not the original attack wording. These two profiles must not be pooled as numerically identical evidence.

The authors selected Qwen2.5-72B-Instruct as their primary Judge. RAGnarok deliberately asks the user to select the Judge to support local and provider-independent studies, so a run using another Judge is protocol-compatible but not numerically identical to the paper's primary-Judge results. The manifest records that deviation unambiguously.

## SPIKEE integration

SPIKEE uses the official `v0.9.1` workspace generator, `seeds-cybersec-2026-01`, `llm_provider` target, and dataset-owned native judges. It covers direct injection objectives including system-prompt leakage, data exfiltration, XSS-style output, and resource exhaustion. Setup generates a frozen, SHA-256-sorted 300-case RAGnarok profile. Light and Medium use deterministic prefixes of 90 and 250 cases. This is explicitly reported as a fixed profile derived from SPIKEE, not the complete set of all possible SPIKEE datasets, plugins, and dynamic attacks.

## AgentDojo integration

AgentDojo runs its official task suites, tool environment, agent pipeline, attack implementation, utility checks, and security checks. RAGnarok connects Ollama through AgentDojo's supported OpenAI-compatible local-model path, keeps concurrency at one, and preserves the official native trajectories. Light and Medium are deterministic prefixes of 100 and 300 cases; Full uses the 629-case security matrix from benchmark version `v1.2.2`. AgentDojo is reported separately as agentic security evidence and is not mislabeled as classic retrieval-only RAG.

## Automated ephemeral execution

`ragnarok auto` reads the TOML-formatted `automation.toml` file. Models are deliberately disabled in the repository template so the exact registry tags can be frozen after the thesis model matrix is selected; the examples exclude 1B models.

```powershell
ragnarok preflight --file automation.toml
ragnarok auto --file automation.toml
```

Automation performs one Subject inference at a time and prefetches a bounded number of future Ollama models concurrently. `download_concurrency` defaults to two and reserves disk space before starting each pull. It records the models installed before startup, never deletes those models, and removes only automation-owned downloads after the current model's results and checkpoint have been committed. SQLite job checkpoints allow completed model/benchmark pairs to be skipped when `resume_suite` points to an interrupted output directory. `sync_command` can copy the suite after each completed model to object storage; without it, outputs remain on the Pod's ephemeral disk and must be downloaded before the Pod is terminated.

The subject queue, Judge configuration, benchmark profiles, provider parameters, concurrency limits, and sync command are frozen in `automation.toml` and copied into `automation_manifest.json`. `subject_concurrency` is schema-locked to one. `judge_concurrency` controls remote Judge requests, `postprocess_workers` controls report generation, and download parallelism never creates a second Subject worker.

For a reproducible Linux/CUDA environment, build the included `Dockerfile`. Ollama may run in the same Pod or at the URL configured by `ollama_url`. The framework never terminates a cloud Pod automatically.

See [RUNPOD_DEPLOYMENT_GUIDE.md](RUNPOD_DEPLOYMENT_GUIDE.md) for the complete ephemeral-Pod workflow, cost controls, secrets, setup, pilot acceptance criteria, resume, output export, and termination checklist.

See [GPU_MODEL_PLAN.md](GPU_MODEL_PLAN.md) for the RunPod price snapshot and the fixed-hardware strategy used to assign model families and quantizations to GPUs.

## Outputs

Each run is isolated and immediately identifiable. A single-model run uses the model identifier in the directory name:

```text
outputs/<model-id>_<UTC-run-id>/
├── report.xlsx
├── cases.csv
├── summary.csv
├── metrics.json
├── results.sqlite
├── suite_manifest.json
├── report.json
├── data/
│   ├── cases.jsonl
│   ├── model_calls.jsonl
│   └── metrics.jsonl
└── artifacts/benchmarks/<benchmark>/<run-id>/<model>/{native,normalized}
```

A multi-model run uses `outputs/group_<UTC-run-id>/`, places the scientific comparative `report.xlsx` at its root, and creates `models/<model-id>/` subdirectories containing filtered `cases.csv` and native `metrics.json`. The workbook includes execution timing, model and quantization comparisons, attack taxonomy, retrieval security, Judge auditing, performance, native metrics, and case-level data.

Automation uses `outputs/automation_<UTC-run-id>/` with the same group report and per-model layout, plus `automation_manifest.json` and persistent job state in `results.sqlite`.

The XLSX report includes overall ASR and resistance, paired quantization comparisons, stratification by attack taxonomy, retrieval-security analysis, Judge auditing, performance statistics, native metrics, CIA coverage, and known coverage gaps. SQLite is canonical, JSONL is the lossless portable export, and CSV is the readable flat export. Subject, Judge, and attacker calls remain separately identifiable. Native artifacts remain authoritative for each benchmark. Report generation never calls a model or recomputes evaluation.

The manifest records:

- Benchmark repository and pinned commit
- Exact official configuration
- Hash of the official model configuration
- Benchmark-owned decoding parameters
- Selected models and providers
- Expected and completed calls
- Separate subject, Judge, and attacker role configurations and call logs
- Exact Judge prompt, parameters, and prompt hash
- Processed dataset hashes
- Fidelity qualification

## Fidelity rules

- Benchmark source is pinned and unmodified.
- Official datasets, splits, or explicitly qualified source-equivalent packaging are used.
- Official prompt and attack construction is used.
- Official evaluation is authoritative.
- Model selection does not change benchmark decoding parameters.
- Unsupported or incomplete installations fail validation.
- RAGnarok does not silently repair upstream behavior.
- No benchmark prompt, scoring rule, or attack payload is silently substituted.

See `BENCHMARK_INTEGRATION_REPORT.md` for the thesis-ready modification and coverage record.

The former custom PDF-RAG pilot is preserved on the `v2-pilot` branch and the `legacy-pdf-rag-final` tag.
