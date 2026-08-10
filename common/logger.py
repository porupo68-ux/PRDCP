from __future__ import annotations

import logging
import logging.config
from pathlib import Path


def configure_logging(level: str = "INFO", *, data_dir: Path | None = None) -> None:
    """Configure one consistent console/file format for all PRDCP layers."""

    normalized_level = level.upper()
    handlers: dict = {
        "console": {
            "class": "logging.StreamHandler",
            "level": normalized_level,
            "formatter": "standard",
            "stream": "ext://sys.stderr",
        }
    }
    root_handlers = ["console"]
    if data_dir is not None:
        log_dir = data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "level": normalized_level,
            "formatter": "standard",
            "filename": str(log_dir / "application.log"),
            "encoding": "utf-8",
            "maxBytes": 2_000_000,
            "backupCount": 3,
        }
        root_handlers.append("file")

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
                }
            },
            "handlers": handlers,
            "root": {"level": normalized_level, "handlers": root_handlers},
        }
    )
