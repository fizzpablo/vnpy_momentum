"""Small atomic strategy state store; it never replaces vn.py OMS state."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("strategy state is unreadable") from exc
        if not isinstance(value, dict):
            raise RuntimeError("strategy state must be a JSON object")
        return value

    def save(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(value, file, sort_keys=True, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise RuntimeError("strategy state cannot be persisted") from exc
