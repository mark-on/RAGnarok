from __future__ import annotations

from ..schemas import ChatMessage, RetrievalHit


def retrieval_context(hits: list[RetrievalHit]) -> str:
    sections = [
        f"[Retrieved reference {hit.rank}; path={hit.document_path}; surface={hit.extracted_surface}]\n{hit.content}"
        for hit in hits
    ]
    return "\n\n".join(sections)


def inference_messages(history: list[ChatMessage], user_prompt: str, hits: list[RetrievalHit]) -> list[ChatMessage]:
    # Only conversation messages, the current public prompt, and actual retrieval are included.
    content = f"Retrieved reference material:\n{retrieval_context(hits)}\n\nUser question:\n{user_prompt}"
    return [*history, ChatMessage(role="user", content=content)]

