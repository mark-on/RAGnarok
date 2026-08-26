from __future__ import annotations

import httpx

from ..schemas import ProviderRequest, ProviderResult
from .base import ModelProvider


class OllamaProvider(ModelProvider):
    name = "ollama"
    keep_alive = "10m"

    async def generate(self, request: ProviderRequest) -> ProviderResult:
        async def operation():
            messages = [message.model_dump() for message in request.conversation_messages]
            if request.system_prompt is not None:
                messages.insert(0, {"role": "system", "content": request.system_prompt})
            payload = {
                "model": request.model,
                "messages": messages,
                "stream": False,
                "think": self.config.reasoning_enabled is True,
                "keep_alive": self.keep_alive,
                "options": {
                    "temperature": request.temperature,
                    "num_predict": request.max_output_tokens,
                },
            }
            if request.stop_sequences:
                payload["options"]["stop"] = request.stop_sequences
            response = await self._http_client().post(
                f"{(self.config.base_url or 'http://localhost:11434').rstrip('/')}/api/chat",
                json=payload,
                timeout=request.timeout,
            )
            response.raise_for_status()
            data = response.json()
            return ProviderResult(
                response_text=data.get("message", {}).get("content", ""),
                provider=self.name,
                model=request.model,
                input_tokens=data.get("prompt_eval_count"),
                output_tokens=data.get("eval_count"),
                runtime_metadata={
                    "total_duration_ns": data.get("total_duration"),
                    "load_duration_ns": data.get("load_duration"),
                    "prompt_eval_duration_ns": data.get("prompt_eval_duration"),
                    "eval_duration_ns": data.get("eval_duration"),
                    "keep_alive": self.keep_alive,
                    "reasoning_enabled": self.config.reasoning_enabled is True,
                    "max_output_tokens_enforced_as": "num_predict",
                },
            )

        return await self._retry(operation)

    async def warm_up(self, request: ProviderRequest) -> dict:
        response = await self._http_client().post(
            f"{(self.config.base_url or 'http://localhost:11434').rstrip('/')}/api/generate",
            json={
                "model": request.model,
                "prompt": "",
                "stream": False,
                "think": False,
                "keep_alive": self.keep_alive,
                "options": {
                    "temperature": 0,
                    "num_predict": 1,
                },
            },
            timeout=request.timeout,
        )
        response.raise_for_status()
        data = response.json()
        runtime = {
            "total_duration_ns": data.get("total_duration"),
            "load_duration_ns": data.get("load_duration"),
            "keep_alive": self.keep_alive,
            "inference_workers": self.recommended_concurrency(),
        }
        try:
            active = await self._http_client().get(
                f"{(self.config.base_url or 'http://localhost:11434').rstrip('/')}/api/ps",
                timeout=5,
            )
            active.raise_for_status()
            loaded = next(
                (
                    item for item in active.json().get("models", [])
                    if (item.get("name") or item.get("model")) == request.model
                ),
                None,
            )
            if loaded:
                model_size = loaded.get("size")
                vram_size = loaded.get("size_vram") or 0
                if vram_size and model_size and vram_size >= model_size * 0.95:
                    processor = "gpu"
                elif vram_size:
                    processor = "hybrid"
                else:
                    processor = "cpu"
                runtime.update({
                    "model_size_bytes": model_size,
                    "model_vram_bytes": vram_size,
                    "processor": processor,
                    "expires_at": loaded.get("expires_at"),
                })
        except (httpx.HTTPError, ValueError, TypeError):
            pass
        return runtime

    async def aclose(self) -> None:
        # Keep the model resident for the next benchmark. Ollama evicts it
        # automatically when memory is needed or after ``keep_alive`` expires.
        await super().aclose()

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
