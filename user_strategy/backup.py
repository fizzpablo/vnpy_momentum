"""Small local state backup used before startup and at clean shutdown."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from shutil import copy2


def backup_state(state_path: Path, backup_dir: Path, log_dir: Path | None = None) -> Path | None:
    """Copy state and optional current logs without ever deleting a previous backup."""
    if not state_path.exists() and not log_dir:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"{state_path.name}.{stamp}.bak"
    if state_path.exists():
        copy2(state_path, target)
    if log_dir and log_dir.is_dir():
        log_backup = backup_dir / f"logs-{stamp}"
        log_backup.mkdir()
        for source in log_dir.glob("*.log"):
            copy2(source, log_backup / source.name)
    return target
