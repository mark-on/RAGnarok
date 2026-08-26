import json
import os
import time
from pathlib import Path
from typing import Optional

import requests
from spikee.templates.target import Target
from spikee.utilities.hinting import (
    Content,
    ModuleDescriptionHint,
    ModuleOptionsHint,
    TargetResponseHint,
    get_content,
)
from spikee.utilities.modules import parse_options


class RAGnarokLLMTarget(Target):
    """SPIKEE transport adapter with deterministic, bounded inference."""

    def __init__(self):
        super().__init__()
        self.session = requests.Session()
        self._log_handle = None

    def get_description(self) -> ModuleDescriptionHint:
        return [], "RAGnarok bounded Ollama and OpenAI-compatible target"

    def get_available_option_values(self) -> ModuleOptionsHint:
        return ["provider=ollama,model=<model>,max_tokens=1024"], True

    def _request_log(self):
        path = os.getenv("RAGNAROK_REQUEST_LOG")
        if not path:
            return None
        if self._log_handle is None:
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = target.open("a", encoding="utf-8", buffering=1)
        return self._log_handle

    def _record(self, row: dict) -> None:
        handle = self._request_log()
        if handle is not None:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def close(self) -> None:
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        self.session.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def process_input(
        self,
        input_text: Content,
        system_message: Optional[Content] = None,
        target_options: Optional[str] = "",
    ) -> TargetResponseHint:
        options = parse_options(target_options)
        provider = options.get("provider", "ollama")
        model = options.get("model")
        if not model:
            raise ValueError("RAGnarok SPIKEE target requires model=<model>")
        max_tokens = int(options.get("max_tokens", "1024"))
        temperature = float(options.get("temperature", "0"))
        timeout = float(os.getenv("RAGNAROK_REQUEST_TIMEOUT", "120"))
        messages = []
        if system_message:
            messages.append({"role": "system", "content": get_content(system_message)})
        prompt_text = get_content(input_text)
        messages.append({"role": "user", "content": prompt_text})
        started = time.perf_counter()
        response_text = ""
        input_tokens = None
        output_tokens = None
        runtime_metadata: dict[str, object] = {
            "reasoning_enabled": False,
            "max_output_tokens": max_tokens,
        }
        error_type = ""
        error_message = ""
        try:
            if provider == "ollama":
                base_url = os.getenv("RAGNAROK_OLLAMA_URL", "http://localhost:11434").rstrip("/")
                response = self.session.post(
                    f"{base_url}/api/chat",
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        "think": False,
                        "keep_alive": os.getenv("RAGNAROK_OLLAMA_KEEP_ALIVE", "10m"),
                        "options": {
                            "temperature": temperature,
                            "num_predict": max_tokens,
                        },
                    },
                    timeout=timeout,
                )
                response.raise_for_status()
                data = response.json()
                response_text = str(data.get("message", {}).get("content", ""))
                input_tokens = data.get("prompt_eval_count")
                output_tokens = data.get("eval_count")
                runtime_metadata.update({
                    "total_duration_ns": data.get("total_duration"),
                    "load_duration_ns": data.get("load_duration"),
                    "prompt_eval_duration_ns": data.get("prompt_eval_duration"),
                    "eval_duration_ns": data.get("eval_duration"),
                    "max_output_tokens_enforced_as": "num_predict",
                })
            elif provider == "openai":
                base_url = os.environ["RAGNAROK_OPENAI_BASE_URL"].rstrip("/")
                api_key = os.environ["RAGNAROK_OPENAI_API_KEY"]
                payload: dict[str, object] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                reasoning = os.getenv("RAGNAROK_REASONING_ENABLED")
                if reasoning is not None:
                    payload["reasoning"] = {"enabled": reasoning.lower() == "true"}
                response = self.session.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=timeout,
                )
                response.raise_for_status()
                data = response.json()
                response_text = str(data["choices"][0]["message"].get("content") or "")
                usage = data.get("usage", {})
                input_tokens = usage.get("prompt_tokens")
                output_tokens = usage.get("completion_tokens")
            else:
                raise ValueError(f"unsupported RAGnarok SPIKEE provider: {provider}")
            return response_text
        except Exception as exc:
            error_type = type(exc).__name__
            error_message = str(exc)[:1000]
            raise
        finally:
            self._record({
                "phase": "subject_inference",
                "raw_prompt": prompt_text,
                "response": response_text,
                "provider": provider,
                "model": model,
                "temperature": temperature,
                "max_output_tokens": max_tokens,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "wall_duration_seconds": time.perf_counter() - started,
                "runtime_metadata": runtime_metadata,
                "error_type": error_type,
                "error_message": error_message,
            })
