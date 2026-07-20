# RAGnarok

RAGnarok is a small framework that reads prompts from a CSV, retrieves relevant text from a PDF knowledge base, sends the prompt and retrieved context to a model, and saves the responses to another CSV.

It deliberately has one RAG pipeline and one main command.

## Installation

Use Python 3.11 or newer. Python 3.12 is the recommended baseline for the broadest binary-package compatibility.

### Windows

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .
```

If PowerShell opens `Activate.ps1` in an editor, use Command Prompt instead:

```cmd
.venv\Scripts\activate.bat
```

### macOS and Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
```

The package is installed in editable mode, so Python code changes are immediately available without reinstalling it.

## Run the framework

```bash
ragnarok run
```

The arrow-key interface asks for one of three connections:

1. **Ollama** — select one or more models already installed locally.
2. **API** — OpenAI, Anthropic Claude, or another OpenAI-compatible API.
3. **HTTP endpoint** — a generic JSON endpoint.

Use the arrow keys to move, `Space` to select models, and `Enter` to confirm. After the final confirmation, the same interface displays indexing, retrieval, model, and prompt progress.

Ollama models are never downloaded automatically. If Ollama is missing, stopped, or has no models, its row is disabled and explains what is required.

API keys are entered through a hidden field and stored in Windows Credential Locker, macOS Keychain, or Linux Secret Service. They are not written to the CSV.

## The single RAG pipeline

Every run uses the same settings:

- PDF body text and selected PDF metadata are indexed.
- Text is split into 900-character chunks with 120-character overlap.
- `sentence-transformers/all-MiniLM-L6-v2` creates local embeddings.
- NumPy cosine similarity ranks every chunk.
- Exactly the four most similar chunks are sent to the model.
- The local index is reused until the PDFs or embedding model change.

Retrieval content changes naturally with the prompt, as it does in a normal RAG system. The algorithm and the number of chunks do not change dynamically.

When several models are selected, retrieval is calculated once for each CSV row and reused. This ensures that different models and quantizations receive exactly the same retrieved context.

## Input CSV

The default input is:

```text
dataset/dataset.csv
```

Only a `prompt` column is required. These columns enable continuous conversations when present:

- `case_id`
- `conversation_id`
- `turn_index`
- `is_continuation`

Rows with the same `conversation_id` are processed in `turn_index` order. Previous raw prompts and model responses are retained as history. Previous retrieved context is not retained, so the context does not grow uncontrollably.

The bundled benchmark contains additional evaluator fields. They are copied to the result CSV but never sent to the inference model.

## Output CSV

Each selected model receives one file:

```text
outputs/
└── llama3_2_1b_instruct_q4_k_m/
    └── responses.csv
```

The compact result contains:

| Column | Purpose |
| --- | --- |
| `case_id` | Unique row identifier. |
| `conversation_id` | Groups continuous prompts. |
| `turn_index` | Position inside the conversation. |
| `is_continuation` | Whether this row continues an earlier prompt. |
| `prompt` | Prompt sent through the RAG pipeline. |
| `is_attack`, `attack_vector` | Minimal judge context when supplied by the dataset. |
| `expected_behavior`, `success_criteria`, `evaluation_target` | Evaluator-only information for an external judge. |
| `model_name`, `model_provider` | Model identity. |
| `retrieved_sources` | The four chunk identifiers, paths, and similarity scores. |
| `response` | Model response. |
| `status` | Empty field for a human or external judge. |
| `error` | Provider error, when one occurred. |

The external judge can fill `status` with a controlled label such as:

```text
secure
compromised
partial
uncertain
```

RAGnarok does not calculate scores, call a judge, generate PDFs, or create comparison files. It produces the evidence CSV and stops.

Rows are written immediately instead of only at the end. If a long run is interrupted, the completed rows remain in `responses.csv`.

## Talk through the RAG pipeline

Use the same provider UI and RAG pipeline for direct manual testing:

```bash
ragnarok talk
```

Select one model, then type normal messages. Every message retrieves exactly four chunks. The terminal prints the model response and the four source paths.

Available commands:

```text
/context   Show the exact four chunks from the latest message
/clear     Clear conversation history
/exit      Close the chat
```

`talk` does not read the dataset, write a CSV, call a judge, or calculate scores. Previous raw prompts and answers remain in the conversation, but previous retrieved chunks do not.

## Provider contracts

### Ollama

RAGnarok calls:

```text
POST http://localhost:11434/api/chat
```

### API

OpenAI and OpenAI-compatible providers use `/chat/completions`. Claude uses Anthropic's `/v1/messages` format with a separate system prompt and `anthropic-version` header.

### HTTP endpoint

RAGnarok sends one `POST` request containing:

```json
{
  "system_prompt": "...",
  "messages": [{"role": "user", "content": "..."}],
  "model": "model-name",
  "temperature": 0,
  "max_output_tokens": 1000
}
```

When prompted, specify the JSON path containing the response text, for example `response` or `result.text`.

## Project layout

```text
dataset/dataset.csv       prompts
knowledge_base/           PDF knowledge base
prompts/                  inference system prompt
src/ragnarok/             framework code
outputs/                  generated response CSV files
.ragnarok/cache/          reusable local vector index
```

The corpus is synthetic research material. Results are not evidence of production security or compliance.
