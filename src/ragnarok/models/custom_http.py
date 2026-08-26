from __future__ import annotations

import httpx

from ..credentials import resolve_credential
from ..schemas import ProviderRequest, ProviderResult
from .base import ModelProvider, nested_get


class CustomHttpProvider(ModelProvider):
    name = "http_endpoint"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        auth = self.config.authentication
        if auth.type != "none":
            token = resolve_credential(auth.credential_id)
            if not token:
                raise ValueError("HTTP endpoint credential is unavailable")
            headers[auth.header_name] = f"Bearer {token}" if auth.type == "bearer" else token
        return headers

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        async def operation():
            payload = {
                "system_prompt": request.system_prompt,
                "messages": [message.model_dump() for message in request.conversation_messages],
                "model": request.model,
                "temperature": request.temperature,
                "max_output_tokens": request.max_output_tokens,
                "stop_sequences": request.stop_sequences,
            }
            response = await self._http_client().post(
                self.config.endpoint or "",
                headers=self._headers(),
                json=payload,
                timeout=request.timeout,
            )
            response.raise_for_status()
            data = response.json()
            text = nested_get(data, self.config.response_text_path)
            if not isinstance(text, str):
                raise ValueError("response text JSON path did not return a string")
            return ProviderResult(response_text=text, provider=self.name, model=request.model)

        return await self._retry(operation)

    async def check(self) -> tuple[bool, str]:
        if not self.config.endpoint:
            return False, "HTTP endpoint is missing"
        try:
            host = httpx.URL(self.config.endpoint).host
        except (TypeError, ValueError):
            host = None
        if host in {"openrouter.ai", "www.openrouter.ai"}:
            return False, (
                "OpenRouter cannot use the generic HTTP endpoint provider. "
                "Select API, then OpenRouter, and choose a model from its catalog."
            )
        return True, "HTTP endpoint configured; it will be contacted during the run"
