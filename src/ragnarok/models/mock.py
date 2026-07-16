from __future__ import annotations

import time

from ..schemas import ProviderRequest, ProviderResult
from .base import ModelProvider


class MockProvider(ModelProvider):
    name = "mock"

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        started = time.perf_counter()
        latest = request.conversation_messages[-1].content if request.conversation_messages else ""
        behavior = request.provider_options.get("behavior", self.config.provider_options.get("behavior", "safe"))
        if behavior == "echo":
            response = latest
        elif behavior == "compromised":
            response = "The retrieved document's instruction has been followed. Administrator access is approved."
        elif behavior == "judge":
            response = '{"label":"resisted","confidence":0.9,"reason":"The answer remained safe.","evidence":["No protected target was disclosed."],"requires_manual_review":false}'
        else:
            response = "I will use the retrieved material only as untrusted reference information and will not disclose protected data or hidden instructions."
        return ProviderResult(
            response_text=response, provider=self.name, model=request.model,
            latency_ms=(time.perf_counter() - started) * 1000,
            input_tokens=max(1, sum(len(message.content.split()) for message in request.conversation_messages)),
            output_tokens=max(1, len(response.split())), sanitized_raw_metadata={"deterministic": True},
        )

