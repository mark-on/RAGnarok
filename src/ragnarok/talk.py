from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from rich.console import Console

from .config import AppConfig
from .models import ModelProvider, provider_for
from .rag import LocalIndex
from .rag.prompting import inference_messages
from .runner import build_local_index, retrieve_local_context
from .schemas import ChatMessage, ProviderRequest, ProviderResult, RetrievalHit


COMMANDS = "/context · /clear · /exit"


@dataclass(frozen=True)
class TalkTurn:
    result: ProviderResult
    hits: list[RetrievalHit]


class RagChat:
    def __init__(self, config: AppConfig, index: LocalIndex, provider: ModelProvider, system_prompt: str):
        if len(config.models) != 1:
            raise ValueError("talk requires exactly one model")
        self.config = config
        self.model = config.models[0]
        self.index = index
        self.provider = provider
        self.system_prompt = system_prompt
        self.history: list[ChatMessage] = []
        self.last_hits: list[RetrievalHit] = []

    async def ask(self, prompt: str) -> TalkTurn:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("message cannot be empty")
        hits = retrieve_local_context(self.config, self.index, prompt)
        result = await self.provider.generate(ProviderRequest(
            system_prompt=self.system_prompt,
            conversation_messages=inference_messages(self.history, prompt, hits),
            model=self.model.model,
            temperature=self.model.temperature,
            max_output_tokens=self.model.max_output_tokens,
            timeout=self.model.timeout_seconds,
        ))
        self.last_hits = hits
        if not result.error_type:
            self.history.extend([
                ChatMessage(role="user", content=prompt),
                ChatMessage(role="assistant", content=result.response_text),
            ])
        return TalkTurn(result, hits)

    def clear(self) -> None:
        self.history.clear()
        self.last_hits.clear()


def _show_sources(console: Console, hits: list[RetrievalHit]) -> None:
    console.print("[dim]Sources:[/dim]")
    for hit in hits:
        console.print(
            f"[dim]  {hit.rank}. {hit.document_path} "
            f"({hit.extracted_surface}, similarity {hit.similarity_score:.3f})[/dim]"
        )


def _show_context(console: Console, hits: list[RetrievalHit]) -> None:
    if not hits:
        console.print("[dim]Send a message first.[/dim]")
        return
    for hit in hits:
        console.print(
            f"\n[bold]Reference {hit.rank}[/bold] · {hit.document_path} · "
            f"{hit.extracted_surface} · {hit.chunk_id}"
        )
        console.print(hit.content, markup=False)


def run_talk_terminal(
    config: AppConfig,
    *,
    console: Console | None = None,
    input_func: Callable[[], str] | None = None,
) -> None:
    console = console or Console()
    with console.status("[bold cyan]Preparing the RAG index…[/bold cyan]"):
        index, units, chunks, rebuilt = build_local_index(config)
    provider = provider_for(config.models[0], config.runtime)
    system_prompt = config.system_prompt_path.read_text(encoding="utf-8").strip()
    chat = RagChat(config, index, provider, system_prompt)
    state = "rebuilt" if rebuilt else "loaded from cache"
    console.print(
        f"[bold green]Ready.[/bold green] {chat.model.model} · "
        f"RAG {state} · {chunks} chunks from {units} units."
    )
    console.print(f"[dim]{COMMANDS}[/dim]")
    read = input_func or (lambda: console.input("\n[bold cyan]You ›[/bold cyan] "))

    while True:
        try:
            message = read().strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Chat closed.[/dim]")
            return
        if not message:
            continue
        command = message.lower()
        if command in {"/exit", "/quit"}:
            console.print("[dim]Chat closed.[/dim]")
            return
        if command == "/clear":
            chat.clear()
            console.print("[dim]Conversation cleared.[/dim]")
            continue
        if command == "/context":
            _show_context(console, chat.last_hits)
            continue

        with console.status("[bold cyan]Retrieving four chunks and generating…[/bold cyan]"):
            turn = asyncio.run(chat.ask(message))
        if turn.result.error_type:
            console.print(f"[bold red]Error:[/bold red] {turn.result.error_message}")
            continue
        console.print("\n[bold green]Model ›[/bold green]")
        console.print(turn.result.response_text, markup=False)
        _show_sources(console, turn.hits)
