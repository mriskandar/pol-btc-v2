"""
Logging setup — file + in-memory ring buffer for dashboard display.
"""

import logging
from collections import deque
from datetime import datetime, timezone, timedelta

WIB = timezone(timedelta(hours=7))

# Ring buffer for dashboard log panel (last 50 entries)
_log_buffer: deque = deque(maxlen=50)


class DashboardHandler(logging.Handler):
    """Pushes formatted log records into the ring buffer for the TUI."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.now(WIB).strftime("%H:%M:%S.%f")[:-3]
            msg = f"[{ts}] {record.getMessage()}"
            _log_buffer.append(msg)
        except Exception:
            self.handleError(record)


def get_log_buffer() -> deque:
    return _log_buffer


def setup_logging(headless: bool = False) -> logging.Logger:
    logger = logging.getLogger("polybot")
    logger.setLevel(logging.DEBUG)

    # File handler → bot.log
    fh = logging.FileHandler("bot.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    if headless:
        import sys
        # Standard console handler for headless mode
        sh = logging.StreamHandler(sys.stdout)
        sh.setLevel(logging.INFO)
        sh.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(sh)
    else:
        # Dashboard ring-buffer handler
        dh = DashboardHandler()
        dh.setLevel(logging.INFO)
        logger.addHandler(dh)

    return logger
