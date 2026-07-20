from __future__ import annotations

import hashlib

from ..schemas import Chunk, ExtractedUnit


def chunk_units(units: list[ExtractedUnit], chunk_size: int, overlap: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    step = chunk_size - overlap
    for unit in units:
        content = unit.content
        for start in range(0, len(content), step):
            text = content[start:start + chunk_size]
            if not text.strip():
                continue
            identity = f"{unit.document_path}|{unit.page_number}|{unit.extracted_surface}|{start}|{text}"
            digest = hashlib.sha256(identity.encode()).hexdigest()
            chunks.append(Chunk(
                chunk_id=f"chunk-{digest[:20]}", document_path=unit.document_path,
                document_id=unit.document_id, page_number=unit.page_number,
                extracted_surface=unit.extracted_surface, content=text,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
            ))
            if start + chunk_size >= len(content):
                break
    return chunks
