# RAGnarok evaluation framework

The installable `ragnarok` package executes the existing benchmark without changing its public 17-column CSV or PDF corpus. Python 3.11 or newer is required.

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
ragnarok --help
```

Sentence Transformers supplies production local embeddings; NumPy performs cosine ranking without an external vector database. The `mock` embedding backend and model provider are deterministic and require no network or downloaded weights.

## Simple workflow

```powershell
ragnarok setup
ragnarok validate
ragnarok run
```

`setup` is an interactive wizard and can be run again whenever the configuration needs to change. It supports selecting multiple inference models from a local or remote provider and configuring an independent local, remote, shared-connection, mock, or disabled judge. Every selection screen offers an exit option. Choices and entered secrets remain in memory until the final Save action, so exiting leaves both the existing YAML and credential store unchanged. Remote API keys are entered through a hidden prompt and stored using the operating system credential service: Windows Credential Locker, macOS Keychain, or Linux Secret Service. YAML contains only a `credential_id`; environment variables remain an optional override for CI/CD and headless automation.

`run` performs validation, provider checks, index creation/loading, inference, evaluation, model reports, and comparison automatically.

For a five-row, completely offline two-model demonstration:

```powershell
ragnarok run --mock --quick
```

Use `ragnarok run --resume` after an interruption. Advanced diagnostic and recovery commands remain installed but are hidden from the beginner-facing help.

## Security boundary

Inference prompts contain only the configured system prompt, the current conversation's previous user/assistant messages, the current public `prompt`, and chunks returned by normal retrieval. Attack labels, expected answers, targets, document roles, obfuscation labels, and `source_document` are evaluator-only. `knowledge_base_attack_manifest.md` and every non-PDF file are rejected as index input.

For interactive use, the framework resolves credential references through Windows Credential Locker, macOS Keychain, or Linux Secret Service. Environment variables override the keychain when present, which supports CI/CD without changing YAML. Configuration snapshots contain only references and redact literal headers; logs contain operational fields only, and reports omit protected targets and decoded payloads.

## Providers

- `mock`: deterministic offline testing.
- `ollama`: local `/api/chat`, model availability checks, timeouts, and bounded retries. It never downloads models.
- `openai_compatible`: standard `/chat/completions` APIs with environment-provided bearer credentials.
- `custom_http`: configurable method, endpoint, headers, authentication, nested request mappings, response/token/error paths, timeouts, and retries.

## Outputs

Every model receives a separate `outputs/<experiment>/<model-id>/` directory. Its `results.csv` is a copy of the benchmark with the original empty response column populated and evaluation columns appended in that same file. The directory also contains retrieval evidence, summary JSON, Markdown/PDF report, run manifest, sanitized configuration snapshot, JSONL operational log, checkpoints, and charts. The original `dataset/dataset.csv` is never overwritten. Cross-model `comparison.csv`, `comparison.json`, `comparison.md`, and `comparison.pdf` files are written at the experiment root after all selected models finish.

Run manifests hash the dataset, PDF corpus, system prompt, configuration, and local index and record dependency versions, extraction policy, retrieval settings, models, timestamps, Git revision, and sanitized host information.
