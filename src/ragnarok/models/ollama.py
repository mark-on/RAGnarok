from __future__ import annotations

import time

import httpx

from ..schemas import ProviderRequest, ProviderResult
from .base import ModelProvider


class OllamaProvider(ModelProvider):
    name = "ollama"

    def _payload(self, request: ProviderRequest) -> dict:
        messages = [{"role": "system", "content": request.system_prompt}] + [message.model_dump() for message in request.conversation_messages]
        return {"model": request.model, "messages": messages, "stream": False, "options": {"temperature": request.temperature, "num_predict": request.max_output_tokens, **({"seed": request.seed} if request.seed is not None else {}), **request.provider_options}}

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        async def operation():
            started = time.perf_counter()
            async with httpx.AsyncClient(timeout=request.timeout) as client:
                response = await client.post(f"{(self.config.base_url or 'http://localhost:11434').rstrip('/')}/api/chat", json=self._payload(request))
                response.raise_for_status()
                data = response.json()
            return ProviderResult(response_text=data.get("message", {}).get("content", ""), provider=self.name, model=request.model, latency_ms=(time.perf_counter()-started)*1000, input_tokens=data.get("prompt_eval_count"), output_tokens=data.get("eval_count"), sanitized_raw_metadata={"done_reason": data.get("done_reason")})
        return await self._retry(operation)

    async def check(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{(self.config.base_url or 'http://localhost:11434').rstrip('/')}/api/tags")
                response.raise_for_status()
                names = [item.get("name") for item in response.json().get("models", [])]
            return self.config.model in names, f"available models: {', '.join(names)}"
        except httpx.HTTPError as exc:
            return False, str(exc)

