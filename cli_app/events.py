from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


class ProgressReporter:
    """Human-readable progress plus one searchable JSONL event stream."""

    _write_lock = Lock()

    def __init__(self, layer: str, data_dir: Path, workflow_id: str | None = None) -> None:
        self.layer = layer
        self.workflow_id = workflow_id
        self.path = data_dir / "logs" / "runtime_events.jsonl"

    async def __call__(self, message: str) -> None:
        print(f"[{self.layer}] {message}")
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "layer": self.layer,
            "workflow_id": self.workflow_id,
            "message": message,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
