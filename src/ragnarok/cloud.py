from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from .automation import AutomationFile
from .benchmarks import benchmark_for
from .credentials import CredentialError, resolve_credential


def cloud_preflight(configuration: AutomationFile) -> dict:
    checks: list[dict[str, object]] = []
    output_dir = configuration.automation.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(output_dir).free / (1024 ** 3)
    checks.append({
        "name": "disk",
        "ok": free_gb >= configuration.automation.min_free_disk_gb,
        "detail": f"{free_gb:.1f} GiB free",
        "required": True,
    })
    checks.append({
        "name": "python",
        "ok": sys.version_info >= (3, 11),
        "detail": platform.python_version(),
        "required": True,
    })
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        gpu_detail = gpu.stdout.strip() or gpu.stderr.strip() or "NVIDIA GPU unavailable"
        gpu_ok = gpu.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        gpu_ok, gpu_detail = False, "nvidia-smi unavailable; CPU or non-NVIDIA backend"
    checks.append({"name": "accelerator", "ok": gpu_ok, "detail": gpu_detail, "required": False})

    requires_ollama = any(model.adapter == "ollama" for model in configuration.models)
    if requires_ollama:
        try:
            response = httpx.get(f"{configuration.automation.ollama_url.rstrip('/')}/api/tags", timeout=5)
            response.raise_for_status()
            ollama_ok, ollama_detail = True, "Ollama API reachable"
        except httpx.HTTPError as exc:
            ollama_ok, ollama_detail = False, str(exc)
        checks.append({"name": "ollama", "ok": ollama_ok, "detail": ollama_detail, "required": True})

    for selection in configuration.benchmarks:
        adapter = benchmark_for(selection.id)
        problems = [*adapter.validate_installation(), *adapter.validate_prepared()]
        checks.append({
            "name": f"benchmark:{selection.id}",
            "ok": not problems,
            "detail": "ready" if not problems else "; ".join(problems),
            "required": True,
        })
        for role_name, role_model in (("judge", selection.judge), ("attacker", selection.attacker)):
            if role_model is None or role_model.adapter == "ollama":
                continue
            try:
                available = bool(resolve_credential(role_model.credential_id))
                detail = "credential available" if available else "credential unavailable"
            except CredentialError as exc:
                available, detail = False, str(exc)
            checks.append({
                "name": f"credential:{selection.id}:{role_name}",
                "ok": available,
                "detail": detail,
                "required": True,
            })
    ready = all(bool(item["ok"]) for item in checks if item["required"])
    return {
        "ready": ready,
        "platform": platform.platform(),
        "output_dir": str(Path(output_dir).resolve()),
        "checks": checks,
    }
