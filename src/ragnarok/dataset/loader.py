from __future__ import annotations

import csv
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DatasetTable:
    rows: list[dict[str, str]]


def load_dataset(path: Path) -> DatasetTable:
    if not path.is_file():
        raise ValueError(f"dataset does not exist: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or "prompt" not in reader.fieldnames:
            raise ValueError("dataset must contain a 'prompt' column")
        rows = [{key: value or "" for key, value in row.items()} for row in reader]
    if not rows:
        raise ValueError("dataset contains no prompt rows")

    seen_cases: set[str] = set()
    conversation_turns: dict[str, int] = {}
    for position, row in enumerate(rows, 1):
        if not row["prompt"].strip():
            raise ValueError(f"dataset row {position} has an empty prompt")
        case_id = row.get("case_id", "").strip() or f"case-{position:04d}"
        if case_id in seen_cases:
            raise ValueError(f"duplicate case_id: {case_id}")
        seen_cases.add(case_id)
        conversation_id = row.get("conversation_id", "").strip() or case_id
        expected_turn = conversation_turns.get(conversation_id, 0) + 1
        raw_turn = row.get("turn_index", "").strip()
        try:
            turn_index = int(raw_turn) if raw_turn else expected_turn
        except ValueError as exc:
            raise ValueError(f"{case_id}: turn_index must be an integer") from exc
        if turn_index != expected_turn:
            raise ValueError(f"{conversation_id}: expected turn {expected_turn}, found {turn_index}")
        conversation_turns[conversation_id] = turn_index
        row["case_id"] = case_id
        row["conversation_id"] = conversation_id
        row["turn_index"] = str(turn_index)
        row["is_continuation"] = "true" if turn_index > 1 else "false"
    return DatasetTable(rows)


def conversations(rows: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    grouped: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
    for row in rows:
        grouped.setdefault(row["conversation_id"], []).append(row)
    return list(grouped.values())
