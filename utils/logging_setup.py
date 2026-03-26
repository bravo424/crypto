"""Shared logging configuration for all strategies and tools.

Writes logs to both the console and a daily-rotating file under logs/.

Rotation schedule
-----------------
Files rotate at **00:00 HKT (UTC+8)** which is **16:00 UTC** the previous day.
Python's TimedRotatingFileHandler works in local time by default; using
``utc=True`` plus an ``atTime`` of ``datetime.time(16, 0)`` makes the rollover
happen at exactly 16:00 UTC = 00:00 HKT regardless of the server's local
timezone.

Log file naming
---------------
  logs/<strategy_name>_<YYYY-MM-DD>.log      (rotated files)
  logs/<strategy_name>.log                   (current file, symlinked/renamed)

Up to 30 days of rotated files are kept before the oldest is deleted.

Usage
-----
  from utils.logging_setup import setup_logging
  setup_logging("experiment_v5", debug=False)
"""
from __future__ import annotations

import datetime
import logging
import logging.handlers
from pathlib import Path

_LOGS_DIR   = Path("logs")
_KEEP_DAYS  = 30
_FMT        = "%(asctime)s %(levelname)s %(name)s %(message)s"
_DATE_FMT   = "%Y-%m-%d %H:%M:%S"

# 00:00 HKT  =  16:00 UTC  (UTC+8 offset is 8 h; midnight HKT = 16:00 previous UTC day)
_ROLLOVER_UTC = datetime.time(16, 0)

_NOISY_LOGGERS = (
    "urllib3", "pybit", "requests", "httpcore", "httpx", "websocket",
)


def setup_logging(strategy_name: str, *, debug: bool = False) -> None:
    """Configure root logger with a console handler and a daily file handler.

    Parameters
    ----------
    strategy_name:
        Used as the log file stem, e.g. ``"experiment_v5"`` →
        ``logs/experiment_v5.log``.
    debug:
        If True, set root level to DEBUG; otherwise INFO.
    """
    _LOGS_DIR.mkdir(exist_ok=True)

    level = logging.DEBUG if debug else logging.INFO

    root = logging.getLogger()
    if root.handlers:
        # Already configured (e.g. test harness) — just adjust level.
        root.setLevel(level)
        return

    root.setLevel(level)
    formatter = logging.Formatter(_FMT, datefmt=_DATE_FMT)

    # ── Console handler ───────────────────────────────────────────────────────
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # ── Daily rotating file handler (rotates at 00:00 HKT = 16:00 UTC) ───────
    log_path = _LOGS_DIR / f"{strategy_name}.log"
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(log_path),
        when="midnight",
        interval=1,
        backupCount=_KEEP_DAYS,
        encoding="utf-8",
        utc=True,           # interpret atTime as UTC
        atTime=_ROLLOVER_UTC,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    # Suffix gives rotated files a clear date stamp: experiment_v5.log.2026-03-25
    file_handler.suffix = "%Y-%m-%d"
    root.addHandler(file_handler)

    # ── Silence noisy third-party libraries ──────────────────────────────────
    for lib in _NOISY_LOGGERS:
        logging.getLogger(lib).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "Logging initialised | strategy=%s level=%s log_file=%s rotate=00:00 HKT",
        strategy_name,
        logging.getLevelName(level),
        log_path,
    )
