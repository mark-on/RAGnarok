from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pandas as pd

from ..schemas import ORIGINAL_COLUMNS


def load_dataset(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, keep_default_na=False, dtype=str)
    validate_contract(frame)
    return frame


def validate_contract(frame: pd.DataFrame) -> None:
    if list(frame.columns) != ORIGINAL_COLUMNS:
        raise ValueError(f"dataset must contain exactly the 17 benchmark columns: {ORIGINAL_COLUMNS}")
    if frame["case_id"].duplicated().any():
        raise ValueError("case_id values must be unique")
    grouped = conversations(frame)
    for conversation_id, rows in grouped:
        indexes = [int(value) for value in rows["turn_index"]]
        if indexes != list(range(1, len(rows) + 1)):
            raise ValueError(f"{conversation_id}: non-sequential turn indexes")
        if rows.iloc[0]["is_continuation"] != "false":
            raise ValueError(f"{conversation_id}: first turn must not be a continuation")
        if any(value != "true" for value in rows.iloc[1:]["is_continuation"]):
            raise ValueError(f"{conversation_id}: later turns must be continuations")


def conversations(frame: pd.DataFrame):
    ordered = frame.assign(_turn=frame["turn_index"].astype(int)).sort_values(["conversation_id", "_turn"])
    return [(key, group.drop(columns="_turn")) for key, group in ordered.groupby("conversation_id", sort=False)]


def apply_filters(frame: pd.DataFrame, **filters) -> pd.DataFrame:
    result = frame
    mapping = {"model": None, "case_id": "case_id", "conversation_id": "conversation_id", "domain": "domain", "attack_vector": "attack_vector"}
    for name, column in mapping.items():
        value = filters.get(name)
        if value and column:
            result = result[result[column] == value]
    limit = filters.get("limit")
    return result.head(limit) if limit else result

