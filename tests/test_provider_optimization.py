import httpx
import pytest
from pydantic import ValidationError

from ragnarok.config import ModelConfig, RuntimeConfig
from ragnarok.models.ollama import OllamaProvider
from ragnarok.schemas import ChatMessage, ProviderRequest
from ragnarok.models.openai_compatible import OpenAICompatibleProvider
from ragnarok.models.custom_http import CustomHttpProvider


def test_generation_limits_reject_unbounded_or_excessive_requests():
    with pytest.raises(ValidationError):
        ProviderRequest(
            conversation_messages=[ChatMessage(role="user", content="prompt")],
            model="model",
            max_output_tokens=0,
        )
    with pytest.raises(ValidationError):
        ProviderRequest(
            conversation_messages=[ChatMessage(role="user", content="prompt")],
            model="model",
            max_output_tokens=4097,
        )


def test_ollama_reuses_connection_warms_up_records_processor_and_keeps_model_loaded():
    paths = []
    generate_payloads = []
    chat_payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={
                "models": [{
                    "name": "model",
                    "size": 1_000,
                    "size_vram": 1_000,
                    "expires_at": "later",
                }]
            })
        if request.url.path == "/api/generate":
            generate_payloads.append(__import__("json").loads(request.content))
            return httpx.Response(200, json={"total_duration": 10, "load_duration": 5})
        chat_payloads.append(__import__("json").loads(request.content))
        return httpx.Response(200, json={
            "message": {"content": "response"},
            "prompt_eval_count": 20,
            "eval_count": 3,
            "total_duration": 100,
            "load_duration": 4,
            "prompt_eval_duration": 60,
            "eval_duration": 30,
        })

    config = ModelConfig(id="model", adapter="ollama", model="model", base_url="http://test")
    provider = OllamaProvider(config, RuntimeConfig(retries=0))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider._client = client
    request = ProviderRequest(
        conversation_messages=[ChatMessage(role="user", content="prompt")],
        model="model",
        temperature=0.1,
        max_output_tokens=150,
        stop_sequences=["END"],
    )

    warm_up = provider.warm_up_sync(request)
    first = provider.generate_sync(request)
    second = provider.generate_sync(request)
    provider.close_sync()

    assert warm_up["processor"] == "gpu"
    assert warm_up["inference_workers"] == 1
    assert first.response_text == second.response_text == "response"
    assert first.runtime_metadata["eval_duration_ns"] == 30
    assert paths.count("/api/chat") == 2
    assert paths.count("/api/generate") == 1
    assert generate_payloads[0]["options"]["num_predict"] == 1
    assert generate_payloads[0]["options"]["temperature"] == 0
    assert generate_payloads[0]["think"] is False
    assert chat_payloads[0]["options"]["num_predict"] == 150
    assert chat_payloads[0]["options"]["stop"] == ["END"]
    assert chat_payloads[0]["think"] is False
    assert client.is_closed


def test_timeout_is_not_retried_after_inference_has_started():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("deadline exceeded", request=request)

    config = ModelConfig(id="model", adapter="ollama", model="model", base_url="http://test")
    provider = OllamaProvider(config, RuntimeConfig(retries=3, retry_backoff_seconds=0))
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = ProviderRequest(
        conversation_messages=[ChatMessage(role="user", content="prompt")],
        model="model",
    )
    try:
        result = provider.generate_sync(request)
    finally:
        provider.close_sync()

    assert calls == 1
    assert result.error_type == "ReadTimeout"


def test_openrouter_disables_reasoning_explicitly_for_saved_profiles():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        })

    config = ModelConfig(
        id="judge",
        adapter="openai",
        model="deepseek/deepseek-v4-flash",
        base_url="https://openrouter.ai/api/v1",
        credential_id="openrouter",
    )
    provider = OpenAICompatibleProvider(config, RuntimeConfig(retries=0))
    provider._headers = lambda: {"Content-Type": "application/json"}
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = ProviderRequest(
        conversation_messages=[ChatMessage(role="user", content="judge")],
        model=config.model,
    )

    try:
        provider.generate_sync(request)
    finally:
        provider.close_sync()

    assert config.reasoning_enabled is False
    assert captured["reasoning"] == {"enabled": False}
    assert captured["provider"] == {
        "allow_fallbacks": True,
        "sort": "throughput",
        "require_parameters": False,
    }


