from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np

from ..schemas import Chunk, RetrievalHit
from .embeddings import Embedder


def corpus_fingerprint(chunks: list[Chunk], embedder: Embedder) -> str:
    value = embedder.model_id + "|" + "|".join(chunk.content_hash for chunk in chunks)
    return hashlib.sha256(value.encode()).hexdigest()


class LocalIndex:
    """Small local cosine-similarity index for the single RAG pipeline."""

    def __init__(self, cache_dir: Path, embedder: Embedder):
        self.cache_dir = cache_dir
        self.embedder = embedder
        self.chunks: list[Chunk] = []
        self.vectors = np.empty((0, 0), dtype=np.float32)
        self.fingerprint = ""

    def build(self, chunks: list[Chunk]) -> bool:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = corpus_fingerprint(chunks, self.embedder)
        metadata_path = self.cache_dir / "index.json"
        vectors_path = self.cache_dir / "vectors.npy"
        if metadata_path.exists() and vectors_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata.get("fingerprint") == fingerprint:
                self.chunks = [Chunk.model_validate(item) for item in metadata["chunks"]]
                self.vectors = np.load(vectors_path)
                self.fingerprint = fingerprint
                return False

        vectors = self.embedder.encode([chunk.content for chunk in chunks])
        temporary_vectors = vectors_path.with_suffix(".npy.tmp")
        with temporary_vectors.open("wb") as handle:
            np.save(handle, vectors)
        os.replace(temporary_vectors, vectors_path)
        payload = {
            "fingerprint": fingerprint,
            "embedding_model": self.embedder.model_id,
            "chunks": [chunk.model_dump() for chunk in chunks],
        }
        temporary_metadata = metadata_path.with_suffix(".json.tmp")
        temporary_metadata.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary_metadata, metadata_path)
        self.chunks = chunks
        self.vectors = vectors
        self.fingerprint = fingerprint
        return True

    def search(self, query: str, top_k: int = 4) -> list[RetrievalHit]:
        if not self.chunks:
            raise RuntimeError("RAG index is empty")
        query_vector = self.embedder.encode([query])[0]
        scores = self.vectors @ query_vector
        selected = np.argsort(-scores)[:top_k]
        return [
            RetrievalHit(
                rank=rank,
                chunk_id=self.chunks[int(index)].chunk_id,
                document_path=self.chunks[int(index)].document_path,
                document_id=self.chunks[int(index)].document_id,
                page_number=self.chunks[int(index)].page_number,
                extracted_surface=self.chunks[int(index)].extracted_surface,
                similarity_score=float(scores[int(index)]),
                content=self.chunks[int(index)].content,
            )
            for rank, index in enumerate(selected, 1)
        ]
