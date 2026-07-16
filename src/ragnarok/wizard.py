from __future__ import annotations

import os
import re
from copy import deepcopy
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
    "pointer": "fg:#ffffff bg:#176b57 bold",
    "highlighted": "fg:#ffffff bg:#176b57 bold",
    "selected": "fg:#5fd7af bold",
    "instruction": "fg:#808080",
})

REMOTE_PRESETS = {
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1", "env": "OPENAI_API_KEY"},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "env": "OPENROUTER_API_KEY"},
    "groq": {"label": "Groq", "base_url": "https://api.groq.com/openai/v1", "env": "GROQ_API_KEY"},
    "together": {"label": "Together AI", "base_url": "https://api.together.xyz/v1", "env": "TOGETHER_API_KEY"},
}


class SetupCancelled(RuntimeError):
    pass


def _answer(question):
    value = question.ask()
    if value is None:
        raise SetupCancelled("setup cancelled")
    return value


def select(message: str, choices: list[Choice], default: Any = None):
    choices = [*choices, Choice("Exit setup without saving", value="__exit__")]
    value = _answer(questionary.select(
        message, choices=choices, default=default, style=STYLE, pointer="»",
        instruction="(use ↑/↓ and Enter)", use_arrow_keys=True, use_jk_keys=True,
    ))
    if value == "__exit__":
        raise SetupCancelled("setup cancelled")
    return value


def checkbox(message: str, choices: list[Choice]) -> list[Any]:
    options = [*choices, Choice("Exit setup without saving", value="__exit__")]
    values = _answer(questionary.checkbox(
        message, choices=options, style=STYLE, pointer="»",
        instruction="(↑/↓ move, Space selects, Enter confirms)",
    ))
    if "__exit__" in values:
        raise SetupCancelled("setup cancelled")
    if not values:
        note("Select at least one model.")
        return checkbox(message, choices)
    return values


def text(message: str, default: str = "", required: bool = True) -> str:
    validator = (lambda value: True if value.strip() else "A value is required") if required else None
    return _answer(questionary.text(message, default=default, validate=validator, style=STYLE)).strip()


def password(message: str) -> str:
    return _answer(questionary.password(message, validate=lambda value: True if value else "A value is required", style=STYLE))


def note(message: str) -> None:
    questionary.print(message, style="fg:#808080")


