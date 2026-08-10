from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def replace_with_retry(
    temporary_name: str,
    path: Path,
    *,
    attempts: int = 10,
    delay_seconds: float = 0.05,
) -> None:
    for attempt in range(attempts):
        try:
            os.replace(temporary_name, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay_seconds)


class JsonRepository:
    @staticmethod
    def write_json_atomic(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )

        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()

                if os.name != "nt":
                    os.fsync(handle.fileno())

            replace_with_retry(temporary_name, path)

        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def write_text_atomic(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )

        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()

                if os.name != "nt":
                    os.fsync(handle.fileno())

            replace_with_retry(temporary_name, path)

        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
