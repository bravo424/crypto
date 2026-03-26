"""Fetch and cache 3 months of Bybit 1-minute OHLCV data.

Usage
-----
  from backtest.data_loader import load_candles
  df = load_candles("BTCUSDT", days=90)

Data is saved to data/historical/<symbol>_1m.csv and reused on subsequent
calls (only fetches candles newer than the last cached row).
"""
from __future__ import annotations

import csv
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

LOGGER   = logging.getLogger(__name__)
BASE_URL = "https://api.bybit.com"
DATA_DIR = Path("data/historical")

# Bybit REST rate limit: 120 req/min on public endpoints.
# We wait 0.6s between requests to stay well within limits.
_REQ_SLEEP = 0.6

COLUMNS = ["ts", "open", "high", "low", "close", "volume", "turnover"]


def _bybit_klines(symbol: str, interval: str, start_ms: int,
                  end_ms: int, limit: int = 1000) -> list[list]:
    """Fetch one page of klines from Bybit REST API v5."""
    resp = requests.get(
        f"{BASE_URL}/v5/market/kline",
        params=dict(
            category="linear",
            symbol=symbol,
            interval=interval,
            start=start_ms,
            end=end_ms,
            limit=limit,
        ),
        timeout=15,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit API error: {result}")
    # Returns newest-first; we reverse to oldest-first
    return list(reversed(result["result"]["list"]))


def _last_cached_ts(path: Path) -> int | None:
    """Return the timestamp (ms) of the most recent cached row, or None."""
    if not path.exists():
        return None
    last_ts: int | None = None
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                last_ts = int(row["ts"])
            except (ValueError, KeyError):
                pass
    return last_ts


def load_candles(symbol: str, days: int = 90,
                 interval: str = "1") -> list[dict]:
    """Return a list of OHLCV dicts for `symbol` covering the last `days` days.

    Fetches missing data from Bybit and appends to the local cache file.
    Returns all rows from the cache covering the requested period.

    Parameters
    ----------
    symbol   : e.g. "BTCUSDT"
    days     : number of calendar days of history to ensure
    interval : candle interval in minutes (default "1")
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = DATA_DIR / f"{symbol}_{interval}m.csv"

    now_ms    = int(time.time() * 1000)
    start_target_ms = now_ms - days * 24 * 3600 * 1000

    last_ts = _last_cached_ts(cache_path)
    fetch_from_ms = max(
        start_target_ms,
        (last_ts + 60_000) if last_ts else start_target_ms,
    )

    # Nothing to fetch — cache already covers the period
    if last_ts and last_ts >= now_ms - 2 * 60_000:
        LOGGER.info("Cache up-to-date for %s (%s)", symbol, cache_path)
    else:
        LOGGER.info("Fetching %s from %s …", symbol,
                    datetime.fromtimestamp(fetch_from_ms / 1000, tz=timezone.utc))
        _fetch_and_append(symbol, interval, fetch_from_ms, now_ms, cache_path)

    return _read_cache(cache_path, start_target_ms)


def _fetch_and_append(symbol: str, interval: str,
                      from_ms: int, to_ms: int, path: Path) -> None:
    """Page through Bybit kline API and append new rows to the CSV."""
    interval_ms   = int(interval) * 60_000
    page_span_ms  = 1000 * interval_ms        # 1000 candles per page
    write_header  = not path.exists()

    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if write_header:
            writer.writerow(COLUMNS)

        cur_start = from_ms
        total_written = 0
        while cur_start < to_ms:
            cur_end = min(cur_start + page_span_ms, to_ms)
            try:
                rows = _bybit_klines(symbol, interval, cur_start, cur_end)
            except Exception as exc:
                LOGGER.warning("Fetch error for %s at %d: %s — retrying in 5s",
                               symbol, cur_start, exc)
                time.sleep(5)
                continue

            for row in rows:
                ts_ms = int(row[0])
                if ts_ms < from_ms:
                    continue
                writer.writerow([
                    ts_ms,
                    row[1],   # open
                    row[2],   # high
                    row[3],   # low
                    row[4],   # close
                    row[5],   # volume
                    row[6],   # turnover
                ])
                total_written += 1

            cur_start = cur_end + interval_ms
            time.sleep(_REQ_SLEEP)

    LOGGER.info("Fetched %d candles for %s → %s", total_written, symbol, path)


def _read_cache(path: Path, since_ms: int) -> list[dict]:
    """Read cached rows since `since_ms` and return as list of dicts."""
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                ts = int(row["ts"])
            except (ValueError, KeyError):
                continue
            if ts >= since_ms:
                rows.append({
                    "ts":       ts,
                    "open":     float(row["open"]),
                    "high":     float(row["high"]),
                    "low":      float(row["low"]),
                    "close":    float(row["close"]),
                    "volume":   float(row["volume"]),
                    "turnover": float(row["turnover"]),
                })
    return rows
