from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
import webbrowser
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import questionary
from prompt_toolkit.styles import Style
from questionary import Choice

from .benchmarks import available_benchmarks
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

AUTOCOMPLETE_STYLE = Style.from_dict({
    "qmark": "fg:#00afff bold",
    "question": "fg:#ffffff bold",
    "answer": "fg:#ffffff bg:#1c1c1c bold",
    "selected": "fg:#000000 bg:#5fd7ff bold",
    "completion-menu.completion": "fg:#ffffff bg:#1c1c1c",
    "completion-menu.completion.current": "fg:#000000 bg:#5fd7ff bold",
    "completion-menu.meta.completion": "fg:#d0d0d0 bg:#303030",
    "completion-menu.meta.completion.current": "fg:#000000 bg:#87d7ff bold",
    "scrollbar.background": "bg:#303030",
    "scrollbar.button": "bg:#87d7ff",
    "validation-toolbar": "fg:#ffffff bg:#af0000 bold",
})

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


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
        validate=lambda selected: True if "__exit__" in selected or selected else "Select at least one item with Space.",
    ))
    if "__exit__" in values:
        raise RunCancelled("run cancelled")
    return values


def autocomplete(message: str, choices: list[str]) -> str:
    """Select one exact value from a searchable list."""

    exit_value = "Exit"
    available = [*choices, exit_value]
    value = _answer(questionary.autocomplete(
        message,
        choices=available,
        style=AUTOCOMPLETE_STYLE,
        match_middle=True,
        ignore_case=True,
        validate=lambda selected: (
            True if selected in available else "Select an item from the downloaded model list"
        ),
    ))
    if value == exit_value:
        raise RunCancelled("run cancelled")
    return value


def text(message: str, default: str = "") -> str:
    return _answer(questionary.text(
        message,
        default=default,
        validate=lambda value: True if value.strip() else "A value is required",
        style=STYLE,
    )).strip()


def integer(message: str, default: int) -> int:
    value = _answer(questionary.text(
        message,
        default=str(default),
        validate=lambda raw: True if raw.strip().isdigit() and int(raw) > 0 else "Enter a positive integer",
        style=STYLE,
    ))
    return int(value)


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
    choices: list[Choice] = []
    previous_group: tuple[str, str] | None = None
    grouped = sorted(models, key=lambda item: (*_ollama_group_key(item[0], item[1]), item[0]))
    for name, detail in grouped:
        group = _ollama_group_key(name, detail)
        if group != previous_group:
            if choices:
                choices.append(Choice("────────────────────────────────────────", disabled="separator"))
            choices.append(Choice(f"{group[0]} · {group[1]}", disabled="model size"))
            previous_group = group
        choices.append(Choice(f"  {name}  ({detail})" if detail else f"  {name}", name))
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


def _ollama_group_key(name: str, detail: str) -> tuple[str, str]:
    base = name.split(":", 1)[0]
    family = re.sub(r"[-_.]+", " ", base).strip().title()
    size_match = re.search(r"\b(\d+(?:\.\d+)?)\s*[bB]\b", detail) or re.search(
        r"(?:^|[:_-])(\d+(?:\.\d+)?)\s*[bB](?:$|[-_])", name
    )
    size = f"{size_match.group(1)}B" if size_match else "Unspecified size"
    return family, size


def discover_api_models(base_url: str, headers: dict[str, str]) -> list[str]:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=8)
        response.raise_for_status()
        return sorted({item.get("id") for item in response.json().get("data", []) if item.get("id")})
    except (httpx.HTTPError, ValueError, TypeError):
        return []


def _is_openrouter_url(value: str) -> bool:
    try:
        return httpx.URL(value).host in {"openrouter.ai", "www.openrouter.ai"}
    except (TypeError, ValueError):
        return False


def configure_openrouter(pending: dict[str, str], *, multiple: bool) -> tuple[list[dict], str]:
    """Configure OpenRouter through its OpenAI-compatible API and verified catalog."""

    label = "OpenRouter"
    credential_id = _credential_id(OPENROUTER_BASE_URL)
    key = capture_credential(credential_id, label, pending)
    discovered = discover_api_models(
        OPENROUTER_BASE_URL,
        {"Authorization": f"Bearer {key}"},
    )
    if not discovered:
        note(
            "OpenRouter could not authenticate or download its model catalog. "
            "Check the API key and internet connection, then retry."
        )
        raise RunCancelled("OpenRouter authentication or model catalog unavailable")
    names = select_api_models(label, discovered, multiple=multiple, searchable=True)
    used: set[str] = set()
    return ([{
        "id": _model_id(name, used),
        "adapter": "openai",
        "model": name,
        "base_url": OPENROUTER_BASE_URL,
        "credential_id": credential_id,
        "reasoning_enabled": False,
    } for name in names], label)