def _human_size(value: int | None) -> str:
    if not value:
        return "unknown size"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _credential_id(label: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    return value[:80] or "remote-api"


def discover_ollama_models(base_url: str) -> list[tuple[str, str]]:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/tags", timeout=4)
        response.raise_for_status()
        models = []
        for item in response.json().get("models", []):
            name = item.get("name") or item.get("model")
            if not name:
                continue
            details = item.get("details") or {}
            description = " · ".join(filter(None, [details.get("parameter_size"), details.get("quantization_level"), _human_size(item.get("size"))]))
            models.append((name, description))
        return sorted(models)
    except (httpx.HTTPError, ValueError, TypeError):
        return []


def discover_openai_models(base_url: str, api_key_env: str | None = None, api_key: str | None = None) -> list[str]:
    headers = {}
    if api_key or api_key_env:
        token = api_key or os.getenv(api_key_env or "")
        if not token:
            return []
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/models", headers=headers, timeout=6)
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data", payload.get("models", [])) if isinstance(payload, dict) else []
        names = [item.get("id") or item.get("name") for item in items if isinstance(item, dict)]
        return sorted({name for name in names if name})
    except (httpx.HTTPError, ValueError, TypeError):
        return []


def choose_ollama_model(base_url: str, role: str, default: str = "llama3.1:8b") -> str:
    models = discover_ollama_models(base_url)
    if models:
        choices = [Choice(f"{index}. {name}  ({detail})", value=name) for index, (name, detail) in enumerate(models, 1)]
        choices.append(Choice(f"{len(choices)+1}. Enter another model name", value="__manual__"))
        selected = select(f"Select the installed Ollama {role} model", choices)
        return text(f"Enter the Ollama {role} model name", default) if selected == "__manual__" else selected
    note(f"Could not discover installed Ollama models at {base_url}. Ensure Ollama is running; no model will be downloaded automatically.")
    return text(f"Enter the Ollama {role} model name", default)


def choose_ollama_models(base_url: str) -> list[str]:
    models = discover_ollama_models(base_url)
    if not models:
        note(f"Could not discover installed Ollama models at {base_url}. Ensure Ollama is running; no model will be downloaded automatically.")
        return [text("Enter the Ollama inference model name", "llama3.1:8b")]
    choices = [Choice(f"{name}  ({detail})", value=name) for name, detail in models]
    choices.append(Choice("Enter another model name", value="__manual__"))
    selected = checkbox("Select one or more installed Ollama inference models", choices)
    return [text("Enter the Ollama inference model name", "llama3.1:8b") if value == "__manual__" else value for value in selected]


def choose_openai_model(base_url: str, api_key_env: str | None, role: str, default: str = "model-name", api_key: str | None = None) -> str:
    models = discover_openai_models(base_url, api_key_env, api_key)
    if models:
        choices = [Choice(f"{index}. {name}", value=name) for index, name in enumerate(models, 1)]
        choices.append(Choice(f"{len(choices)+1}. Enter another model name", value="__manual__"))
        selected = select(f"Select the available {role} model", choices)
        return text(f"Enter the {role} model name", default) if selected == "__manual__" else selected
    if api_key_env and not os.getenv(api_key_env):
        note(f"No environment variable named {api_key_env} is currently set, so remote model discovery is unavailable.")
        note(f"In CMD, set it before setup with: set {api_key_env}=<your-api-key>")
        note("Do not paste the API key into the environment-variable name field. Secrets are never stored in YAML.")
    else:
        note("The provider did not expose a model list; enter the model identifier manually.")
    return text(f"Enter the {role} model identifier", default)


def choose_openai_models(base_url: str, api_key_env: str | None = None, api_key: str | None = None) -> list[str]:
    models = discover_openai_models(base_url, api_key_env, api_key)
    if not models:
        if api_key_env and not os.getenv(api_key_env):
            note(f"{api_key_env} is not set, so remote model discovery is unavailable. The key itself will never be stored in YAML.")
        else:
            note("The provider did not expose a model list; enter the model identifier manually.")
    return [text("Enter the inference model identifier", "model-name")]
    choices = [Choice(name, value=name) for name in models]
    choices.append(Choice("Enter another model name", value="__manual__"))
    selected = checkbox("Select one or more available inference models", choices)
    return [text("Enter the inference model identifier", "model-name") if value == "__manual__" else value for value in selected]


def capture_credential(credential_id: str, label: str, pending: dict[str, str]) -> str:
    try:
        existing = get_stored_credential(credential_id)
    except CredentialError as exc:
        note(str(exc))
        existing = None
    if existing:
        action = select(f"A saved {label} credential exists", [
            Choice("Use the saved credential", "use"),
            Choice("Replace it with a new API key", "replace"),
        ], "use")
        if action == "use":
            return existing
    secret = password(f"Enter the {label} API key (input is hidden)")
    pending[credential_id] = secret
    return secret


def _model_id(name: str, used: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "model"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _custom_http(role: str, pending: dict[str, str] | None = None) -> dict:
    pending = pending if pending is not None else {}
    endpoint = text(f"{role.title()} HTTP endpoint")
    method = select("HTTP method", [Choice("1. POST (recommended)", "POST"), Choice("2. PUT", "PUT")], "POST")
    auth = select("Authentication", [Choice("1. Bearer token from an environment variable", "bearer"), Choice("2. Custom header from an environment variable", "header"), Choice("3. None", "none")], "bearer")
    authentication: dict = {"type": auth}
    if auth != "none":
        credential_id = _credential_id(f"custom-{endpoint}")
        capture_credential(credential_id, "custom endpoint", pending)
        authentication["credential_id"] = credential_id
        authentication["token_env"] = "RAGNAROK_CUSTOM_HTTP_KEY"
    if auth == "header":
        authentication["header_name"] = text("Authentication header name", "X-API-Key")
    return {
        "id": f"{role}_model", "adapter": "custom_http", "model": text(f"{role.title()} model identifier", "private-model"),
        "endpoint": endpoint, "method": method, "authentication": authentication,
        "response_text_path": text("Response text JSON path", "response"), "temperature": 0,
    }


def configure_local(role: str, pending: dict[str, str] | None = None) -> tuple[dict, str]:
    provider = select(
        f"Select the local {role} provider",
        [
            Choice("1. Ollama (recommended)", "ollama"),
            Choice("2. LM Studio (OpenAI-compatible)", "lm_studio"),
            Choice("3. vLLM (OpenAI-compatible)", "vllm"),
            Choice("4. Other OpenAI-compatible server", "openai_compatible"),
            Choice("5. Custom HTTP endpoint", "custom_http"),
        ], "ollama",
    )
    if provider == "ollama":
        base_url = text("Ollama base URL", "http://localhost:11434")
        model = choose_ollama_model(base_url, role)
        return {"id": f"{role}_model", "adapter": "ollama", "model": model, "base_url": base_url, "temperature": 0}, "local / Ollama"
    if provider == "custom_http":
        return _custom_http(role, pending), "local / custom HTTP"
    defaults = {"lm_studio": "http://localhost:1234/v1", "vllm": "http://localhost:8000/v1", "openai_compatible": "http://localhost:8000/v1"}
    base_url = text("OpenAI-compatible base URL", defaults[provider])
    model = choose_openai_model(base_url, None, role)
    return {"id": f"{role}_model", "adapter": "openai_compatible", "model": model, "base_url": base_url, "temperature": 0}, f"local / {provider.replace('_', ' ')}"


def configure_local_inference(pending: dict[str, str] | None = None) -> tuple[list[dict], str]:
    provider = select("Select the local inference provider", [
        Choice("1. Ollama (recommended)", "ollama"),
        Choice("2. LM Studio (OpenAI-compatible)", "lm_studio"),
        Choice("3. vLLM (OpenAI-compatible)", "vllm"),
        Choice("4. Other OpenAI-compatible server", "openai_compatible"),
        Choice("5. Custom HTTP endpoint", "custom_http"),
    ], "ollama")
    if provider == "custom_http":
        model = _custom_http("inference", pending)
        return [model], "local / custom HTTP"
    if provider == "ollama":
        base_url = text("Ollama base URL", "http://localhost:11434")
        names = choose_ollama_models(base_url)
        template = {"adapter": "ollama", "base_url": base_url, "temperature": 0}
        label = "local / Ollama"
    else:
        defaults = {"lm_studio": "http://localhost:1234/v1", "vllm": "http://localhost:8000/v1", "openai_compatible": "http://localhost:8000/v1"}
        base_url = text("OpenAI-compatible base URL", defaults[provider])
        names = choose_openai_models(base_url, None)
        template = {"adapter": "openai_compatible", "base_url": base_url, "temperature": 0}
        label = f"local / {provider.replace('_', ' ')}"
    used: set[str] = set()
    return [{"id": _model_id(name, used), "model": name, **template} for name in names], label


def configure_remote(role: str, pending: dict[str, str] | None = None) -> tuple[dict, str]:
    pending = pending if pending is not None else {}
    provider = select(
        f"Select the remote {role} provider",
        [
            Choice("1. OpenAI", "openai"), Choice("2. OpenRouter", "openrouter"),
            Choice("3. Groq", "groq"), Choice("4. Together AI", "together"),
            Choice("5. Generic OpenAI-compatible API", "generic"),
            Choice("6. Custom HTTP endpoint", "custom_http"),
        ], "openai",
    )
    if provider == "custom_http":
        return _custom_http(role, pending), "remote / custom HTTP"
    if provider == "generic":
        label = "OpenAI-compatible API"; base_url = text("API base URL", "https://provider.example/v1"); default_env = "REMOTE_MODEL_API_KEY"
    else:
        preset = REMOTE_PRESETS[provider]; label = preset["label"]; base_url = preset["base_url"]; default_env = preset["env"]
    credential_id = _credential_id(provider if provider != "generic" else base_url)
    api_key = capture_credential(credential_id, label, pending)
    model = choose_openai_model(base_url, None, role, api_key=api_key)
    return {"id": f"{role}_model", "adapter": "openai_compatible", "model": model, "base_url": base_url, "credential_id": credential_id, "api_key_env": default_env, "temperature": 0}, f"remote / {label}"


def configure_remote_inference(pending: dict[str, str] | None = None) -> tuple[list[dict], str]:
    pending = pending if pending is not None else {}
    provider = select("Select the remote inference provider", [
        Choice("1. OpenAI", "openai"), Choice("2. OpenRouter", "openrouter"),
        Choice("3. Groq", "groq"), Choice("4. Together AI", "together"),
        Choice("5. Generic OpenAI-compatible API", "generic"),
        Choice("6. Custom HTTP endpoint", "custom_http"),
    ], "openai")
    if provider == "custom_http":
        return [_custom_http("inference", pending)], "remote / custom HTTP"
    if provider == "generic":
        label = "OpenAI-compatible API"; base_url = text("API base URL", "https://provider.example/v1"); default_env = "REMOTE_MODEL_API_KEY"
    else:
        preset = REMOTE_PRESETS[provider]; label = preset["label"]; base_url = preset["base_url"]; default_env = preset["env"]
    credential_id = _credential_id(provider if provider != "generic" else base_url)
    api_key = capture_credential(credential_id, label, pending)
    names = choose_openai_models(base_url, None, api_key)
    used: set[str] = set()
    models = [{"id": _model_id(name, used), "adapter": "openai_compatible", "model": name, "base_url": base_url, "credential_id": credential_id, "api_key_env": default_env, "temperature": 0} for name in names]
    return models, f"remote / {label}"


def choose_same_connection_model(inference: dict, pending: dict[str, str] | None = None) -> dict:
    judge = deepcopy(inference); judge["id"] = "judge_model"
    adapter = inference["adapter"]
    choice = select("Judge model", [Choice(f"1. Use the inference model ({inference['model']})", "same"), Choice("2. Select a different model", "different")], "same")
    if choice == "different":
        if adapter == "ollama":
            judge["model"] = choose_ollama_model(inference["base_url"], "judge", inference["model"])
        elif adapter == "openai_compatible":
            credential_id = inference.get("credential_id")
            api_key = (pending or {}).get(credential_id) if credential_id else None
            if not api_key and credential_id:
                api_key = get_stored_credential(credential_id)
            judge["model"] = choose_openai_model(inference["base_url"], inference.get("api_key_env"), "judge", inference["model"], api_key)
        else:
            judge["model"] = text("Judge model identifier", inference["model"])
    return judge


def configure_embedding(inference_location: str) -> dict | None:
    if inference_location == "mock":
        return {"embedding_backend": "mock", "embedding_model": "mock-hash-v1", "cache_dir": ".ragnarok/mock-cache"}
    backend = select("Select the local RAG embedding profile", [
        Choice("1. Fast semantic embeddings — all-MiniLM-L6-v2 (recommended)", "mini"),
        Choice("2. Retrieval-focused — multi-qa-MiniLM-L6-cos-v1", "multiqa"),
        Choice("3. Enter another Sentence Transformers model", "custom"),
        Choice("4. Deterministic mock embeddings (testing only)", "mock"),
    ], "mini")
    if backend == "mock":
        return {"embedding_backend": "mock", "embedding_model": "mock-hash-v1", "cache_dir": ".ragnarok/mock-cache"}
    names = {"mini": "sentence-transformers/all-MiniLM-L6-v2", "multiqa": "sentence-transformers/multi-qa-MiniLM-L6-cos-v1"}
    model = text("Sentence Transformers model", "sentence-transformers/all-MiniLM-L6-v2") if backend == "custom" else names[backend]
    return {"embedding_backend": "sentence_transformers", "embedding_model": model, "cache_dir": ".ragnarok/cache"}


def run_setup_wizard(root: Path) -> tuple[dict, list[str], list[str], dict[str, str]]:
    questionary.print("\nRAGnarok model setup", style="bold fg:#5f87ff")
    questionary.print("Nothing is written until you choose Save on the final screen.\n", style="fg:#808080")
    while True:
        pending_credentials: dict[str, str] = {}
        location = select("Where do the inference models run?", [Choice("1. Local — Ollama, LM Studio, vLLM, or local HTTP", "local"), Choice("2. Remote — hosted API", "remote"), Choice("3. Mock — offline framework test", "mock")], "local")
        if location == "local":
            inference_models, inference_label = configure_local_inference(pending_credentials)
        elif location == "remote":
            inference_models, inference_label = configure_remote_inference(pending_credentials)
        else:
            inference_models = [{"id": "safe_mock", "adapter": "mock", "model": "safe-mock", "temperature": 0}]; inference_label = "mock"

        judge_location = select("Where does the judge run?", [Choice("1. No judge — responses and deterministic metrics only", "disabled"), Choice("2. Same connection as the first inference model", "same"), Choice("3. Local", "local"), Choice("4. Remote", "remote"), Choice("5. Mock — testing only", "mock")], "disabled")
        judge: dict = {"enabled": False}; judge_label = "disabled"
        if judge_location == "same":
            judge_entry = choose_same_connection_model(inference_models[0], pending_credentials); judge = {"enabled": True, "confidence_threshold": 0.7, "model": judge_entry}; judge_label = f"same connection / {judge_entry['model']}"
        elif judge_location == "local":
            judge_entry, provider_label = configure_local("judge", pending_credentials); judge = {"enabled": True, "confidence_threshold": 0.7, "model": judge_entry}; judge_label = f"{provider_label} / {judge_entry['model']}"
        elif judge_location == "remote":
            judge_entry, provider_label = configure_remote("judge", pending_credentials); judge = {"enabled": True, "confidence_threshold": 0.7, "model": judge_entry}; judge_label = f"{provider_label} / {judge_entry['model']}"
        elif judge_location == "mock":
            judge_entry = {"id": "judge_model", "adapter": "mock", "model": "judge-mock", "temperature": 0}; judge = {"enabled": True, "confidence_threshold": 0.7, "model": judge_entry}; judge_label = "mock / judge-mock"

        configuration: dict = {"models": inference_models, "judge": judge}
        configuration["rag"] = configure_embedding(location)
        note("\nConfiguration summary")
        note("Models: " + ", ".join(model["model"] for model in inference_models))
        note("Judge: " + judge_label)
        action = select("Finish setup", [Choice("Save configuration", "save"), Choice("Go back and choose again", "back")], "save")
        if action == "back":
            continue
        required_env = []
        for model in [*inference_models, judge.get("model", {})]:
            if model.get("api_key_env") and not model.get("credential_id"):
                required_env.append(model["api_key_env"])
            auth = model.get("authentication") or {}
            if auth.get("token_env") and not auth.get("credential_id"):
                required_env.append(auth["token_env"])
        labels = [f"{inference_label} / " + ", ".join(model["model"] for model in inference_models), judge_label]
        return configuration, labels, sorted(set(required_env)), pending_credentials
