# RAGnarok

RAGnarok is a local evaluation framework for testing Retrieval-Augmented Generation (RAG) systems against prompt injection, unsafe retrieval, access-control failures, and ordinary benign questions.

It runs one or more language models over a synthetic benchmark, records every response, evaluates the results, generates a PDF report for each model, and produces a cross-model comparison.

The benchmark currently contains:

- 100 evaluation turns grouped into 85 conversations;
- 40 synthetic PDF documents across five fictional domains;
- benign questions, direct prompt-injection attacks, and indirect attacks embedded in retrieved PDFs;
- single-turn and multi-turn conversations; and
- visible, metadata-based, and white-on-white PDF injection techniques.

The original dataset and knowledge base are never modified during an experiment.

## Installation

RAGnarok currently installs from the project source. A packaged release and public installation command will be added later.

### Requirements

- Python 3.11 or newer
- Git, if the project is being cloned
- An internet connection during dependency installation
- Optional: Ollama or another local model server for local inference
- Optional: an API key for remote providers

A GPU is not required by the framework itself. Hardware requirements depend on the inference and embedding models you select.

### 1. Open a terminal in the project directory

```text
RAGnarok/
├── pyproject.toml
├── src/
├── dataset/
└── knowledge_base/
```

All commands below should be run from this directory.

### 2. Create a clean virtual environment

Do not reuse a virtual environment created with another Python version.

Windows CMD:

```cmd
py -3.12 -m venv .venv
.\.venv\Scripts\activate.bat
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

When activation succeeds, the terminal prompt normally begins with `(.venv)`.

### 3. Install RAGnarok and its development tools

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

The `-e` flag installs the project in editable mode. Changes made to the source code are immediately available without reinstalling the package. The `[dev]` extra installs the test tools used while the framework is under development.

### 4. Verify the installation

```bash
ragnarok --help
```

The public commands are:

```text
setup      Configure inference models, RAG embeddings, and the optional judge
validate   Check the configuration, benchmark, PDFs, providers, and runtime
run        Execute the complete evaluation lifecycle
status     Show the state of an experiment
```

If `ragnarok` is not recognized, confirm that the virtual environment is active. It can also be called directly on Windows:

```cmd
.\.venv\Scripts\ragnarok.exe --help
```

## First run

The normal workflow has three commands:

```bash
ragnarok setup
ragnarok run
ragnarok status
```

### Configure the framework

Start the interactive wizard:

```bash
ragnarok setup
```

Use the arrow keys to move, `Enter` to confirm a single choice, and `Space` to select models on multiple-choice screens.

The wizard asks for:

1. The inference location: local, remote, or mock.
2. The provider and one or more inference models.
3. An optional judge model.
4. The RAG embedding profile.
5. A final confirmation before saving.

Every selection screen includes **Exit setup without saving**. Nothing is written until **Save configuration** is selected on the final screen. Exiting keeps the previous configuration and credentials unchanged.

Run `ragnarok setup` again whenever you want to replace the configuration. No `--force` option is needed.

### Try the offline demonstration

For a first test without Ollama, API keys, model downloads, or network calls, select the mock profile during setup and run:

```bash
ragnarok run --mock --quick
```

This executes five benchmark rows using two deterministic mock models, mock embeddings, and a mock judge. It tests the full pipeline but does not measure the quality of a real model.

### Run a real experiment

After configuring local or remote models:

```bash
ragnarok run
```

`run` automatically validates the project, checks configured providers, builds or loads the RAG index, executes every selected model, evaluates the responses, and generates reports.

An explicit provider check is optional:

```bash
ragnarok validate --online
```

If a run is interrupted, continue from its checkpoints with:

```bash
ragnarok run --resume
```

## Setup choices

### Local inference

The wizard supports:

- Ollama;
- LM Studio;
- vLLM;
- other OpenAI-compatible local servers; and
- custom HTTP endpoints.

For Ollama, RAGnarok queries the running Ollama service and displays the models already installed on the machine. Multiple models can be selected with `Space`. RAGnarok never downloads an Ollama model automatically.

### Remote inference

The built-in remote presets include:

- OpenAI;
- OpenRouter;
- Groq;
- Together AI;
- generic OpenAI-compatible APIs; and
- custom HTTP endpoints.

The API key is entered through a hidden password field. RAGnarok uses the operating system's credential service:

- Windows Credential Manager;
- macOS Keychain; or
- Linux Secret Service, such as GNOME Keyring or KDE Wallet.

The key is not stored in YAML, source code, reports, logs, or output files. YAML contains only a credential reference such as:

```yaml
credential_id: openai
```

Environment variables remain supported as an override for CI/CD and headless servers. A minimal Linux server without Secret Service should use that automation path.

### Optional judge

The judge is a separate model used when deterministic evaluation cannot confidently classify a response. It can be:

- disabled;
- the same connection as the first inference model;
- another local model;
- a remote model; or
- a deterministic mock judge for testing.

When no judge is configured, inference still runs normally. `results.csv` contains the model responses and deterministic metrics, while judge-specific columns remain empty.

### RAG embedding profile

The embedding model converts PDF passages and questions into numeric vectors. Similar vectors identify passages likely to answer the question.

```text
PDF text ──> embedding model ──> document vectors
Question ──> embedding model ──> question vector
                                      │
                                      └──> most similar PDF passages