def select_api_models(label: str, discovered: list[str], *, multiple: bool, searchable: bool) -> list[str]:
    """Choose catalog models without allowing unverified identifiers for hosted catalogs."""

    if searchable:
        selected: list[str] = []
        remaining = list(discovered)
        while remaining:
            model = autocomplete(f"Search and select the {label} model", remaining)
            selected.append(model)
            remaining.remove(model)
            if not multiple or not remaining:
                break
            action = select(
                "Model selected: " + model,
                [Choice("Continue with these models", "done"), Choice("Add another model", "add")],
                "done",
            )
            if action == "done":
                break
        return selected

    manual = "__manual_model__"
    choices = [Choice(name, name) for name in discovered]
    choices.append(Choice("Enter a model identifier manually", manual))
    if multiple:
        names = checkbox(f"Select one or more {label} models", choices)
        if manual in names:
            names = [name for name in names if name != manual]
            names.append(text(f"{label} model identifier"))
        return names
    selected = select(f"Select the {label} model", choices)
    return [text(f"{label} model identifier")] if selected == manual else [selected]


def configure_api(pending: dict[str, str], *, multiple: bool = True) -> tuple[list[dict], str]:
    api = select("Select the API", [
        Choice("1. OpenAI API", "openai"),
        Choice("2. Anthropic Claude API", "anthropic"),
        Choice("3. OpenRouter", "openrouter"),
        Choice("4. Other OpenAI-compatible API", "compatible"),
    ], "openai")
    if api == "openai":
        label, base_url, adapter, credential_id = "OpenAI", "https://api.openai.com/v1", "openai", "openai"
    elif api == "anthropic":
        label, base_url, adapter, credential_id = "Claude", "https://api.anthropic.com/v1", "anthropic", "anthropic"
    elif api == "openrouter":
        return configure_openrouter(pending, multiple=multiple)
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
    if discovered:
        names = select_api_models(label, discovered, multiple=multiple, searchable=False)
    else:
        note("The API model list was unavailable. Enter one model identifier manually.")
        names = [text(f"{label} model identifier")]
    used: set[str] = set()
    models = []
    for name in names:
        model = {
            "id": _model_id(name, used),
            "adapter": adapter,
            "model": name,
            "base_url": base_url,
            "credential_id": credential_id,
        }
        models.append(model)
    return models, label


def configure_http(pending: dict[str, str], *, multiple: bool = False) -> tuple[list[dict], str]:
    endpoint = text("HTTP endpoint URL")
    if _is_openrouter_url(endpoint):
        note("OpenRouter detected. Using its OpenAI-compatible provider and verified model catalog.")
        return configure_openrouter(pending, multiple=multiple)
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
    return configure_http(pending, multiple=multiple)


def _profile_path(root: Path) -> Path:
    return root / ".ragnarok" / "model_profiles.json"


def _load_model_profiles(root: Path) -> dict[str, dict]:
    path = _profile_path(root)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {str(name): dict(config) for name, config in payload.items() if isinstance(config, dict)}
    except (OSError, ValueError, TypeError):
        return {}


def _save_model_profiles(root: Path, profiles: dict[str, dict]) -> None:
    path = _profile_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profiles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def configure_named_role_model(
    root: Path,
    pending: dict[str, str],
    *,
    role: str,
) -> dict:
    """Always request a role choice while allowing explicit reuse of named profiles."""

    profiles = _load_model_profiles(root)
    choices = [
        Choice(
            f"Use saved profile: {name} — {profiles[name].get('model', 'unknown model')}",
            ("saved", name),
        )
        for name in sorted(profiles)
    ]
    choices.append(Choice(f"Configure a new {role} model", ("new", None)))
    action, name = select(f"Select the {role} model for this run", choices)
    if action == "saved":
        model = dict(profiles[name])
        credential_id = model.get("credential_id")
        if credential_id and credential_id not in pending:
            capture_credential(credential_id, f"{role} profile '{name}'", pending)
        return model
    provider = choose_provider()
    configured, _ = configure_models(provider, pending, multiple=False)
    model = configured[0]
    save = select(
        f"Save this {role} model as a named profile?",
        [Choice("Save named profile", "save"), Choice("Use once", "once")],
        "save",
    )
    if save == "save":
        profile_name = text(f"Name for this {role} profile")
        profiles[profile_name] = model
        _save_model_profiles(root, profiles)
        note(f"Saved model profile: {profile_name}")
    return model


