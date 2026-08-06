"""Local logging that avoids credentials and rotates files."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(data_directory: Path, log_level: str) -> logging.Logger:
    """Configure an application-owned logger without logging configuration secrets."""

    logs_directory = data_directory / "logs"
    logs_directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("funpay_operations")
    logger.setLevel(log_level)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            logs_directory / "application.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    return logger
