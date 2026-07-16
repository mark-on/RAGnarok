import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_dataset import REQUIRED_COLUMNS, validate


def load_rows():
    with (ROOT / "dataset" / "dataset.csv").open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, list(reader)


def test_basic_csv_structure():
    columns, rows = load_rows()
    assert columns == REQUIRED_COLUMNS
    assert len(rows) == 100
    assert len({row["case_id"] for row in rows}) == 100
    assert all(row["prompt"] and row["response"] == "" for row in rows)
    assert all(not row["source_document"] or (ROOT / row["source_document"]).is_file() for row in rows)


def test_multi_turn_conversations():
    _, rows = load_rows()
    conversations = defaultdict(list)
    for row in rows:
        conversations[row["conversation_id"]].append(row)
    multi = [turns for turns in conversations.values() if len(turns) > 1]
    single = [turns for turns in conversations.values() if len(turns) == 1]
    assert len(conversations) == 85
    assert len(single) == 80
    assert len(multi) == 5
    assert all([int(turn["turn_index"]) for turn in turns] == [1, 2, 3, 4] for turns in multi)
    assert all(turns[0]["is_continuation"] == "false" and all(turn["is_continuation"] == "true" for turn in turns[1:]) for turns in multi)


def test_csv_cells_are_flat_scalars_not_json():
    _, rows = load_rows()
    for row in rows:
        for value in row.values():
            stripped = value.strip()
            if not stripped.startswith(("{", "[")):
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            assert not isinstance(parsed, (dict, list))


def test_every_attack_has_an_observable_target():
    _, rows = load_rows()
    attacks = [row for row in rows if row["is_attack"] == "true"]
    assert len(attacks) == 52
    assert all(row["evaluation_target"] for row in attacks)
    protected_direct = [row for row in attacks if row["attack_vector"] == "direct" and row["attack_objective"] != "behavior_hijacking"]
    assert all(row["evaluation_target"] not in row["prompt"] for row in protected_direct)


def test_document_roles_match_case_types():
    _, rows = load_rows()
    assert all(row["document_role"] == "clean" for row in rows if row["is_attack"] == "false")
    assert all(row["document_role"] == "poisoned" for row in rows if row["attack_vector"] == "indirect")
    assert all(row["document_role"] in {"restricted", "none"} for row in rows if row["attack_vector"] == "direct")
    assert len({row["source_document"] for row in rows if row["document_role"] == "poisoned"}) == 10


def test_realistic_knowledge_base_shape():
    documents = list((ROOT / "knowledge_base").rglob("*.pdf"))
    assert len(documents) == 40
    assert not list((ROOT / "knowledge_base").rglob("*.md"))
    assert Counter(path.parent.name for path in documents) == {
        "university": 8,
        "corporate": 8,
        "technical_support": 8,
        "public_services": 8,
        "customer_support": 8,
    }
    assert not any(path.stem.endswith(("_clean", "_update", "_notice", "_bulletin")) for path in documents)
    assert all("Document ID:" in "\n".join(page.extract_text() or "" for page in PdfReader(path).pages) for path in documents)


def test_pdf_obfuscation_surfaces():
    _, rows = load_rows()
    poisoned = {
        row["source_document"]: row["obfuscation_technique"]
        for row in rows if row["document_role"] == "poisoned"
    }
    assert Counter(poisoned.values()) == {"none": 4, "pdf_metadata": 3, "white_on_white": 3}
    for source, technique in poisoned.items():
        reader = PdfReader(ROOT / source)
        metadata = {str(key): str(value) for key, value in (reader.metadata or {}).items()}
        extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
        if technique == "pdf_metadata":
            assert metadata.get("/IndexingNote")
        elif technique == "white_on_white":
            targets = {row["evaluation_target"] for row in rows if row["source_document"] == source}
            assert any(target.lower() in extracted.lower() for target in targets)
            assert not metadata.get("/IndexingNote")


def test_indirect_taxonomy_coverage():
    _, rows = load_rows()
    techniques = {row["attack_technique"] for row in rows if row["attack_vector"] == "indirect"}
    assert techniques == {
        "third_party_content_contamination",
        "data_flow_injection",
        "command_injection",
        "retrieval_ranking_manipulation",
        "multi_hop_chained_injection",
        "persistent_delayed_injection",
        "citation_authority_spoofing",
    }


def test_artificial_canaries_are_absent():
    text = (ROOT / "dataset" / "dataset.csv").read_text(encoding="utf-8")
    for path in (ROOT / "knowledge_base").rglob("*.pdf"):
        reader = PdfReader(path)
        text += "\n" + "\n".join(page.extract_text() or "" for page in reader.pages)
        text += "\n" + " ".join(str(value) for value in (reader.metadata or {}).values())
    assert "_CANARY_" not in text


def test_attack_manifest_covers_every_pdf():
    manifest_path = ROOT / "knowledge_base_attack_manifest.md"
    manifest = manifest_path.read_text(encoding="utf-8")
    documents = sorted((ROOT / "knowledge_base").rglob("*.pdf"))
    assert "Evaluator-only file" in manifest
    assert manifest.count("](knowledge_base/") == 40
    assert all(path.relative_to(ROOT).as_posix() in manifest for path in documents)
    assert "pdf_metadata" in manifest and "white_on_white" in manifest


def test_minimal_validator():
    assert validate() == []


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"All {len(tests)} dataset tests passed.")
