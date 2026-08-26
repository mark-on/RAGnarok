from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

import httpx

from ..config import ModelConfig, RuntimeConfig
from ..schemas import ProviderRequest, ProviderResult


SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization|api[-_ ]?key|token|secret)(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
]


class RetryableProviderError(Exception):
    """A provider-side failure that is safe to retry before accepting output."""


def redact(value: str) -> str:
    for pattern in SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value[:1000]


def nested_get(payload, path: str, default=None):
    current = payload
    for part in path.split("."):
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return default
        elif isinstance(current, dict):
            current = current.get(part, default)
        else:
            return default
    return current


class ModelProvider(ABC):
    name: str

    def __init__(self, config: ModelConfig, runtime: RuntimeConfig):
        self.config = config
        self.runtime = runtime
        self._client: httpx.AsyncClient | None = None
        self._sync_loop: asyncio.AbstractEventLoop | None = None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    @abstractmethod
    async def generate(self, request: ProviderRequest) -> ProviderResult: ...

    async def check(self) -> tuple[bool, str]:
        return True, "provider configured"

    async def warm_up(self, request: ProviderRequest) -> dict:
        """Prepare a local runtime without consuming a benchmark case."""

        return {}

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _loop(self) -> asyncio.AbstractEventLoop:
        if self._sync_loop is None:
            self._sync_loop = asyncio.new_event_loop()
        return self._sync_loop

    def generate_sync(self, request: ProviderRequest) -> ProviderResult:
        """Run repeated synchronous benchmark calls on one persistent event loop."""

        return self._loop().run_until_complete(self.generate(request))

    def warm_up_sync(self, request: ProviderRequest) -> dict:
        return self._loop().run_until_complete(self.warm_up(request))

    def close_sync(self) -> None:
        if self._sync_loop is None:
            return
        try:
            self._sync_loop.run_until_complete(self.aclose())
        finally:
            self._sync_loop.close()
            self._sync_loop = None

    def recommended_concurrency(self) -> int:
        """Return a conservative worker count unless a provider proves extra capacity."""

        return 1

    async def _retry(self, operation: Callable[[], Awaitable[ProviderResult]]) -> ProviderResult:
        last_error: Exception | None = None
        for attempt in range(self.runtime.retries + 1):
            try:
                return await operation()
            except (
                httpx.HTTPError,
                asyncio.TimeoutError,
                ValueError,
                KeyError,
                RetryableProviderError,
            ) as exc:
                last_error = exc
                if attempt < self.runtime.retries and self._is_retryable(exc):
                    delay = self.runtime.retry_backoff_seconds * (2**attempt)
                    if isinstance(exc, RetryableProviderError):
                        delay = max(delay, 1.0 * (2**attempt))
                    await asyncio.sleep(delay)
                    continue
                break
        return ProviderResult(
            provider=self.name,
            model=self.config.model,
            error_type=type(last_error).__name__,
            error_message=redact(str(last_error)),
        )

    @staticmethod
    def _is_retryable(error: Exception) -> bool:
        """Retry only failures that cannot conceal a still-running inference."""

        if isinstance(error, RetryableProviderError):
            return True
        if isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException)):
            return False
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            return status == 429 or status >= 500
        return isinstance(error, (httpx.ConnectError, httpx.NetworkError))


def provider_for(config: ModelConfig, runtime: RuntimeConfig) -> ModelProvider:
    from .anthropic import AnthropicProvider
    from .custom_http import CustomHttpProvider
    from .ollama import OllamaProvider
    from .openai_compatible import OpenAICompatibleProvider

    providers = {
        "ollama": OllamaProvider,
        "openai": OpenAICompatibleProvider,
        "anthropic": AnthropicProvider,
        "custom_http": CustomHttpProvider,
    }
    return providers[config.adapter](config, runtime)
