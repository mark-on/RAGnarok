# Vast.ai Remote Ollama Guide

This setup keeps RAGnarok, benchmarks, datasets, and results on the local PC while running LLM inference on a Vast.ai GPU.

```text
Local RAGnarok -> localhost:11434 -> SSH tunnel -> Vast.ai Ollama -> NVIDIA GPU
```

## Requirements

- A running Vast.ai instance with an NVIDIA GPU
- SSH access configured in Vast.ai
- Ollama installed on the instance
- The required Ollama model downloaded on the instance
- Local Ollama completely stopped to free port `11434`

## First-time remote setup

Copy the current Direct SSH command from the Vast.ai instance page and connect from PowerShell:

```powershell
ssh -p SSH_PORT root@INSTANCE_IP
```

Verify the GPU:

```bash
nvidia-smi
```

Install Ollama if it is not already installed:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Download a model:

```bash
ollama pull qwen3.5:0.8b
```

## Start a benchmark session

### 1. Start Ollama remotely

Connect to the instance and run:

```bash
ollama serve
```

Keep this terminal open.

### 2. Create the SSH tunnel

Open another local PowerShell window:

```powershell
ssh -o ExitOnForwardFailure=yes -N -L 11434:127.0.0.1:11434 -p SSH_PORT root@INSTANCE_IP
```

Keep this terminal open. Always use the current IP address and SSH port shown by Vast.ai.

### 3. Verify the remote endpoint

Open another local PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:11434/api/tags
```

The response must include the remote model.

### 4. Run RAGnarok

```powershell
cd C:\Users\labro\Desktop\Code\Projects\RAGnarok
.venv\Scripts\Activate.ps1
ragnarok run
```

Select `Ollama`. Although the UI describes it as local, `localhost:11434` is forwarded to the remote Ollama server through SSH.

For a minimal validation run, select:

```text
Model: qwen3.5:0.8b
Benchmark: PoisonedRAG
Profile: Light
```

## After restarting the instance

The model remains available when an instance is stopped and restarted, but processes and SSH tunnels do not. Repeat these steps:

1. Wait for the instance to reach `Running`.
2. Copy the current Direct SSH command.
3. Run `ollama serve` remotely.
4. Recreate the SSH tunnel locally.
5. Verify `/api/tags`.
6. Start `ragnarok run`.

## Verification and troubleshooting

Check whether the model is loaded on the GPU:

```bash
ollama ps
nvidia-smi
```

If local port `11434` cannot be bound, close local Ollama and run:

```powershell
Get-Process -Name ollama* -ErrorAction SilentlyContinue | Stop-Process -Force
```

Then recreate the tunnel.

If RAGnarok cannot list models, verify that:

- `ollama serve` is still running remotely;
- the SSH tunnel window is still open;
- the current Vast.ai IP address and SSH port are correct;
- `Invoke-RestMethod http://127.0.0.1:11434/api/tags` returns the model list.

## Ending the session

1. Stop or complete RAGnarok.
2. Close the SSH tunnel with `Ctrl+C`.
3. Stop the Vast.ai instance if it will be reused soon, or destroy it if its data is no longer needed.

Vast.ai continues charging for allocated storage while a stopped instance exists. Destroying the instance deletes its container data, including downloaded Ollama models.
