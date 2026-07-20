from __future__ import annotations

import httpx

from ..credentials import resolve_credential
from ..schemas import ProviderRequest, ProviderResult
from .base import ModelProvider


class OpenAICompatibleProvider(ModelProvider):
    name = "api"

    def _headers(self) -> dict[str, str]:
        token = resolve_credential(self.config.credential_id)
        if not token:
            raise ValueError("API credential is unavailable")
        return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        async def operation():
            payload = {
                "model": request.model,
                "messages": [
                    {"role": "system", "content": request.system_prompt},
                    *[message.model_dump() for message in request.conversation_messages],
                ],
                "temperature": request.temperature,
                "max_tokens": request.max_output_tokens,
            }
            async with httpx.AsyncClient(timeout=request.timeout) as client:
                response = await client.post(
                    f"{(self.config.base_url or 'https://api.openai.com/v1').rstrip('/')}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
            usage = data.get("usage", {})
            return ProviderResult(
                response_text=data["choices"][0]["message"]["content"],
                provider=self.name,
                model=request.model,
                input_tokens=usage.get("prompt_tokens"),
                output_tokens=usage.get("completion_tokens"),
            )

        return await self._retry(operation)

    async def check(self) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.get(
                    f"{(self.config.base_url or 'https://api.openai.com/v1').rstrip('/')}/models",
                    headers=self._headers(),
                )
                response.raise_for_status()
                names = [item.get("id") for item in response.json().get("data", [])]
            return self.config.model in names, f"API models discovered: {len(names)}"
        except (httpx.HTTPError, ValueError) as exc:
            return False, str(exc)