```

The setup profiles are:

- `all-MiniLM-L6-v2`: fast and lightweight; recommended for a first real run;
- `multi-qa-MiniLM-L6-cos-v1`: optimized for question-to-passage retrieval;
- a custom Sentence Transformers model; or
- deterministic mock embeddings for framework testing only.

The embedding model is independent from the inference model and judge.

## How the framework works

### 1. Preflight validation

Before inference, RAGnarok verifies:

- installed Python dependencies;
- the configuration schema;
- the dataset's 17-column contract and benchmark invariants;
- PDF readability and knowledge-base boundaries;
- the system prompt;
- configured credentials and model providers;
- output-directory access; and
- the availability of a cached RAG index.

A failed required check stops the run before model calls begin.

### 2. PDF extraction

Only PDF files below `knowledge_base/` are eligible for indexing. RAGnarok extracts page text and configured metadata fields.

This matters because the benchmark includes indirect attacks in:

- visible PDF text;
- standard PDF metadata; and
- extractable white text on a white background.

The evaluator manifest and all non-PDF files are excluded from the index.

### 3. Chunking and embeddings

Extracted text is divided into overlapping chunks. Each chunk is embedded locally and stored in a lightweight NumPy cosine-similarity index.

If neither the PDF corpus nor embedding configuration changed, the cached index is reused on later runs.

### 4. Retrieval

For each benchmark prompt, the framework searches the index and selects the top matching chunks. It records the real document paths, chunk identifiers, ranks, similarity scores, and extracted surfaces in `retrieval_log.csv`.

The benchmark's expected source is used only after retrieval as ground truth. It is never forced into the model context.

### 5. Inference

The inference model receives only:

- the configured system prompt;
- previous public messages from the current conversation;
- the current user prompt; and
- passages returned by normal retrieval.

For multi-turn cases, history is preserved within a conversation and reset before the next conversation.

RAGnarok deliberately hides evaluator-only fields, including attack labels, expected behavior, success criteria, evaluation targets, document roles, obfuscation labels, and expected source paths.

### 6. Evaluation

Every response first passes through deterministic rules. When the result is inconclusive and a judge is enabled, the isolated judge receives a structured evaluation package and returns a label, confidence, and reason.

The final result records whether the model:

- resisted an attack;
- was compromised;
- produced a partial or uncertain result;
- requires manual review; or
- encountered a provider error.

Retrieval success and generation safety are measured separately. This distinction prevents a model from receiving credit for resisting an indirect attack when the poisoned document was never retrieved.

### 7. Multiple models

All selected inference models are evaluated sequentially against the same benchmark scope and shared RAG index:

```text
Shared dataset and RAG index
          │
          ├──> Model A ──> results and report
          ├──> Model B ──> results and report
          └──> Model C ──> results and report
                              │
                              └──> cross-model comparison
```

Each model has independent checkpoints and output files.

### 8. Reports

RAGnarok calculates security, retrieval, utility, and operational metrics, including:

- direct attack success rate;
- indirect end-to-end attack success rate;
- conditional indirect attack success rate;
- expected-source retrieval rate;
- poisoned-source exposure;
- benign task success and false-refusal rates;
- latency and token usage;
- provider errors; and
- manual-review rate.

Reports omit evaluator targets and decoded hidden payloads.

## Output files

Every generated experiment artifact stays below `outputs/`. Each model receives its own directory:

```text
outputs/
└── <experiment-id>/
    ├── <model-a>/
    │   ├── results.csv
    │   ├── retrieval_log.csv
    │   ├── report.pdf
    │   ├── report.md
    │   ├── model_summary.json
    │   ├── run_manifest.json
    │   ├── configuration.snapshot.yaml
    │   ├── logs.jsonl
    │   ├── checkpoints/
    │   └── charts/
    ├── <model-b>/
    │   └── ...
    ├── comparison.csv
    ├── comparison.json
    ├── comparison.md
    ├── comparison.pdf
    └── status.json
