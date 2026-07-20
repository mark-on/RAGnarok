from __future__ import annotations

import httpx

from ..schemas import ProviderRequest, ProviderResult
from .base import ModelProvider


class OllamaProvider(ModelProvider):
    name = "ollama"

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        async def operation():
            payload = {
                "model": request.model,
                "messages": [
                    {"role": "system", "content": request.system_prompt},
                    *[message.model_dump() for message in request.conversation_messages],
                ],
                "stream": False,
                "options": {
                    "temperature": request.temperature,
                    "num_predict": request.max_output_tokens,
                },
            }
            async with httpx.AsyncClient(timeout=request.timeout) as client:
                response = await client.post(
                    f"{(self.config.base_url or 'http://localhost:11434').rstrip('/')}/api/chat",
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
            return ProviderResult(
                response_text=data.get("message", {}).get("content", ""),
                provider=self.name,
                model=request.model,
                input_tokens=data.get("prompt_eval_count"),
                output_tokens=data.get("eval_count"),
            )

        return await self._retry(operation)

    async def check(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(
                    f"{(self.config.base_url or 'http://localhost:11434').rstrip('/')}/api/tags"
                )
                response.raise_for_status()
                names = [item.get("name") or item.get("model") for item in response.json().get("models", [])]
            return self.config.model in names, f"installed models: {', '.join(filter(None, names))}"
        except httpx.HTTPError as exc:
            return False, str(exc)
