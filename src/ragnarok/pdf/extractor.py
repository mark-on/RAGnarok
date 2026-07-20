from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pypdf import PdfReader

from ..schemas import ExtractedUnit


METADATA_FIELDS = {"title", "author", "subject", "keywords", "creator", "producer", "indexingnote"}


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _document_id(text: str) -> str:
    match = re.search(r"Document ID:\s*([A-Z0-9-]+)", text)
    return match.group(1) if match else "unknown"


def extract_pdf(path: Path, root: Path) -> list[ExtractedUnit]:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    reader = PdfReader(path)
    page_texts = [page.extract_text() or "" for page in reader.pages]
    document_id = _document_id("\n".join(page_texts))
    units = [
        ExtractedUnit(
            document_path=relative,
            document_id=document_id,
            page_number=page_number,
            extracted_surface="body",
            content=content,
            content_hash=_hash(content),
        )
        for page_number, content in enumerate(page_texts, 1)
        if content.strip()
    ]
    metadata = []
    for raw_key, raw_value in (reader.metadata or {}).items():
        key = str(raw_key).lstrip("/")
        value = str(raw_value or "").strip()
        if key.lower() in METADATA_FIELDS and value:
            metadata.append((key, value))
    if metadata:
        content = "\n".join(
            [f"Document path: {relative}", f"Document ID: {document_id}"]
            + [f"{key}: {value}" for key, value in metadata]
        )
        units.append(ExtractedUnit(
            document_path=relative,
            document_id=document_id,
            extracted_surface="metadata",
            content=content,
            content_hash=_hash(content),
        ))
    return units


def extract_knowledge_base(root: Path) -> list[ExtractedUnit]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"knowledge base directory does not exist: {root}")
    pdfs = sorted(root.rglob("*.pdf"))
    if not pdfs:
        raise ValueError("knowledge base contains no PDF files")
    return [unit for path in pdfs for unit in extract_pdf(path, root)]
