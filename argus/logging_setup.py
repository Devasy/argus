"""Configures the `argus.*` logger namespace once at startup. Without
this, `logger.info(...)` calls across the app are silently dropped by
Python's default root log level (WARNING)."""
import logging
import logging.handlers
from pathlib import Path

from argus.config import Settings

_FMT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def configure_logging(settings: Settings) -> None:
    logger = logging.getLogger("argus")
    logger.setLevel(settings.log_level)
    logger.handlers.clear()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter(_FMT))
    logger.addHandler(stream_handler)

    if settings.log_dir:
        log_dir = Path(settings.log_dir).expanduser()
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "argus.log",
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count)
        file_handler.setFormatter(logging.Formatter(_FMT))
        logger.addHandler(file_handler)

    logger.propagate = False
