from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import numpy as np

from ..schemas import Chunk, RetrievalHit
from .embeddings import Embedder


def corpus_fingerprint(chunks: list[Chunk], embedder: Embedder) -> str:
    payload = embedder.model_id + "|" + "|".join(chunk.content_hash for chunk in chunks)
    return hashlib.sha256(payload.encode()).hexdigest()


class LocalIndex:
    def __init__(self, cache_dir: Path, embedder: Embedder):
        self.cache_dir = cache_dir
        self.embedder = embedder
        self.chunks: list[Chunk] = []
        self.vectors = np.empty((0, 0), dtype=np.float32)
        self.fingerprint = ""

    def build(self, chunks: list[Chunk], force: bool = False) -> bool:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        fingerprint = corpus_fingerprint(chunks, self.embedder)
        metadata_path = self.cache_dir / "index.json"
        vectors_path = self.cache_dir / "vectors.npy"
        if not force and metadata_path.exists() and vectors_path.exists():
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
        payload = {"fingerprint": fingerprint, "embedding_model": self.embedder.model_id, "chunks": [chunk.model_dump() for chunk in chunks]}
        temporary_metadata = metadata_path.with_suffix(".json.tmp")
        temporary_metadata.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary_metadata, metadata_path)
        self.chunks, self.vectors, self.fingerprint = chunks, vectors, fingerprint
        return True

    def load(self) -> None:
        metadata = json.loads((self.cache_dir / "index.json").read_text(encoding="utf-8"))
        self.chunks = [Chunk.model_validate(item) for item in metadata["chunks"]]
        self.vectors = np.load(self.cache_dir / "vectors.npy")
        self.fingerprint = metadata["fingerprint"]

    def search(self, query: str, top_k: int) -> list[RetrievalHit]:
        if not self.chunks:
            raise RuntimeError("index is not loaded")
        query_vector = self.embedder.encode([query])[0]
        scores = self.vectors @ query_vector
        order = np.argsort(-scores)[:top_k]
        return [RetrievalHit(
            rank=rank, chunk_id=self.chunks[index].chunk_id,
            document_path=self.chunks[index].document_path,
            document_id=self.chunks[index].document_id,
            page_number=self.chunks[index].page_number,
            extracted_surface=self.chunks[index].extracted_surface,
            similarity_score=float(scores[index]), content=self.chunks[index].content,
        ) for rank, index in enumerate(order, 1)]