def test_openrouter_enforces_json_schema_for_judge_requests():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"severity": 0}'}}],
            "usage": {},
        })

    config = ModelConfig(
        id="judge",
        adapter="openai",
        model="deepseek/deepseek-v4-flash-0731",
        base_url="https://openrouter.ai/api/v1",
        credential_id="openrouter",
    )
    provider = OpenAICompatibleProvider(config, RuntimeConfig(retries=0))
    provider._headers = lambda: {"Content-Type": "application/json"}
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    schema = {"type": "object", "properties": {"severity": {"type": "integer"}}}
    request = ProviderRequest(
        conversation_messages=[ChatMessage(role="user", content="judge")],
        model=config.model,
        response_schema=schema,
    )

    try:
        provider.generate_sync(request)
    finally:
        provider.close_sync()

    assert captured["response_format"]["type"] == "json_schema"
    assert captured["response_format"]["json_schema"]["strict"] is True
    assert captured["response_format"]["json_schema"]["schema"] == schema
    assert captured["provider"]["require_parameters"] is True


def test_non_openrouter_api_does_not_receive_reasoning_parameter():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(__import__("json").loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {},
        })

    config = ModelConfig(
        id="api",
        adapter="openai",
        model="model",
        base_url="https://provider.example/v1",
        credential_id="provider",
    )
    provider = OpenAICompatibleProvider(config, RuntimeConfig(retries=0))
    provider._headers = lambda: {"Content-Type": "application/json"}
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = ProviderRequest(
        conversation_messages=[ChatMessage(role="user", content="test")],
        model=config.model,
    )

    try:
        provider.generate_sync(request)
    finally:
        provider.close_sync()

    assert "reasoning" not in captured


def test_openai_provider_retries_a_success_response_without_choices():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"error": {"message": "upstream unavailable"}})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "recovered"}}],
            "usage": {},
        })

    config = ModelConfig(
        id="judge", adapter="openai", model="judge", credential_id="credential"
    )
    provider = OpenAICompatibleProvider(
        config, RuntimeConfig(retries=2, retry_backoff_seconds=0)
    )
    provider._headers = lambda: {"Content-Type": "application/json"}
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = ProviderRequest(
        conversation_messages=[ChatMessage(role="user", content="judge")], model="judge"
    )
    try:
        result = provider.generate_sync(request)
    finally:
        provider.close_sync()

    assert calls == 2
    assert result.response_text == "recovered"
    assert result.error_type == ""


def test_openai_provider_retries_openrouter_provider_error_http_400():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(400, json={"error": {"message": "Provider returned error"}})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "recovered"}}],
            "usage": {},
        })

    config = ModelConfig(
        id="judge", adapter="openai", model="judge", credential_id="credential"
    )
    provider = OpenAICompatibleProvider(
        config, RuntimeConfig(retries=2, retry_backoff_seconds=0)
    )
    provider._headers = lambda: {"Content-Type": "application/json"}
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = ProviderRequest(
        conversation_messages=[ChatMessage(role="user", content="judge")], model="judge"
    )
    try:
        result = provider.generate_sync(request)
    finally:
        provider.close_sync()

    assert calls == 2
    assert result.response_text == "recovered"


def test_openai_provider_does_not_retry_a_non_transient_bad_request():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "invalid model parameter"}})

    config = ModelConfig(
        id="judge", adapter="openai", model="judge", credential_id="credential"
    )
    provider = OpenAICompatibleProvider(
        config, RuntimeConfig(retries=2, retry_backoff_seconds=0)
    )
    provider._headers = lambda: {"Content-Type": "application/json"}
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = ProviderRequest(
        conversation_messages=[ChatMessage(role="user", content="judge")], model="judge"
    )
    try:
        result = provider.generate_sync(request)
    finally:
        provider.close_sync()

    assert calls == 1
    assert result.error_type == "ValueError"


def test_generic_http_provider_rejects_openrouter_before_a_run():
    config = ModelConfig(
        id="wrong-openrouter",
        adapter="custom_http",
        model="model",
        endpoint="https://openrouter.ai/api/v1",
    )
    provider = CustomHttpProvider(config, RuntimeConfig(retries=0))

    try:
        ok, message = provider._loop().run_until_complete(provider.check())
    finally:
        provider.close_sync()

    assert ok is False
    assert "Select API, then OpenRouter" in message
