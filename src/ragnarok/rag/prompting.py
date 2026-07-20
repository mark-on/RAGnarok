from __future__ import annotations

from ..schemas import ChatMessage, RetrievalHit


def retrieval_context(hits: list[RetrievalHit]) -> str:
    return "\n\n".join(
        f"[Reference {hit.rank}; {hit.document_path}; {hit.extracted_surface}]\n{hit.content}"
        for hit in hits
    )


def inference_messages(
    clean_history: list[ChatMessage], user_prompt: str, hits: list[RetrievalHit]
) -> list[ChatMessage]:
    """Attach RAG context to this turn without storing it in chat history."""

    current = ChatMessage(
        role="user",
        content=(
            f"Retrieved reference material:\n{retrieval_context(hits)}"
            f"\n\nUser question:\n{user_prompt}"
        ),
    )
    return [*clean_history, current]
