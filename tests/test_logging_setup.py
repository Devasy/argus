import logging
import logging.handlers

from argus.config import Settings
from argus.logging_setup import configure_logging


def test_configure_logging_sets_level_and_stream_handler():
    settings = Settings(database_url="x", gitlab_url="x", gitlab_token="t",
                        log_level="INFO", log_dir=None)
    configure_logging(settings)
    logger = logging.getLogger("argus")
    assert logger.level == logging.INFO
    assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)
    assert logger.propagate is False


def test_configure_logging_info_messages_are_captured(capsys):
    settings = Settings(database_url="x", gitlab_url="x", gitlab_token="t",
                        log_level="INFO", log_dir=None)
    configure_logging(settings)
    logger = logging.getLogger("argus.graphify")
    logger.info("falling back to graphify update --force (code-only)")
    assert "falling back to graphify update --force" in capsys.readouterr().err


def test_configure_logging_adds_rotating_file_handler_when_log_dir_set(tmp_path):
    settings = Settings(database_url="x", gitlab_url="x", gitlab_token="t",
                        log_level="INFO", log_dir=str(tmp_path / "logs"))
    configure_logging(settings)
    logger = logging.getLogger("argus")
    assert any(isinstance(h, logging.handlers.RotatingFileHandler)
              for h in logger.handlers)
    logging.getLogger("argus.runner").info("hello from the rotating handler")
    for h in logger.handlers:
        h.flush()
    assert (tmp_path / "logs" / "argus.log").exists()
    assert "hello from the rotating handler" in (tmp_path / "logs" / "argus.log").read_text()
