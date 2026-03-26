"""CLI entry point for the backtesting engine.

Usage
-----
  run-backtest --symbol BTCUSDT --days 90
  run-backtest --symbol BTCUSDT --days 90 --spread 0.0004 --inventory 150
  run-backtest --all --days 90                   # run all symbols in symbol_list.csv
  run-backtest --symbol BTCUSDT --grid           # grid search over spreads

Options
-------
  --symbol   SYMBOL         single symbol to backtest (e.g. BTCUSDT)
  --all                     run all active symbols from market_data/symbol_list.csv
  --days     N              days of history (default 90)
  --spread   PCT            half-spread as decimal (default 0.0003 = 0.03%)
  --inventory USDT          max inventory in USDT (default 100)
  --skew     FACTOR         inventory skew factor 0-1 (default 0.5)
  --capital  USDT           initial capital (default 500)
  --grid                    grid search over spreads [0.0002 … 0.0010]
  --no-fetch                skip API fetch, use only cached data
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

from backtest.data_loader import load_candles
from backtest.mm_strategy import run_mm_backtest, suggest_params

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

_SYMBOL_LIST_CSV = Path("market_data/symbol_list.csv")
_GRID_SPREADS    = [0.0002, 0.0003, 0.0004, 0.0005, 0.0006, 0.0008, 0.001]


def _load_active_symbols() -> list[str]:
    if not _SYMBOL_LIST_CSV.exists():
        return []
    syms: list[str] = []
    with _SYMBOL_LIST_CSV.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("active", "true").strip().lower() in ("true", "1", "yes"):
                syms.append(row["symbol"].strip())
    return syms


def _run_one(symbol: str, days: int, spread: float, inventory: float,
             skew: float, capital: float) -> None:
    print(f"\n=== {symbol} ({days}d) spread={spread*100:.3f}% "
          f"inventory={inventory:.0f} USDT ===")
    candles = load_candles(symbol, days=days)
    if not candles:
        print(f"  No data for {symbol} — skipping.")
        return
    result = run_mm_backtest(
        symbol=symbol,
        candles=candles,
        half_spread_pct=spread,
        max_inventory_usd=inventory,
        inventory_skew_factor=skew,
        initial_capital=capital,
    )
    print(result.summary())


def _run_grid(symbol: str, days: int, inventory: float,
              skew: float, capital: float) -> None:
    print(f"\n=== GRID SEARCH: {symbol} ({days}d) ===")
    candles = load_candles(symbol, days=days)
    if not candles:
        print(f"  No data for {symbol}.")
        return
    results = []
    for spread in _GRID_SPREADS:
        r = run_mm_backtest(symbol=symbol, candles=candles,
                            half_spread_pct=spread,
                            max_inventory_usd=inventory,
                            inventory_skew_factor=skew,
                            initial_capital=capital)
        results.append(r)
        print(f"  spread={spread*100:.3f}%  net_pnl={r.net_pnl:+.4f}  "
              f"trades={r.n_trades}  sharpe={r.sharpe:.3f}")

    best = suggest_params(results)
    print("\nSuggested live params for experiment_v6/params.json:")
    for k, v in best.items():
        print(f"  {k}: {v}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Market making backtest")
    parser.add_argument("--symbol",    default="BTCUSDT")
    parser.add_argument("--all",       action="store_true")
    parser.add_argument("--days",      type=int,   default=90)
    parser.add_argument("--spread",    type=float, default=0.0003)
    parser.add_argument("--inventory", type=float, default=100.0)
    parser.add_argument("--skew",      type=float, default=0.5)
    parser.add_argument("--capital",   type=float, default=500.0)
    parser.add_argument("--grid",      action="store_true")
    parser.add_argument("--no-fetch",  action="store_true")
    args = parser.parse_args()

    symbols = _load_active_symbols() if args.all else [args.symbol]

    for sym in symbols:
        if args.grid:
            _run_grid(sym, args.days, args.inventory, args.skew, args.capital)
        else:
            _run_one(sym, args.days, args.spread, args.inventory, args.skew, args.capital)


if __name__ == "__main__":
    main()
