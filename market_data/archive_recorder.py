"""Append-only JSONL archive of live trades + throttled top-of-book snapshots.

Bybit (and most venues) do **not** expose ~1 year of historical L2 books or
tick trades on the public REST API.  To research microstructure you either:

- buy vendor data (Kaiko, Tardis, etc.), or
- **record forward** from the WebSocket (this module).

Layout under ``base_dir``::

    <base_dir>/<SYMBOL>/trades_YYYY-MM-DD.jsonl
    <base_dir>/<SYMBOL>/book_YYYY-MM-DD.jsonl

Each line is one JSON object.  Book lines are emitted at most once per
``book_interval_sec`` per symbol to limit disk use.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)


class ArchiveRecorder:
    def __init__(self, base_dir: Path, *, book_interval_sec: float = 2.0) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self._book_interval = max(0.5, float(book_interval_sec))
        self._lock = threading.Lock()
        self._last_book_mono: dict[str, float] = {}

    def _day_path(self, symbol: str, kind: str) -> Path:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        d = self._base / symbol
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{kind}_{day}.jsonl"

    def log_trade(self, symbol: str, side: str, qty: float, price: float,
                  ts: float) -> None:
        line = json.dumps(
            {"ts": ts, "side": side, "qty": qty, "price": price},
            separators=(",", ":"),
        )
        path = self._day_path(symbol, "trades")
        try:
            with self._lock:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:
            LOGGER.warning("archive trade write failed: %s", exc)

    def log_book_throttled(self, symbol: str, bids: list, asks: list) -> None:
        now = time.monotonic()
        with self._lock:
            last = self._last_book_mono.get(symbol, 0.0)
            if now - last < self._book_interval:
                return
            self._last_book_mono[symbol] = now
        # Keep top 5 levels only (already what orderbook.50 uses at wire)
        snap = {
            "ts": time.time(),
            "b": bids[:5] if isinstance(bids, list) else bids,
            "a": asks[:5] if isinstance(asks, list) else asks,
        }
        line = json.dumps(snap, separators=(",", ":"))
        path = self._day_path(symbol, "book")
        try:
            with self._lock:
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:
            LOGGER.warning("archive book write failed: %s", exc)
