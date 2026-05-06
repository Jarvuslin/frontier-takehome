"""Structured logging setup. JSONL to file + pretty console."""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog


def configure_logging(level: str = "INFO", fmt: str = "json", logs_dir: Path | None = None) -> Path | None:
    """Configure structlog. Returns the JSONL log file path if file logging is enabled."""
    log_path: Path | None = None
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]

    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = logs_dir / f"run-{ts}.jsonl"
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(message)s"))
        handlers.append(fh)

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    if fmt == "console":
        processors.append(structlog.dev.ConsoleRenderer(colors=True))
    else:
        processors.append(structlog.processors.JSONRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    return log_path


def get_logger(name: str = "safco") -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
