from __future__ import annotations

import httpx

from ..credentials import resolve_credential
from ..schemas import ProviderRequest, ProviderResult
from .base import ModelProvider, RetryableProviderError


class OpenAICompatibleProvider(ModelProvider):
    name = "api"

    @staticmethod
    def _raise_api_error(response: httpx.Response) -> None:
        if response.is_success:
            return
        message = response.reason_phrase
        try:
            payload = response.json()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            if isinstance(error, dict):
                message = str(error.get("message") or message)
            elif error:
                message = str(error)
        except (TypeError, ValueError):
            pass
        detail = f"API returned HTTP {response.status_code}: {message}"
        lowered = message.lower()
        if (
            response.status_code in {408, 409, 425, 429}
            or response.status_code >= 500
            or (response.status_code == 400 and "provider returned error" in lowered)
        ):
            raise RetryableProviderError(detail)
        raise ValueError(detail)

    @staticmethod
    def _completion(data: object) -> tuple[str, dict]:
        if not isinstance(data, dict):
            raise RetryableProviderError("API returned a non-object response")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            error = data.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("code") or "provider error")
            elif error:
                message = str(error)
            else:
                message = "response did not contain choices"
            raise RetryableProviderError(f"API returned no completion: {message}")
        first = choices[0]
        if not isinstance(first, dict) or not isinstance(first.get("message"), dict):
            raise RetryableProviderError("API returned a malformed completion choice")
        content = first["message"].get("content")
        if not isinstance(content, str):
            raise RetryableProviderError("API completion did not contain text content")
        usage = data.get("usage")
        return content, usage if isinstance(usage, dict) else {}

    def _headers(self) -> dict[str, str]:
        token = resolve_credential(self.config.credential_id)
        if not token:
            raise ValueError("API credential is unavailable")
        return {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        async def operation():
            messages = [message.model_dump() for message in request.conversation_messages]
            if request.system_prompt is not None:
                messages.insert(0, {"role": "system", "content": request.system_prompt})
            payload = {
                "model": request.model,
                "messages": messages,
                "temperature": request.temperature,
                "max_tokens": request.max_output_tokens,
            }
            if request.stop_sequences:
                payload["stop"] = request.stop_sequences
            if request.response_schema is not None:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "ragnarok_judge_result",
                        "strict": True,
                        "schema": request.response_schema,
                    },
                }
            if self.config.reasoning_enabled is not None:
                payload["reasoning"] = {"enabled": self.config.reasoning_enabled}
            if "openrouter.ai" in (self.config.base_url or "").lower():
                payload["provider"] = {
                    "allow_fallbacks": True,
                    "sort": "throughput",
                    "require_parameters": request.response_schema is not None,
                }
            response = await self._http_client().post(
                f"{(self.config.base_url or 'https://api.openai.com/v1').rstrip('/')}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=request.timeout,
            )
            self._raise_api_error(response)
            data = response.json()
            content, usage = self._completion(data)
            return ProviderResult(
                response_text=content,
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
                self._raise_api_error(response)
                names = [item.get("id") for item in response.json().get("data", [])]
            return self.config.model in names, f"API models discovered: {len(names)}"
        except (httpx.HTTPError, ValueError) as exc:
            return False, str(exc)
