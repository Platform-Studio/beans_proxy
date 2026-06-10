"""Logging setup for Beans Proxy."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOGGER_NAME = "beans_proxy"
_DEFAULT_FORMAT = (
    "%(asctime)s %(levelname)s %(name)s - %(message)s"
)


def configure_logging(log_file: str | Path, level: int = logging.INFO) -> logging.Logger:
    """Configure the package logger with a rotating file handler.

    Returns the configured logger.
    """
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    # Idempotent: clear any existing handlers we've added before.
    for handler in list(logger.handlers):
        if getattr(handler, "_beans_proxy_owned", False):
            logger.removeHandler(handler)

    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
    handler._beans_proxy_owned = True  # type: ignore[attr-defined]
    logger.addHandler(handler)

    # Also tee to stderr at INFO for dev visibility.
    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
    stream._beans_proxy_owned = True  # type: ignore[attr-defined]
    logger.addHandler(stream)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)
