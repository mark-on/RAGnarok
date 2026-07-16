from __future__ import annotations

import json
import os
from pathlib import Path

from .schemas import LifecycleState, utc_now


class Lifecycle:
    def __init__(self, path: Path):
        self.path = path

    def set(self, state: LifecycleState, **details) -> dict:
        payload = {"state": state.value, "updated_at": utc_now(), **details}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
        return payload

    def get(self) -> dict:
        if not self.path.exists():
            return {"state": LifecycleState.SETTING_UP.value}
        return json.loads(self.path.read_text(encoding="utf-8"))

