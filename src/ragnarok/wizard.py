from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import questionary
from prompt_toolkit.styles import Style
from questionary import Choice

from .credentials import CredentialError, get_stored_credential


STYLE = Style.from_dict({
    "qmark": "fg:#5f87ff bold",
    "question": "bold",
    "answer": "fg:#5fd7af bold",
    "pointer": "noinherit fg:#ffffff bg:#176b57 bold",
    "highlighted": "noinherit fg:#ffffff bg:#176b57 bold",
    "selected": "noinherit fg:#5fd7af bold",
    "instruction": "fg:#808080",
    "disabled": "fg:#666666 italic",
})


class RunCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class OllamaAvailability:
    state: str
    executable: str | None = None
    model_count: int = 0


def _answer(question):
    value = question.ask()
    if value is None:
        raise RunCancelled("run cancelled")
    return value


def select(message: str, choices: list[Choice], default: Any = None):
    value = _answer(questionary.select(
        message,
        choices=[*choices, Choice("Exit", value="__exit__")],
        default=default,
        style=STYLE,
        pointer="»",
        instruction="(use ↑/↓ and Enter)",
        use_arrow_keys=True,
        use_jk_keys=True,
    ))
    if value == "__exit__":
        raise RunCancelled("run cancelled")
    return value


def checkbox(message: str, choices: list[Choice]) -> list[Any]:
    values = _answer(questionary.checkbox(
        message,
        choices=[*choices, Choice("Exit", value="__exit__")],
        style=STYLE,
        pointer="»",
        instruction="(↑/↓ move, Space selects, Enter confirms)",
        validate=lambda selected: True if "__exit__" in selected or selected else "Select at least one model with Space.",
    ))
    if "__exit__" in values:
        raise RunCancelled("run cancelled")
    return values


def text(message: str, default: str = "") -> str:
    return _answer(questionary.text(
        message,
        default=default,
        validate=lambda value: True if value.strip() else "A value is required",
        style=STYLE,
    )).strip()


def password(message: str) -> str:
    return _answer(questionary.password(
        message,
        validate=lambda value: True if value else "A value is required",
        style=STYLE,
    ))


def note(message: str) -> None:
    questionary.print(message, style="fg:#808080")


