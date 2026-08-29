"""One local process lock for a single personal strategy instance."""

from __future__ import annotations

import os
from pathlib import Path


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._held = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RuntimeError(f"strategy instance lock already exists: {self.path}") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(str(os.getpid()))
        self._held = True

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False
