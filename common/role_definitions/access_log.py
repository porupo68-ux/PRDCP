from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


_APPEND_LOCK = RLock()


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
            payload = line.encode("utf-8", errors="strict")
            flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
            with _APPEND_LOCK:
                descriptor = os.open(self.path, flags, 0o666)
                try:
                    written = os.write(descriptor, payload)
                    if written != len(payload):
                        raise OSError(
                            f"incomplete RD access log append: {written}/{len(payload)} bytes"
                        )
                finally:
                    os.close(descriptor)