def _model_id(name: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "model"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _credential_id(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:80] or "api"


def capture_credential(credential_id: str, label: str, pending: dict[str, str]) -> str:
    if credential_id in pending:
        action = select(
            f"A {label} credential was entered earlier in this setup",
            [Choice("Use the entered credential", "use"), Choice("Replace credential", "replace")],
            "use",
        )
        if action == "use":
            return pending[credential_id]
    try:
        existing = get_stored_credential(credential_id)
    except CredentialError as exc:
        note(str(exc))
        existing = None
    if existing:
        action = select(
            f"A saved {label} credential exists",
            [Choice("Use saved credential", "use"), Choice("Replace credential", "replace")],
            "use",
        )
        if action == "use":
            return existing
    secret = password(f"Enter the {label} key (hidden)")
    pending[credential_id] = secret
    return secret


def find_ollama_executable() -> str | None:
    executable = shutil.which("ollama")
    if executable:
        return executable
    candidates: list[Path] = []
    if platform.system() == "Windows" and os.getenv("LOCALAPPDATA"):
        candidates.append(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Ollama" / "ollama.exe")
    elif platform.system() == "Darwin":
        candidates.extend([Path("/usr/local/bin/ollama"), Path("/Applications/Ollama.app/Contents/Resources/ollama")])
    else:
        candidates.extend([Path("/usr/local/bin/ollama"), Path("/usr/bin/ollama")])
    return str(next((path for path in candidates if path.is_file()), "")) or None


def discover_ollama_models(base_url: str = "http://localhost:11434") -> list[tuple[str, str]]:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=3)
        response.raise_for_status()
        models = []
        for item in response.json().get("models", []):
            name = item.get("name") or item.get("model")
            if name:
                details = item.get("details") or {}
                description = " · ".join(filter(None, [details.get("parameter_size"), details.get("quantization_level")]))
                models.append((name, description))
        return sorted(models)
    except (httpx.HTTPError, ValueError, TypeError):
        return []


def ollama_availability() -> OllamaAvailability:
    models = discover_ollama_models()
    if models:
        return OllamaAvailability("available", find_ollama_executable(), len(models))
    executable = find_ollama_executable()
    try:
        response = httpx.get("http://localhost:11434/api/tags", timeout=1.5)
        response.raise_for_status()
        return OllamaAvailability("no_models", executable)
    except httpx.HTTPError:
        return OllamaAvailability("not_running" if executable else "not_installed", executable)


def start_ollama(executable: str) -> None:
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if platform.system() == "Windows":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    subprocess.Popen([executable, "serve"], **options)
    for _ in range(10):
        time.sleep(0.5)
        if ollama_availability().state in {"available", "no_models"}:
            return


def choose_provider() -> str:
    while True:
        availability = ollama_availability()
        if availability.state == "available":
            ollama = Choice(f"1. Ollama [{availability.model_count} model(s) installed]", "ollama")
            action = None
        elif availability.state == "not_installed":
            ollama = Choice("1. Ollama [not installed]", "ollama", disabled="Ollama is not installed")
            action = Choice("   Install Ollama from the official website", "install")
        elif availability.state == "not_running":
            ollama = Choice("1. Ollama [not running]", "ollama", disabled="Ollama is stopped")
            action = Choice("   Start Ollama and recheck", "start")
        else:
            ollama = Choice("1. Ollama [no models installed]", "ollama", disabled="Install an Ollama model first")
            action = Choice("   Recheck installed Ollama models", "recheck")
        choices = [ollama]
        if action:
            choices.append(action)
        choices.extend([Choice("2. API — OpenAI, Claude, or compatible", "api"), Choice("3. HTTP endpoint", "http")])
        selected = select("Select the model provider", choices, "ollama" if availability.state == "available" else "api")
        if selected == "install":
            webbrowser.open("https://ollama.com/download")
            note("Complete the official Ollama installation, then return and recheck.")
            continue
        if selected == "start":
            if availability.executable:
                start_ollama(availability.executable)
            continue
        if selected == "recheck":
            note("Install a model with: ollama pull <model-name>")
            continue
        return selected


def configure_ollama(*, multiple: bool = True) -> tuple[list[dict], str]:
    base_url = "http://localhost:11434"
    models = discover_ollama_models(base_url)
    choices = [Choice(f"{name}  ({detail})" if detail else name, name) for name, detail in models]
    names = (
        checkbox("Select one or more installed Ollama models", choices)
        if multiple
        else [select("Select the Ollama model", choices)]
    )
    used: set[str] = set()
    return ([{
        "id": _model_id(name, used),
        "adapter": "ollama",
        "model": name,
        "base_url": base_url,
    } for name in names], "Ollama")


def discover_api_models(base_url: str, headers: dict[str, str]) -> list[str]:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=8)
        response.raise_for_status()
        return sorted({item.get("id") for item in response.json().get("data", []) if item.get("id")})
    except (httpx.HTTPError, ValueError, TypeError):
        return []


def configure_api(pending: dict[str, str], *, multiple: bool = True) -> tuple[list[dict], str]:
    api = select("Select the API", [
        Choice("1. OpenAI API", "openai"),
        Choice("2. Anthropic Claude API", "anthropic"),
        Choice("3. OpenAI-compatible API", "compatible"),
    ], "openai")
    if api == "openai":
        label, base_url, adapter, credential_id = "OpenAI", "https://api.openai.com/v1", "openai", "openai"
    elif api == "anthropic":
        label, base_url, adapter, credential_id = "Claude", "https://api.anthropic.com/v1", "anthropic", "anthropic"
    else:
        label, base_url, adapter = "OpenAI-compatible API", text("API base URL", "https://provider.example/v1"), "openai"
        credential_id = _credential_id(base_url)
    key = capture_credential(credential_id, label, pending)
    headers = (
        {"x-api-key": key, "anthropic-version": "2023-06-01"}
        if adapter == "anthropic"
        else {"Authorization": f"Bearer {key}"}
    )
    discovered = discover_api_models(base_url, headers)
    if "openrouter.ai" in base_url.lower():
        discovered = sorted(set(discovered) | {"openrouter/free"})
    if discovered:
        manual = "__manual_model__"
        choices = [Choice(name, name) for name in discovered]
        choices.append(Choice("Enter a model identifier manually", manual))
        if multiple:
            names = checkbox(f"Select one or more {label} models", choices)
            if manual in names:
                names = [name for name in names if name != manual]
                names.append(text(f"{label} model identifier"))
        else:
            selected = select(f"Select the {label} model", choices)
            names = [text(f"{label} model identifier")] if selected == manual else [selected]
    else:
        note("The API model list was unavailable. Enter one model identifier manually.")
        names = [text(f"{label} model identifier")]
    used: set[str] = set()
    return ([{
        "id": _model_id(name, used),
        "adapter": adapter,
        "model": name,
        "base_url": base_url,
        "credential_id": credential_id,
    } for name in names], label)