def run_configuration_wizard(root: Path) -> tuple[dict, dict[str, str]]:
    questionary.print("\nRAGnarok", style="bold fg:#5f87ff")
    questionary.print(
        "Run an unmodified upstream benchmark through the shared model gateway.\n",
        style="fg:#808080",
    )
    while True:
        pending: dict[str, str] = {}
        adapters = available_benchmarks()
        by_id = {adapter.info.id: adapter for adapter in adapters}
        benchmark_choices: list[Choice] = []
        tracks = (
            ("Direct", ("spikee",)),
            ("Indirect (classic RAG; MPIB also includes direct cases)", ("poisonedrag", "mpib")),
            ("Agentic", ("agentdojo",)),
        )
        for track, benchmark_ids in tracks:
            if benchmark_choices:
                benchmark_choices.append(Choice("────────────────────────────────────────", disabled="separator"))
            benchmark_choices.append(Choice(track, disabled="evaluation track"))
            benchmark_choices.extend(
                Choice(f"  {by_id[benchmark_id].info.name}", benchmark_id, checked=True)
                for benchmark_id in benchmark_ids if benchmark_id in by_id
            )
        benchmark_ids = checkbox(
            "Select one or more benchmarks",
            benchmark_choices,
        )
        suite_profile = select(
            "Select the evaluation size for all selected benchmarks",
            [
                Choice("Light - fastest thesis iteration", "light"),
                Choice("Medium - balanced thesis iteration", "medium"),
                Choice("Full - complete declared profile", "full"),
            ],
            "medium",
        )
        selections = []
        selected_adapters = []
        for benchmark_id in benchmark_ids:
            benchmark = next(adapter for adapter in adapters if adapter.info.id == benchmark_id)
            selected_adapters.append(benchmark)
            note(f"\n{benchmark.info.name} configuration")
            options: dict[str, object] = {}
            for spec in benchmark.option_specs():
                if spec.key == "profile" and any(choice.value == suite_profile for choice in spec.choices):
                    options[spec.key] = suite_profile
                elif spec.kind == "select":
                    options[spec.key] = select(spec.label, [Choice(choice.label, choice.value) for choice in spec.choices], spec.default)
                else:
                    options[spec.key] = integer(spec.label, int(spec.default))
            selections.append({"id": benchmark.info.id, "options": benchmark.validate_options(options)})

        provider = choose_provider()
        models, label = configure_models(provider, pending, multiple=True)
        for benchmark, selection in zip(selected_adapters, selections):
            if benchmark.info.requires_judge:
                note(f"\n{benchmark.info.name} Judge configuration")
                selection["judge"] = configure_named_role_model(root, pending, role="Judge")
            if benchmark.info.requires_attacker:
                note(f"\n{benchmark.info.name} attacker configuration")
                selection["attacker"] = configure_named_role_model(root, pending, role="attacker")
        installation_problems = [(benchmark.info.name, benchmark.validate_installation()) for benchmark in selected_adapters]
        subject_calls = sum(benchmark.estimate_model_calls(selection["options"]) for benchmark, selection in zip(selected_adapters, selections)) * len(models)
        judge_calls = sum(benchmark.estimate_judge_calls(selection["options"]) for benchmark, selection in zip(selected_adapters, selections)) * len(models)
        attacker_calls = sum(benchmark.estimate_attacker_calls(selection["options"]) for benchmark, selection in zip(selected_adapters, selections)) * len(models)
        note("\nRun summary")
        note("Benchmarks: " + ", ".join(benchmark.info.name for benchmark in selected_adapters))
        note(f"Provider: {label}")
        note("Models: " + ", ".join(model["model"] for model in models))
        for selection in selections:
            note(f"{selection['id']} configuration: {selection['options']}")
            if selection.get("judge"):
                judge = selection["judge"]
                note(f"{selection['id']} Judge: {judge['model']} via {judge.get('base_url') or judge['adapter']}")
            if selection.get("attacker"):
                attacker = selection["attacker"]
                note(f"{selection['id']} attacker: {attacker['model']} via {attacker.get('base_url') or attacker['adapter']}")
        note(f"Estimated maximum subject calls: {subject_calls}")
        if judge_calls:
            note(f"Estimated Judge calls: {judge_calls}")
        if attacker_calls:
            note(f"Estimated attacker calls (minimum): {attacker_calls}")
        note("Evaluation: official or pinned paper-specified benchmark evaluator")
        if any(problems for _, problems in installation_problems):
            note("Setup required before execution:")
            for name, problems in installation_problems:
                for problem in problems:
                    note(f"  - {name}: {problem}")
        action = select("Ready", [Choice("Start", "start"), Choice("Go back", "back")], "start")
        if action == "back":
            continue
        return {"benchmarks": selections, "models": models}, pending
