"""Standalone market-data daemon: WebSocket + optional JSONL archive only.

Run this in a **separate terminal / systemd / NSSM** from ``run-strat`` so
historical recording continues when a strategy process crashes.

``experiment_v7`` / ``v6`` / ``v5`` keep their **own** WebSocket in-process
because ``SignalStore`` is not shared across processes.  This service is for
**archiving** only; point ``md_record_dir`` to empty in strategy params if
you do not want duplicate JSONL files from the bot.

Examples::

    run-market-data --csv-path strategies/experiment_v7/symbol_list.csv \\
        --record-dir data/md_archive

    # Or rely on .env MARKET_DATA_RECORD_DIR=data/md_archive
    run-market-data --csv-path strategies/experiment_v7/symbol_list.csv
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

import market_data as md

LOGGER = logging.getLogger(__name__)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Bybit linear public WS + archive (no trading)")
    parser.add_argument(
        "--csv-path",
        type=Path,
        default=None,
        help="symbol_list.csv (default: market_data/symbol_list.csv)",
    )
    parser.add_argument(
        "--record-dir",
        type=Path,
        default=None,
        help="JSONL output root (default: env MARKET_DATA_RECORD_DIR, required)",
    )
    parser.add_argument(
        "--book-interval",
        type=float,
        default=2.0,
        help="Minimum seconds between L2 snapshot lines per symbol (default 2)",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    from utils.logging_setup import setup_logging
    setup_logging("market_data_service", debug=args.debug)

    csv_p = args.csv_path
    if csv_p is not None and not csv_p.is_absolute():
        csv_p = Path.cwd() / csv_p

    rd = args.record_dir
    if rd is None:
        ev = os.getenv("MARKET_DATA_RECORD_DIR", "").strip()
        rd = Path(ev) if ev else None
    if rd is not None and str(rd).strip():
        rd = Path(rd)
        if not rd.is_absolute():
            rd = Path.cwd() / rd
    else:
        LOGGER.error(
            "Set --record-dir or MARKET_DATA_RECORD_DIR in the environment "
            "(archive is the point of this daemon).",
        )
        sys.exit(1)

    stop = threading.Event()

    def _shutdown(signum, frame):  # noqa: ANN001
        LOGGER.info("Signal %s — stopping WebSocket …", signum)
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    md.start_market_data(
        csv_path=csv_p,
        record_dir=rd,
        book_snapshot_interval_sec=args.book_interval,
    )
    LOGGER.info(
        "market_data service running | csv=%s | archive=%s | Ctrl+C to stop",
        csv_p or "(package default)",
        rd,
    )

    try:
        while not stop.is_set():
            time.sleep(0.5)
    finally:
        md.stop_market_data()
        LOGGER.info("market_data service stopped.")


if __name__ == "__main__":
    main()