def configure_http(pending: dict[str, str]) -> tuple[list[dict], str]:
    endpoint = text("HTTP endpoint URL")
    auth = select("Authentication", [
        Choice("1. Bearer token", "bearer"),
        Choice("2. Custom authentication header", "header"),
        Choice("3. None", "none"),
    ], "bearer")
    authentication: dict[str, str] = {"type": auth}
    if auth != "none":
        credential_id = _credential_id(endpoint)
        capture_credential(credential_id, "HTTP endpoint", pending)
        authentication["credential_id"] = credential_id
        if auth == "header":
            authentication["header_name"] = text("Authentication header name", "X-API-Key")
    model = text("Model identifier")
    return ([{
        "id": _model_id(model, set()),
        "adapter": "custom_http",
        "model": model,
        "endpoint": endpoint,
        "authentication": authentication,
        "response_text_path": text("Response text JSON path", "response"),
    }], "HTTP endpoint")


def configure_models(provider: str, pending: dict[str, str], *, multiple: bool) -> tuple[list[dict], str]:
    if provider == "ollama":
        return configure_ollama(multiple=multiple)
    if provider == "api":
        return configure_api(pending, multiple=multiple)
    return configure_http(pending)


def configure_judge(pending: dict[str, str]) -> tuple[dict, str]:
    mode = select("Select result evaluation", [
        Choice("1. No judge — leave status empty", "none"),
        Choice("2. LLM-as-a-judge", "llm"),
    ], "none")
    if mode == "none":
        return {"mode": "none"}, "No judge"

    availability = ollama_availability()
    ollama_choice = (
        Choice("2. Local Ollama model", "ollama")
        if availability.state == "available"
        else Choice("2. Local Ollama model", "ollama", disabled=f"Ollama is {availability.state.replace('_', ' ')}")
    )
    source = select("Select the judge model", [
        Choice("1. Same as each inference model", "same"),
        ollama_choice,
        Choice("3. API — OpenAI, Claude, or compatible", "api"),
        Choice("4. HTTP endpoint", "http"),
    ], "same")
    if source == "same":
        return {"mode": "same_as_inference"}, "Same as each inference model"

    models, label = configure_models(source, pending, multiple=False)
    return {"mode": "model", "model": models[0]}, f"{label}: {models[0]['model']}"


def run_configuration_wizard(root: Path) -> tuple[dict, dict[str, str]]:
    questionary.print("\nRAGnarok", style="bold fg:#5f87ff")
    questionary.print(
        "Choose the model provider. RAGnarok will retrieve four chunks for each CSV prompt and start immediately.\n",
        style="fg:#808080",
    )
    while True:
        pending: dict[str, str] = {}
        provider = choose_provider()
        models, label = configure_models(provider, pending, multiple=True)
        judge, judge_label = configure_judge(pending)
        note("\nRun summary")
        note(f"Provider: {label}")
        note("Models: " + ", ".join(model["model"] for model in models))
        note(f"Judge: {judge_label}")
        note(f"Dataset: {root / 'dataset' / 'dataset.csv'}")
        note("RAG: all-MiniLM-L6-v2 · top 4 chunks")
        action = select("Ready", [Choice("Start", "start"), Choice("Go back", "back")], "start")
        if action == "back":
            continue
        return {"models": models, "judge": judge}, pending


def talk_configuration_wizard(root: Path) -> tuple[dict, dict[str, str]]:
    questionary.print("\nRAGnarok talk", style="bold fg:#5f87ff")
    questionary.print(
        "Choose one model and chat through the same fixed top-4 RAG pipeline.\n",
        style="fg:#808080",
    )
    while True:
        pending: dict[str, str] = {}
        provider = choose_provider()
        models, label = configure_models(provider, pending, multiple=False)
        model = models[0]
        note("\nChat summary")
        note(f"Provider: {label}")
        note(f"Model: {model['model']}")
        note(f"Knowledge base: {root / 'knowledge_base'}")
        note("RAG: all-MiniLM-L6-v2 · top 4 chunks")
        action = select("Ready", [Choice("Start chat", "start"), Choice("Go back", "back")], "start")
        if action == "back":
            continue
        return {"models": [model]}, pending
