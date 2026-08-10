"""Logging setup.

One thing worth being deliberate about: a trading log is evidence. When a
position turns out wrong, the log is the only record of what the bot believed
at the moment it acted. So calculations are logged at the point of decision,
not reconstructed afterwards, and the file handler is never quiet about errors.

Secrets never reach here. `Secrets.__repr__` is redacted precisely so that an
accidental `log.debug("%s", secrets)` cannot leak a password into a file that
outlives the process.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-12s %(message)s"
_FILE_FORMAT = "%(asctime)s %(levelname)-7s %(name)-16s %(funcName)s:%(lineno)d %(message)s"

_configured = False


def setup(log_dir: Path, level: int = logging.INFO, filename: str = "bot.log") -> logging.Logger:
    """Configure root logging once. Safe to call repeatedly."""
    global _configured
    root = logging.getLogger()
    if _configured:
        return root

    log_dir.mkdir(parents=True, exist_ok=True)
    root.setLevel(logging.DEBUG)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT, datefmt="%H:%M:%S"))
    root.addHandler(console)

    # 10 MB x 5 keeps roughly a fortnight of a chatty daemon without unbounded
    # growth on a VPS with a small disk.
    file_handler = RotatingFileHandler(
        log_dir / filename, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    root.addHandler(file_handler)

    # httpx logs every Telegram request at INFO. That is noise here, and it
    # puts the bot token's URL in the log file.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _configured = True
    return root


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)
