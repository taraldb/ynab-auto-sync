from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class JsonStateStore:
    """Crash-safe JSON key-value file: writes go to a temp file in the same
    directory and are atomically renamed into place, so a process killed
    mid-write never leaves a corrupt or partially-written state file behind.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open("r") as f:
            return json.load(f)

    def write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise

    def update(self, **changes: Any) -> dict[str, Any]:
        data = self.read()
        data.update(changes)
        self.write(data)
        return data
