"""JSONL logging with bounded rotation and conservative secret redaction."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)(\s*[=:]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
)


def redact(value: str) -> str:
    value = _SECRET_PATTERNS[0].sub(r"\1\2[REDACTED]", value)
    return _SECRET_PATTERNS[1].sub(r"\1[REDACTED]", value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in ("event", "component", "correlation_id", "job_id", "status"):
            value = getattr(record, key, None)
            if value is not None:
                entry[key] = redact(str(value))
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))


def configure_logging(log_dir: str | Path, level: str = "INFO", *, max_bytes: int = 1_048_576, backup_count: int = 5) -> logging.Logger:
    normalized = level.upper()
    if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError(f"Niveau de journalisation invalide : {level!r}")
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("invest_ai")
    logger.setLevel(normalized)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    file_handler = RotatingFileHandler(directory / "invest-ai.jsonl", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)
    return logger
