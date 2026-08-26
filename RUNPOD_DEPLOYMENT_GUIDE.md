# RAGnarok RunPod Deployment Guide

## Purpose

This guide deploys RAGnarok on a single ephemeral RunPod GPU for reproducible thesis evaluations. The recommended architecture keeps Subject inference on the rented GPU, uses a remote API only for benchmarks that require a Judge, runs exactly one Subject inference at a time, and overlaps safe background work such as Judge requests, model downloads, setup, and report generation.

The primary cost-optimized topology is:

```text
RunPod Community Pod
├── RAGnarok
├── Ollama
├── one active Subject model in GPU memory
├── local benchmark datasets and frozen retrieval artifacts
└── outputs and checkpoints under /workspace
        │
        └── remote Judge API for MPIB only
```

A Serverless endpoint is not required. A Pod is normally less expensive for a long, continuous benchmark session because the GPU remains busy for most of the run.

## 1. Requirements before renting a GPU

Do not create the Pod until every item below is complete.

- Push the exact RAGnarok revision to GitHub. Record the commit SHA used for the experiment.
- Decide the Subject model family, size, and quantization matrix.
- Verify that every quantized Ollama tag exists and derives from the intended base checkpoint.
- Accept the gated MPIB dataset terms on Hugging Face.
- Obtain the approved MPIB `payload_registry.json` required for exact V2 attacks.
- Record the Judge provider, API base URL, exact model ID, temperature, and output-token limit.
- Create the Judge API credential and a spending limit at the provider.
- Decide how completed outputs will leave the Pod before termination.
- Run the local 1B pilot successfully before starting the thesis model matrix.

The current repository template deliberately leaves Subject models disabled in `automation.toml`. Enable models only after their tags and provenance have been verified.

## 2. Choose the Pod

See [GPU_MODEL_PLAN.md](GPU_MODEL_PLAN.md) for the frozen price snapshot, model-to-GPU decision matrix, fallback rules, and calibration protocol.

For Llama or Qwen models up to approximately 8B in the planned quantizations, start with one 24 GB GPU such as an RTX 3090 or A5000. Select a larger GPU only when preflight or an actual model-load test proves that 24 GB is insufficient.

Recommended initial configuration:

| Setting | Recommendation |
|---|---|
| Cloud | Community Cloud for the lowest cost |
| GPU count | 1 |
| GPU memory | 24 GB |
| Base image | Current RunPod PyTorch image with CUDA 12 support |
| Container disk | 30-50 GB |
| Volume disk | Sized for the repository, datasets, current models, prefetch window, and outputs |
| Volume mount | `/workspace` |
| Exposed ports | None required for an in-Pod CLI run |
| SSH | Enabled |

Use this storage estimate:

```text
required space = framework and dependencies
               + prepared benchmark assets
               + current Subject model
               + prefetched Subject models
               + expected outputs
               + 10 GiB safety reserve
```

RAGnarok checks the declared model sizes before prefetching. Keep `min_free_disk_gb = 10` unless the dataset matrix proves that a larger reserve is needed.

RunPod distinguishes three storage types:

- Container disk is temporary and is cleared when the Pod stops.
- Volume disk is mounted at `/workspace`, survives a stop, and is deleted when the Pod is terminated.
- Network volume survives Pod deletion but adds persistent storage cost and is not required for the initial ephemeral strategy.

