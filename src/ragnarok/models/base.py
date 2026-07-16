from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

import httpx

from ..config import ModelConfig, RuntimeConfig
from ..schemas import ProviderRequest, ProviderResult


SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization|api[-_ ]?key|token|secret)(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
]


def redact(value: str) -> str:
    for pattern in SECRET_PATTERNS:
        value = pattern.sub(lambda match: (match.group(1) + match.group(2) if match.lastindex and match.lastindex >= 2 else "") + "[REDACTED]", value)
    return value[:1000]


def nested_get(payload, path: str | None, default=None):
    if not path:
        return default
    current = payload
    for part in path.split("."):
        if isinstance(current, list):
            current = current[int(part)]
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
        self.retry_count = 0

    @abstractmethod
    async def generate(self, request: ProviderRequest) -> ProviderResult: ...

    async def check(self) -> tuple[bool, str]:
        return True, "provider configured"

    async def _retry(self, operation: Callable[[], Awaitable[ProviderResult]]) -> ProviderResult:
        last_error: Exception | None = None
        for attempt in range(self.runtime.retries + 1):
            try:
                return await operation()
            except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
                last_error = exc
                if attempt == self.runtime.retries:
                    break
                self.retry_count += 1
                await asyncio.sleep(self.runtime.retry_backoff_seconds * (2 ** attempt))
        return ProviderResult(provider=self.name, model=self.config.model, error_type=type(last_error).__name__, error_message=redact(str(last_error)))


def provider_for(config: ModelConfig, runtime: RuntimeConfig) -> ModelProvider:
    from .custom_http import CustomHttpProvider
    from .mock import MockProvider
    from .ollama import OllamaProvider
    from .openai_compatible import OpenAICompatibleProvider

    mapping = {"mock": MockProvider, "ollama": OllamaProvider, "openai_compatible": OpenAICompatibleProvider, "custom_http": CustomHttpProvider}
    return mapping[config.adapter](config, runtime)

