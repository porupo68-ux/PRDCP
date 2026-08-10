from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


class RDAccessLog:
    """Append-only audit log containing RD identity, never the complete RD body."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.metrics: Counter[str] = Counter()
        self._lock = RLock()

    def record(self, event: str, **fields: Any) -> None:
        self.metrics[event] += 1
        if self.path is None:
            return
        record = {
            "event": event,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(line)
