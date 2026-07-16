from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pypdf import PdfReader

from ..config import PdfExtractionConfig
from ..schemas import ExtractedUnit


FORBIDDEN_NAMES = {"knowledge_base_attack_manifest.md"}
METADATA_ALIASES = {name.lower(): name for name in ("Title", "Author", "Subject", "Keywords", "Creator", "Producer", "IndexingNote")}


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _document_id(text: str) -> str:
    match = re.search(r"Document ID:\s*([A-Z0-9-]+)", text)
    return match.group(1) if match else "unknown"


def extract_pdf(path: Path, root: Path, config: PdfExtractionConfig) -> list[ExtractedUnit]:
    if path.suffix.lower() != ".pdf" or path.name in FORBIDDEN_NAMES:
        raise ValueError(f"unsafe index input: {path}")
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    reader = PdfReader(path)
    page_texts = [page.extract_text() or "" for page in reader.pages]
    document_id = _document_id("\n".join(page_texts))
    units: list[ExtractedUnit] = []
    if config.policy != "metadata_only":
        for page_number, content in enumerate(page_texts, 1):
            if content.strip():
                units.append(ExtractedUnit(
                    document_path=relative, document_id=document_id, page_number=page_number,
                    extracted_surface="body", content=content, extraction_method="pypdf.extract_text",
                    content_hash=_hash(content),
                ))
    if config.policy != "body_only":
        requested = {field.lower() for field in config.metadata_fields}
        for raw_key, raw_value in (reader.metadata or {}).items():
            key = str(raw_key).lstrip("/")
            if key.lower() not in requested and key.lower() not in METADATA_ALIASES:
                continue
            value = str(raw_value or "")
            if value.strip():
                units.append(ExtractedUnit(
                    document_path=relative, document_id=document_id, page_number=None,
                    extracted_surface="metadata", metadata_field=key, content=value,
                    extraction_method="pypdf.metadata", content_hash=_hash(value),
                ))
    return units


def extract_knowledge_base(root: Path, config: PdfExtractionConfig) -> list[ExtractedUnit]:
    root = root.resolve()
    if not root.is_dir() or root.name != "knowledge_base":
        raise ValueError("index root must be the knowledge_base directory")
    unsafe = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() != ".pdf"]
    if unsafe:
        raise ValueError(f"evaluator-only or unsupported file found in knowledge-base input: {unsafe[0].name}")
    pdfs = sorted(root.rglob("*.pdf"))
    if not pdfs:
        raise ValueError("knowledge base contains no PDFs")
    return [unit for path in pdfs for unit in extract_pdf(path, root, config)]

