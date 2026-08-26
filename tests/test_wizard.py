from __future__ import annotations

import pytest
import questionary

from ragnarok import wizard


def test_autocomplete_uses_only_supported_questionary_arguments(monkeypatch):
    captured = {}

    class FakeQuestion:
        def ask(self):
            return "model/a"

    def fake_autocomplete(*args, **kwargs):
        captured.update(kwargs)
        return FakeQuestion()

    monkeypatch.setattr(questionary, "autocomplete", fake_autocomplete)

    assert wizard.autocomplete("Choose", ["model/a"]) == "model/a"
    assert "instruction" not in captured
    assert captured["style"] is wizard.AUTOCOMPLETE_STYLE


def test_openrouter_judge_is_selected_from_downloaded_catalog(monkeypatch):
    monkeypatch.setattr(wizard, "select", lambda *_args, **_kwargs: "openrouter")
    monkeypatch.setattr(wizard, "capture_credential", lambda *_args, **_kwargs: "secret")
    monkeypatch.setattr(
        wizard,
        "discover_api_models",
        lambda *_args, **_kwargs: ["deepseek/deepseek-v4-flash", "qwen/qwen3.5-flash"],
    )
    monkeypatch.setattr(
        wizard,
        "autocomplete",
        lambda _message, choices: "qwen/qwen3.5-flash" if "qwen/qwen3.5-flash" in choices else choices[0],
    )

    models, label = wizard.configure_api({}, multiple=False)

    assert label == "OpenRouter"
    assert models == [{
        "id": "qwen_qwen3_5_flash",
        "adapter": "openai",
        "model": "qwen/qwen3.5-flash",
        "base_url": "https://openrouter.ai/api/v1",
        "credential_id": "https-openrouter-ai-api-v1",
        "reasoning_enabled": False,
    }]


def test_openrouter_does_not_allow_manual_model_when_catalog_is_unavailable(monkeypatch):
    monkeypatch.setattr(wizard, "select", lambda *_args, **_kwargs: "openrouter")
    monkeypatch.setattr(wizard, "capture_credential", lambda *_args, **_kwargs: "secret")
    monkeypatch.setattr(wizard, "discover_api_models", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(wizard, "note", lambda *_args, **_kwargs: None)

    with pytest.raises(wizard.RunCancelled, match="catalog unavailable"):
        wizard.configure_api({}, multiple=False)


def test_http_openrouter_url_uses_openrouter_provider(monkeypatch):
    monkeypatch.setattr(wizard, "text", lambda *_args, **_kwargs: "https://openrouter.ai/api/v1")
    monkeypatch.setattr(
        wizard,
        "configure_openrouter",
        lambda pending, multiple: ([{"adapter": "openai", "model": "model"}], "OpenRouter"),
    )
    monkeypatch.setattr(wizard, "note", lambda *_args, **_kwargs: None)

    models, label = wizard.configure_http({}, multiple=True)

    assert label == "OpenRouter"
    assert models == [{"adapter": "openai", "model": "model"}]


def test_searchable_catalog_can_select_multiple_models(monkeypatch):
    answers = iter(["model/a", "model/b"])
    actions = iter(["add", "done"])
    monkeypatch.setattr(wizard, "autocomplete", lambda *_args, **_kwargs: next(answers))
    monkeypatch.setattr(wizard, "select", lambda *_args, **_kwargs: next(actions))

    selected = wizard.select_api_models(
        "OpenRouter",
        ["model/a", "model/b", "model/c"],
        multiple=True,
        searchable=True,
    )

    assert selected == ["model/a", "model/b"]


def test_saved_role_profile_always_allows_credential_replacement(monkeypatch, tmp_path):
    profiles = {
        "openrouter_judge": {
            "id": "judge",
            "adapter": "openai",
            "model": "deepseek/deepseek-v4-flash",
            "base_url": "https://openrouter.ai/api/v1",
            "credential_id": "https-openrouter-ai-api-v1",
        }
    }
    (tmp_path / ".ragnarok").mkdir()
    (tmp_path / ".ragnarok" / "model_profiles.json").write_text(
        __import__("json").dumps(profiles), encoding="utf-8"
    )
    monkeypatch.setattr(wizard, "select", lambda *_args, **_kwargs: ("saved", "openrouter_judge"))
    captured = []
    monkeypatch.setattr(
        wizard,
        "capture_credential",
        lambda credential_id, label, pending: captured.append((credential_id, label)) or "new-key",
    )

    model = wizard.configure_named_role_model(tmp_path, {}, role="Judge")

    assert model["model"] == "deepseek/deepseek-v4-flash"
    assert captured == [("https-openrouter-ai-api-v1", "Judge profile 'openrouter_judge'")]
