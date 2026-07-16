from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

import numpy as np


class Embedder(ABC):
    model_id: str

    @abstractmethod
    def encode(self, texts: list[str]) -> np.ndarray: ...


class MockEmbedder(Embedder):
    """Deterministic token-hashing embedder for tests and network-free demos."""

    model_id = "mock-hash-v1"

    def __init__(self, dimensions: int = 256):
        self.dimensions = dimensions

    def encode(self, texts: list[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), self.dimensions), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in text.lower().split():
                digest = hashlib.sha256(token.strip(".,:;!?()[]").encode()).digest()
                index = int.from_bytes(digest[:4], "little") % self.dimensions
                matrix[row, index] += 1
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return matrix / norms


class SentenceTransformerEmbedder(Embedder):
    def __init__(self, model_id: str):
        from sentence_transformers import SentenceTransformer

        self.model_id = model_id
        self.model = SentenceTransformer(model_id)

    def encode(self, texts: list[str]) -> np.ndarray:
        return np.asarray(self.model.encode(texts, normalize_embeddings=True, show_progress_bar=False), dtype=np.float32)

