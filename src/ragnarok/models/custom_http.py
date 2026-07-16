from __future__ import annotations

import asyncio
import time

import httpx

from ..schemas import ProviderRequest, ProviderResult
from ..credentials import resolve_credential
from .base import ModelProvider, nested_get


def _set_nested(payload: dict, path: str, value) -> None:
    current = payload
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


class CustomHttpProvider(ModelProvider):
    name = "custom_http"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.config.headers}
        auth = self.config.authentication
        if auth.type != "none":
            token = resolve_credential(auth.credential_id, auth.token_env)
            if not token:
                raise ValueError(f"credential is unavailable: {auth.credential_id or auth.token_env}")
            headers[auth.header_name] = f"Bearer {token}" if auth.type == "bearer" else token
        return headers

    def _payload(self, request: ProviderRequest) -> dict:
        values = {
            "system_prompt": request.system_prompt,
            "messages": [message.model_dump() for message in request.conversation_messages],
            "model": request.model, "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens, "seed": request.seed,
        }
        payload: dict = {}
        mapping = self.config.request_mapping or {key: key for key in values}
        for source, destination in mapping.items():
            if source in values:
                _set_nested(payload, destination, values[source])
        payload.update(request.provider_options)
        return payload

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        async def operation():
            started = time.perf_counter()
            async with httpx.AsyncClient(timeout=request.timeout) as client:
                response = await client.request(self.config.method, self.config.endpoint or "", headers=self._headers(), json=self._payload(request))
                response.raise_for_status()
                data = response.json()
                if self.config.polling.enabled:
                    poll_url = nested_get(data, self.config.polling.status_url_path)
                    if not isinstance(poll_url, str):
                        raise ValueError("polling status URL path did not return a URL")
                    for _ in range(self.config.polling.max_attempts):
                        if isinstance(nested_get(data, self.config.response_text_path), str):
                            break
                        await asyncio.sleep(self.config.polling.interval_seconds)
                        poll_response = await client.get(poll_url, headers=self._headers())
                        poll_response.raise_for_status()
                        data = poll_response.json()
                    else:
                        raise ValueError("asynchronous job polling exceeded its configured attempt limit")
            error = nested_get(data, self.config.error_path)
            if error:
                raise ValueError(str(error))
            text = nested_get(data, self.config.response_text_path)
            if not isinstance(text, str):
                raise ValueError("configured response text path did not return a string")
            return ProviderResult(response_text=text, provider=self.name, model=request.model, latency_ms=(time.perf_counter()-started)*1000, input_tokens=nested_get(data, self.config.input_tokens_path), output_tokens=nested_get(data, self.config.output_tokens_path), sanitized_raw_metadata={"status_code": response.status_code})
        return await self._retry(operation)
