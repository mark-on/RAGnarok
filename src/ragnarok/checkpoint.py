from __future__ import annotations

import json
import os
from pathlib import Path

from .schemas import CaseState, utc_now


class CheckpointStore:
    def __init__(self, path: Path):
        self.path = path
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"cases": {}, "updated_at": utc_now()}

    def state(self, case_id: str) -> str:
        return self.data["cases"].get(case_id, {}).get("state", CaseState.PENDING.value)

    def result(self, case_id: str) -> dict | None:
        return self.data["cases"].get(case_id, {}).get("result")

    def update(self, case_id: str, state: CaseState, result: dict | None = None) -> None:
        self.data["cases"][case_id] = {"state": state.value, "result": result, "updated_at": utc_now()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = utc_now()
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

