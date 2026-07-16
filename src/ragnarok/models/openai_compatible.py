from __future__ import annotations

import time

import httpx

from ..schemas import ProviderRequest, ProviderResult
from ..credentials import resolve_credential
from .base import ModelProvider


class OpenAICompatibleProvider(ModelProvider):
    name = "openai_compatible"

    def _headers(self) -> dict[str, str]:
        if not self.config.api_key_env and not self.config.credential_id:
            return {"Content-Type": "application/json"}
        token = resolve_credential(self.config.credential_id, self.config.api_key_env)
        if not token:
            raise ValueError(f"credential is unavailable: {self.config.credential_id or self.config.api_key_env}")
        return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    def _payload(self, request: ProviderRequest) -> dict:
        return {"model": request.model, "messages": [{"role": "system", "content": request.system_prompt}] + [message.model_dump() for message in request.conversation_messages], "temperature": request.temperature, "max_tokens": request.max_output_tokens, **({"seed": request.seed} if request.seed is not None else {}), **request.provider_options}

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        async def operation():
            started = time.perf_counter()
            async with httpx.AsyncClient(timeout=request.timeout) as client:
                response = await client.post(f"{(self.config.base_url or '').rstrip('/')}/chat/completions", headers=self._headers(), json=self._payload(request))
                response.raise_for_status()
                data = response.json()
            usage = data.get("usage", {})
            return ProviderResult(response_text=data["choices"][0]["message"]["content"], provider=self.name, model=request.model, latency_ms=(time.perf_counter()-started)*1000, input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"), sanitized_raw_metadata={"finish_reason": data["choices"][0].get("finish_reason")})
        return await self._retry(operation)