Official references: [RunPod Pod storage](https://docs.runpod.io/pods/storage/types) and [RunPod Pod management](https://docs.runpod.io/pods/manage-pods).

## 3. Add a cost safety limit

Set an automatic stop or termination deadline when creating the Pod. If the CLI is used, RunPod supports `--stop-after` and `--terminate-after` on Pod creation.

Use stop when an interrupted run may need to resume from `/workspace`. Use terminate only after outputs have been exported, because termination permanently deletes the Pod volume.

RunPod management reference: [runpodctl pod](https://docs.runpod.io/runpodctl/reference/runpodctl-pod).

## 4. Configure secrets

Never write API keys into `automation.toml`, Git, reports, or shell history. Create RunPod Secrets and map them to environment variables in the Pod template.

Example mappings:

```text
HF_TOKEN={{ RUNPOD_SECRET_huggingface_token }}
RAGNAROK_CREDENTIAL_DS4={{ RUNPOD_SECRET_ds4_api_key }}
```

The suffix of `RAGNAROK_CREDENTIAL_` must match the uppercased `credential_id` in the automation file. For example:

```toml
[benchmarks.judge]
credential_id = "ds4"
```

resolves from:

```text
RAGNAROK_CREDENTIAL_DS4
```

RunPod Secrets reference: [Manage secrets](https://docs.runpod.io/pods/templates/secrets).

## 5. Connect and validate the machine

Open the RunPod web terminal or connect through SSH, then run:

```bash
nvidia-smi
python --version
df -h /workspace
git --version
```

Confirm all of the following:

- The expected GPU is visible.
- NVIDIA drivers and CUDA are available.
- Python is 3.11 or newer; Python 3.12 is preferred.
- `/workspace` has the planned capacity.
- The Pod has internet access to GitHub, Hugging Face, Ollama, and the Judge provider.

## 6. Install RAGnarok

Clone the exact published revision into the persistent Pod volume:

```bash
cd /workspace
git clone --recurse-submodules https://github.com/mark-on/RAGnarok.git
cd RAGnarok
git rev-parse HEAD
```

Compare the printed commit with the thesis experiment record.

Create the environment. `--system-site-packages` reuses the PyTorch installation supplied by the RunPod image instead of downloading another large CUDA build when compatible:

```bash
python -m venv .venv --system-site-packages
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
ragnarok --help
```

If the preinstalled PyTorch version is incompatible with the pinned benchmark requirements, do not silently replace it during a thesis run. Record the conflict, select a compatible image, and recreate the environment.

## 7. Install and start Ollama

Install Ollama using its official Linux installer:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Store downloaded models in `/workspace` so they survive a Pod stop:

```bash
mkdir -p /workspace/ollama-models
export OLLAMA_MODELS=/workspace/ollama-models
nohup ollama serve >/workspace/ollama.log 2>&1 &
```

Verify the service:

```bash
curl http://127.0.0.1:11434/api/tags
ollama list
```

RAGnarok automation will download enabled Ollama models. Manual `ollama pull` is useful only for a one-model diagnostic.

## 8. Install and prepare benchmark dependencies

Optionally place the approved MPIB payload registry at:

```text
/workspace/RAGnarok/benchmarks/mpib/payload_registry.json
```

With this file, setup records `approved_registry` and restores exact V2 payloads. Without it, setup remains runnable and records `official_public_structural_mock`, matching the pinned public toolkit fallback. Do not describe the latter as exact restricted-payload evaluation in the thesis.

Then run setup:

```bash
source /workspace/RAGnarok/.venv/bin/activate
cd /workspace/RAGnarok
ragnarok setup --plain --workers 2
```

Two setup workers are a conservative starting point on a 24 GB Pod. Increase the value only after observing adequate RAM, disk, and GPU headroom. Dependency installation remains serial; independent benchmark preparation runs concurrently with separate logs.

Validate readiness:

```bash
ragnarok benchmarks
```

Do not start a paid experiment unless every selected benchmark reports `ready`.

## 9. Create a pilot automation file

Keep the thesis automation file unchanged and create a separate pilot file. The first cloud pilot should contain only MPIB Light, one Llama 3.2 1B quantization, and DS4 Flash as the remote Judge.

Example structure:

```toml
version = 1

[automation]
prefetch = true
download_concurrency = 1
cleanup_downloaded_models = true
resume = true
min_free_disk_gb = 10
ollama_url = "http://127.0.0.1:11434"
output_dir = "outputs"

[runtime]
retries = 2
retry_backoff_seconds = 0.5
subject_concurrency = 1
judge_concurrency = 4
postprocess_workers = 0

[[benchmarks]]
id = "mpib"
enabled = true

[benchmarks.options]
profile = "light"

[benchmarks.judge]
id = "ds4-judge"
adapter = "openai"
model = "REPLACE_WITH_EXACT_DS4_MODEL_ID"
base_url = "REPLACE_WITH_DS4_OPENAI_COMPATIBLE_BASE_URL"
credential_id = "ds4"
temperature = 0
max_output_tokens = 1024

[[models]]
id = "llama-3.2-1b-q4"
adapter = "ollama"
model = "llama3.2:1b-instruct-q4_K_M"
estimated_size_gb = 1.0
enabled = true
```

Do not use aliases such as `latest` for thesis runs. Freeze the exact model ID or digest when the provider exposes one.

## 10. Run preflight without inference

Run both checks before any benchmark call:

```bash
ragnarok preflight --file automation.pilot.toml
ragnarok auto --file automation.pilot.toml --dry-run --plain
```

Preflight validates disk, Ollama, credentials, selected benchmark readiness, and the available accelerator. Dry-run freezes and validates the execution plan without downloading models or calling the Subject or Judge.

Inspect the generated dry-run manifest and confirm:

- one enabled Subject model;
- only the intended benchmark;
- `subject_concurrency = 1`;
- the exact DS4 model and base URL;
- the expected Light profile;
- the intended output directory.

## 11. Run the pilot

Start the pilot:

```bash
ragnarok auto --file automation.pilot.toml --plain
```

Expected MPIB Light accounting for one Subject model:

```text
120 local Subject calls
120 remote Judge calls
```

The Subject remains serial. Remote Judge requests use the configured bounded queue and may overlap Subject generation. Local Ollama Judges are automatically forced to one request at a time.

Monitor the GPU and storage in a second terminal:

```bash
watch -n 2 nvidia-smi
watch -n 10 df -h /workspace
```

Monitor Ollama separately if needed:

```bash
ollama ps
tail -f /workspace/ollama.log
```

## 12. Pilot acceptance criteria

Do not proceed to larger models until all criteria pass.

- `suite_manifest.json` reports `complete`.
- Every expected job is marked complete.
- Subject calls equal the selected case count.
- Judge calls equal the MPIB case count.
- Judge invalid-JSON count is acceptably low and explicitly reported.
- Subject and Judge logs identify different providers and model IDs.
- No second Subject inference worker is recorded.
- `report.pdf` or `group_report.pdf` opens correctly.
- `responses.csv`, `summary.csv`, `metrics.json`, and `results.sqlite` exist.
- The PDF metrics agree with the CSV and native benchmark metrics.
- Resume is tested once by interrupting a disposable pilot and continuing from the same suite directory.
- Output export is tested before the Pod is terminated.

The 1B pilot validates plumbing, not thesis conclusions. Do not include its security score in the final quantization comparison unless the model is explicitly part of the approved experimental matrix.

## 13. Prepare the thesis automation file

After the pilot passes:

1. Freeze the final model family and size.
2. Enable only quantizations derived from the same base checkpoint.
3. Record every Ollama tag, digest, file size, quantization method, and weight hash available.
4. Keep benchmark profile, Subject prompt, Judge prompt, Judge model, temperature, and runtime settings identical across quantizations.
5. Use one automation suite for quantizations of the same model size.
6. Keep different model sizes in separate suites and reports.

Recommended concurrency starting values:

```toml
[automation]
download_concurrency = 2

[runtime]
subject_concurrency = 1
judge_concurrency = 4
postprocess_workers = 0
```

If the Judge provider returns rate-limit errors, reduce `judge_concurrency` to 2 before changing retry behavior. Record the final value in the thesis methodology.

## 14. Checkpoints and resume

Automation stores persistent job state in:

```text
outputs/automation_<UTC-run-id>/results.sqlite
```

To resume, set the existing directory in the automation file:

```toml
[automation]
resume_suite = "outputs/automation_<UTC-run-id>"
```

The frozen models, benchmarks, and runtime configuration must match the original manifest. RAGnarok rejects a mismatched resume instead of combining incompatible results.

Stopping the Pod preserves `/workspace`. Terminating the Pod deletes it. A checkpoint cannot recover data after termination unless the suite was copied elsewhere.

## 15. Export results

The canonical suite directory contains:

```text
outputs/automation_<UTC-run-id>/
├── group_report.pdf
├── summary.csv
├── results.sqlite
├── suite_manifest.json
├── automation_manifest.json
├── data/
├── models/
└── artifacts/
```

Preferred export options:

1. Configure `sync_command` to copy the suite to object storage after each completed model.
2. Use RunPod Cloud Sync to export `/workspace/RAGnarok/outputs`.
3. Create an archive and download it before termination.

Example archive:

```bash
cd /workspace/RAGnarok
tar -czf /workspace/ragnarok-results.tar.gz outputs/automation_<UTC-run-id>
```

Verify the downloaded archive locally before deleting the Pod. At minimum, compare its size and SHA-256 digest:

```bash
sha256sum /workspace/ragnarok-results.tar.gz
```

RunPod recommends external backup for critical results: [RunPod storage and transfer guidance](https://docs.runpod.io/pods/storage/types).

## 16. Stop or terminate

Use **Stop** when:

- the run is interrupted;
- outputs have not yet been exported;
- the same volume must be resumed.

Use **Terminate** only when:

- the suite is complete;
- reports and raw artifacts were exported;
- the archive was verified locally;
- no model or dataset on the Pod must be retained.

Stopping releases the GPU but continues volume-disk billing. Termination deletes non-network-volume data. Check current prices in the [RunPod pricing documentation](https://docs.runpod.io/pods/pricing).

## 17. Failure handling

### Judge rate limiting

- Reduce `judge_concurrency` from 4 to 2.
- Keep temperature, prompt, and model unchanged.
- Resume the same suite after confirming checkpoint state.
- Record rate-limit and retry counts in the experiment notes.

### Out of memory

- Confirm only one Subject model is loaded with `ollama ps`.
- Verify that the requested context and model fit the GPU.
- Do not introduce CPU offload silently during a thesis comparison.
- Move the entire quantization group to a larger GPU if one member cannot run under the common hardware protocol.

### Disk pressure

- Reduce `download_concurrency` to 1.
- Increase volume size before the run.
- Confirm automation removes only models it downloaded itself.
- Never delete a model or suite with an incomplete checkpoint.

### Pod interruption

- Stop destructive cleanup.
- Restart the Pod if the volume remains available.
- Confirm Ollama uses `/workspace/ollama-models`.
- Set `resume_suite` to the existing automation directory.
- Run preflight again before resuming.

### Invalid Judge JSON

- Inspect `judge_requests.jsonl`.
- Confirm the exact Judge model and endpoint.
- Do not repair outputs manually.
- Invalid judgments must remain excluded according to the benchmark protocol.

## 18. Final pre-run checklist

- [ ] Exact Git commit published and recorded
- [ ] Pod GPU and storage selected
- [ ] Automatic stop or termination deadline configured
- [ ] Secrets configured without plaintext keys
- [ ] Ollama model directory under `/workspace`
- [ ] All selected benchmarks report ready
- [ ] MPIB gated assets and payload registry validated
- [ ] Exact Subject tags and quantization provenance frozen
- [ ] Exact Judge model, endpoint, prompt, and parameters frozen
- [ ] `subject_concurrency = 1`
- [ ] Judge and download concurrency limits recorded
- [ ] Preflight passed
- [ ] Dry-run manifest inspected
- [ ] Output synchronization or download method tested
- [ ] Pilot completed before the thesis matrix

## Current project-specific blockers

At the time this guide was written, the following items still require confirmation before a RunPod thesis run:

- The current local changes must be committed and pushed to GitHub.
- MPIB, SPIKEE, and AgentDojo still require full setup and live upstream validation.
- The exact DS4 Flash API base URL and model ID must be frozen.
- The final Subject model and quantization matrix has not been finalized.
- The repository Dockerfile has not yet been published as a versioned container image and does not bundle Ollama or prepared benchmark assets.

The correct next milestone is the local Llama 3.2 1B plus DS4 Flash API pilot. RunPod deployment should begin only after that pilot validates the complete Judge, output, report, and resume paths.