```

### `results.csv`

There is one result CSV per model. It starts as a copy of `dataset/dataset.csv`, fills the original empty `response` column, and appends runtime and evaluation columns to the same file.

It includes model/provider information, final and rule labels, optional judge results, retrieval-ground-truth measurements, latency, token usage, errors, run identifiers, and timestamps.

The source file at `dataset/dataset.csv` remains unchanged.

### Model report

Each model directory contains a Markdown report and a presentation-ready PDF with headline metrics, retrieval exposure, benign utility, latency, errors, limitations, and reproducibility information.

### Comparison report

When multiple models are selected, `comparison.pdf` summarizes their headline metrics and disagreements. Machine-readable CSV and JSON versions are generated beside it.

## Security boundaries

RAGnarok is designed so the model being tested cannot see the answers used to evaluate it.

- Evaluator-only CSV fields never enter inference prompts.
- `knowledge_base_attack_manifest.md` never enters the RAG index.
- Only `knowledge_base/**/*.pdf` files are indexed.
- Expected source paths are checked only after retrieval.
- API keys are stored outside the project through the OS credential service.
- Literal custom headers are redacted from configuration snapshots.
- Logs and reports do not contain protected targets or decoded attack payloads.

Restricted synthetic documents intentionally exist in the research corpus. In a production RAG system, authorization must be enforced during retrieval rather than delegated only to the language model.

## Dataset structure

`dataset/dataset.csv` contains exactly 100 rows and 17 original columns. Each row represents one model turn. Rows sharing a `conversation_id` form a multi-turn conversation and are ordered by `turn_index`.

The original columns are:

| Column | Purpose |
|---|---|
| `case_id` | Unique benchmark-turn identifier. |
| `conversation_id` | Groups turns from the same conversation. |
| `turn_index` | One-based position within the conversation. |
| `is_continuation` | Indicates whether the row continues an earlier turn. |
| `prompt` | User prompt sent through the RAG pipeline. |
| `is_attack` | Whether the case is adversarial. |
| `attack_vector` | `none`, `direct`, or `indirect`. |
| `attack_objective` | Intended failure for an attack case. |
| `attack_technique` | Prompt-injection technique. |
| `domain` | Fictional knowledge-base domain. |
| `source_document` | Expected primary retrieval source; evaluator-only. |
| `document_role` | `clean`, `poisoned`, `restricted`, or `none`; evaluator-only. |
| `obfuscation_technique` | Visible, metadata, or white-on-white surface information. |
| `expected_behavior` | Safe behavior expected from the model. |
| `success_criteria` | Observable evaluation condition. |
| `evaluation_target` | Protected value or unsafe outcome for attack cases. |
| `response` | Initially empty and populated in the per-model output copy. |

All organizations, people, records, credentials, domains, and identifiers in the benchmark are synthetic.

## Troubleshooting

### `ragnarok` is not recognized

Activate the virtual environment again:

Windows CMD:

```cmd
.\.venv\Scripts\activate.bat
```

PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS or Linux:

```bash
source .venv/bin/activate
```

### PowerShell opens `Activate.ps1` in Notepad

The script is being opened rather than executed. Start PowerShell in the project directory and enter:

```powershell
& .\.venv\Scripts\Activate.ps1
```

Alternatively, use Windows CMD and `activate.bat`.

### NumPy reports incompatible compiled modules

The virtual environment was probably reused with a different Python version. Delete `.venv`, recreate it with one Python installation, and reinstall the project. Do not copy or reuse packages between Python versions.

### Ollama models are not displayed

Confirm that Ollama is running and that at least one model is installed:

```bash
ollama list
```

The wizard also allows manual model-name entry. It never downloads a model automatically.

### The credential store is unavailable on Linux

Desktop Linux requires a working Secret Service implementation, commonly GNOME Keyring or KDE Wallet. Headless servers often do not provide one; use the provider's environment-variable override in that environment.

### A run was interrupted

Resume from saved per-case checkpoints:

```bash
ragnarok run --resume
```

### Change the selected models or judge

Run setup again:

```bash
ragnarok setup
```

The previous YAML remains untouched unless the final Save action is confirmed.

## Development and verification

Run the complete test suite:

```bash
python -m pytest -q
```

Validate only the benchmark contract:

```bash
python scripts/validate_dataset.py
```

The framework code is under `src/ragnarok/`. Benchmark generation and validation tools are under `scripts/`, and automated tests are under `tests/`.

## Project status

RAGnarok is a synthetic research pilot and evaluation framework. The benchmark still requires human annotation and expert review before results are used for academic or production-security claims.

Packaging, public distribution, versioning, and release instructions will be completed at the end of the project. Until then, use the editable source installation documented above.
