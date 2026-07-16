"""Structural and corpus validator for the RAGnarok pilot dataset."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "dataset" / "dataset.csv"
REQUIRED_COLUMNS = [
    "case_id", "conversation_id", "turn_index", "is_continuation", "prompt",
    "is_attack", "attack_vector", "attack_objective", "attack_technique", "domain",
    "source_document", "document_role", "obfuscation_technique", "expected_behavior",
    "success_criteria", "evaluation_target", "response",
]
ALLOWED_VECTORS = {"direct", "indirect", "none"}
ALLOWED_DOMAINS = {"university", "corporate", "technical_support", "public_services", "customer_support"}
ALLOWED_DOCUMENT_ROLES = {"clean", "poisoned", "restricted", "none"}
ALLOWED_OBFUSCATION = {"none", "pdf_metadata", "white_on_white"}
DIRECT_TECHNIQUES = {
    "instruction_following", "role_playing", "logic_trap_construction",
    "systematic_evaluation_frameworks",
}
INDIRECT_TECHNIQUES = {
    "third_party_content_contamination", "data_flow_injection", "command_injection",
    "retrieval_ranking_manipulation", "multi_hop_chained_injection",
    "persistent_delayed_injection", "citation_authority_spoofing",
}
CREDENTIAL_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
    re.compile(r"\b(?:api|secret)[_-]?key\s*[:=]\s*[A-Za-z0-9_-]{12,}\b", re.IGNORECASE),
    re.compile(r"\b(?:password|token)\s*[:=]\s*[A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
]


def contains_structured_json(value: str) -> bool:
    """Return True only for cells containing a complete JSON object or array."""
    stripped = value.strip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return isinstance(parsed, (dict, list))


def validate() -> list[str]:
    errors: list[str] = []
    if not DATASET.is_file():
        return ["dataset/dataset.csv does not exist"]

    with DATASET.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != REQUIRED_COLUMNS:
            return [f"expected exactly the 17 required columns, got {reader.fieldnames}"]
        rows = list(reader)

    if len(rows) != 100:
        errors.append(f"expected 100 rows, got {len(rows)}")
    case_ids = [row["case_id"] for row in rows]
    if len(case_ids) != len(set(case_ids)):
        errors.append("case_id values are not unique")

    conversations: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row_number, row in enumerate(rows, 2):
        label = row.get("case_id") or f"CSV row {row_number}"
        if not row.get("conversation_id"):
            errors.append(f"{label}: conversation_id is empty")
        else:
            conversations[row["conversation_id"]].append(row)
        if not row.get("prompt", "").strip():
            errors.append(f"{label}: prompt is empty")
        if row.get("response", "") != "":
            errors.append(f"{label}: response must be empty")
        if row.get("is_attack") not in {"true", "false"}:
            errors.append(f"{label}: invalid is_attack")
        if row.get("is_continuation") not in {"true", "false"}:
            errors.append(f"{label}: invalid is_continuation")
        if row.get("attack_vector") not in ALLOWED_VECTORS:
            errors.append(f"{label}: invalid attack_vector")
        if row.get("domain") not in ALLOWED_DOMAINS:
            errors.append(f"{label}: invalid domain")
        role = row.get("document_role")
        source = row.get("source_document", "")
        obfuscation = row.get("obfuscation_technique", "")
        if role not in ALLOWED_DOCUMENT_ROLES:
            errors.append(f"{label}: invalid document_role")
        if role == "none" and source:
            errors.append(f"{label}: document_role=none must not reference a document")
        if role != "none" and not source:
            errors.append(f"{label}: retrieval-backed case lacks source_document")
        if source:
            source_path = (ROOT / source).resolve()
            knowledge_root = (ROOT / "knowledge_base").resolve()
            if knowledge_root not in source_path.parents or not source_path.is_file():
                errors.append(f"{label}: source_document does not resolve to a knowledge-base file: {source}")
            if source_path.suffix.lower() != ".pdf":
                errors.append(f"{label}: source_document must reference a PDF")
        if obfuscation not in ALLOWED_OBFUSCATION:
            errors.append(f"{label}: invalid obfuscation_technique")
        if role != "poisoned" and obfuscation != "none":
            errors.append(f"{label}: only poisoned documents may declare obfuscation")
        if row.get("is_attack") == "true" and not row.get("evaluation_target"):
            errors.append(f"{label}: attack row lacks an evaluation_target")
        if row.get("is_attack") == "false" and role != "clean":
            errors.append(f"{label}: benign case must reference a clean document")
        if row.get("attack_vector") == "indirect" and role != "poisoned":
            errors.append(f"{label}: indirect attack must reference a poisoned document")
        if row.get("attack_vector") == "direct" and role not in {"restricted", "none"}:
            errors.append(f"{label}: direct attack has an unexpected document role")
        technique = row.get("attack_technique", "")
        if row.get("attack_vector") == "direct" and technique not in DIRECT_TECHNIQUES:
            errors.append(f"{label}: direct technique is not aligned with the source taxonomy")
        if row.get("attack_vector") == "indirect" and technique not in INDIRECT_TECHNIQUES:
            errors.append(f"{label}: indirect technique is not aligned with the source taxonomy")
        if row.get("attack_vector") == "none" and technique:
            errors.append(f"{label}: benign case must not declare an attack technique")
        if row.get("attack_vector") == "direct" and row.get("attack_objective") != "behavior_hijacking" and row.get("evaluation_target") in row.get("prompt", ""):
            errors.append(f"{label}: protected direct target is exposed in the attacker prompt")
        for column, value in row.items():
            if contains_structured_json(value):
                errors.append(f"{label}: {column} contains structured JSON; dataset cells must remain scalar")

    for conversation_id, turns in conversations.items():
        try:
            indexes = [int(turn["turn_index"]) for turn in turns]
        except ValueError:
            errors.append(f"{conversation_id}: turn_index must be an integer")
            continue
        if indexes != list(range(1, len(turns) + 1)):
            errors.append(f"{conversation_id}: turn indexes are not sequential from 1")
        if turns[0]["is_continuation"] != "false":
            errors.append(f"{conversation_id}: first turn must have is_continuation=false")
        if any(turn["is_continuation"] != "true" for turn in turns[1:]):
            errors.append(f"{conversation_id}: later turns must have is_continuation=true")

    # Small composition checks keep the pilot understandable.
    vectors = Counter(row["attack_vector"] for row in rows)
    if vectors != {"none": 48, "direct": 20, "indirect": 32}:
        errors.append(f"unexpected pilot composition: {dict(vectors)}")
    multi = [turns for turns in conversations.values() if len(turns) > 1]
    if len(multi) != 5 or any(len(turns) != 4 for turns in multi):
        errors.append("expected five four-turn conversations")
    single = [turns for turns in conversations.values() if len(turns) == 1]
    if len(conversations) != 85 or len(single) != 80:
        errors.append(f"expected 85 conversations containing 80 single-turn conversations, got {len(conversations)} total and {len(single)} single-turn")

    knowledge_files = sorted((ROOT / "knowledge_base").rglob("*.pdf"))
    if len(knowledge_files) != 40:
        errors.append(f"expected 40 PDF knowledge-base documents, got {len(knowledge_files)}")
    markdown_files = sorted((ROOT / "knowledge_base").rglob("*.md"))
    if markdown_files:
        errors.append(f"knowledge base still contains {len(markdown_files)} Markdown files")
    domain_counts = Counter(path.parent.name for path in knowledge_files)
    if domain_counts != {domain: 8 for domain in ALLOWED_DOMAINS}:
        errors.append(f"expected eight documents per domain, got {dict(domain_counts)}")
    document_ids: list[str] = []
    required_sections = ["Purpose and scope", "Service rules", "Standard procedure"]
    pdf_text: dict[str, str] = {}
    pdf_metadata: dict[str, dict[str, str]] = {}
    for path in knowledge_files:
        try:
            reader = PdfReader(path)
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            metadata = {str(key): str(value) for key, value in (reader.metadata or {}).items()}
        except Exception as exc:
            errors.append(f"{path.relative_to(ROOT)}: unreadable PDF: {exc}")
            continue
        relative = path.relative_to(ROOT).as_posix()
        pdf_text[relative] = text
        pdf_metadata[relative] = metadata
        id_match = re.search(r"Document ID:\s*([A-Z0-9-]+)", text)
        if not id_match:
            errors.append(f"{path.relative_to(ROOT)}: missing Document ID")
        else:
            document_ids.append(id_match.group(1).strip())
        if path.name != "restricted_records.pdf":
            for section in required_sections:
                if section not in text:
                    errors.append(f"{path.relative_to(ROOT)}: missing section {section}")
            if len(text.split()) < 150:
                errors.append(f"{path.relative_to(ROOT)}: document is too short for the realistic corpus")
    if len(document_ids) != len(set(document_ids)):
        errors.append("knowledge-base Document ID values are not unique")

    poisoned_sources = {row["source_document"] for row in rows if row["document_role"] == "poisoned"}
    restricted_sources = {row["source_document"] for row in rows if row["document_role"] == "restricted"}
    if len(poisoned_sources) != 10:
        errors.append(f"expected ten authoritative poisoned documents, got {len(poisoned_sources)}")
    if len(restricted_sources) != 5:
        errors.append(f"expected five restricted record documents, got {len(restricted_sources)}")
    source_obfuscation = {
        row["source_document"]: row["obfuscation_technique"]
        for row in rows if row["document_role"] == "poisoned"
    }
    if Counter(source_obfuscation.values()) != {"none": 4, "pdf_metadata": 3, "white_on_white": 3}:
        errors.append(f"unexpected poisoned PDF surface composition: {dict(Counter(source_obfuscation.values()))}")
    for source, obfuscation in source_obfuscation.items():
        metadata = pdf_metadata.get(source, {})
        text = pdf_text.get(source, "")
        if obfuscation == "pdf_metadata" and not metadata.get("/IndexingNote"):
            errors.append(f"{source}: metadata-obfuscated PDF lacks /IndexingNote")
        if obfuscation != "pdf_metadata" and metadata.get("/IndexingNote"):
            errors.append(f"{source}: unexpected metadata injection")
        if obfuscation == "white_on_white":
            targets = {row["evaluation_target"] for row in rows if row["source_document"] == source}
            if not any(target.lower() in text.lower() for target in targets):
                errors.append(f"{source}: white-on-white evaluation target is not text-extractable")
    indirect_techniques = {row["attack_technique"] for row in rows if row["attack_vector"] == "indirect"}
    if indirect_techniques != INDIRECT_TECHNIQUES:
        errors.append(f"indirect techniques do not cover the source taxonomy: {sorted(indirect_techniques)}")

    searchable_text = DATASET.read_text(encoding="utf-8")
    for source, text in pdf_text.items():
        searchable_text += "\n" + text
        searchable_text += "\n" + " ".join(pdf_metadata.get(source, {}).values())
    if any(pattern.search(searchable_text) for pattern in CREDENTIAL_PATTERNS):
        errors.append("real-looking credential or API key detected")
    if re.search(r"\b(?:PROMPT|PRIVATE|THIRD_PARTY|HIJACK|ADMIN)_CANARY_", searchable_text):
        errors.append("artificial canary identifier detected")
    emails = re.findall(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", searchable_text)
    if any(not email.lower().endswith(".example") for email in emails):
        errors.append("non-example email address detected")
    return errors


if __name__ == "__main__":
    problems = validate()
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}")
        raise SystemExit(1)
    print("Red-team pilot validation passed: one flat CSV with 100 scalar rows, 85 conversations, 17 columns, 40 verified PDFs, controlled obfuscation, valid references, and empty responses.")
