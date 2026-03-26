"""CLI entry point for the backtesting engine.

Usage
-----
  run-backtest --symbol BTCUSDT --days 90
  run-backtest --symbol BTCUSDT --days 90 --spread 0.0004 --inventory 150
  run-backtest --all --days 90                   # run all symbols in symbol_list.csv
  run-backtest --symbol BTCUSDT --grid           # grid search over spreads
  run-backtest --all --grid --update-params      # grid all symbols + write best to params.json

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
  --update-params           after grid search write best params to experiment_v6/params.json
  --no-fetch                skip API fetch, use only cached data
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from backtest.data_loader import load_candles
from backtest.mm_strategy import BacktestResult, run_mm_backtest, suggest_params

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

_SYMBOL_LIST_CSV  = Path("market_data/symbol_list.csv")
_V6_PARAMS_FILE   = Path("strategies/experiment_v6/params.json")
_GRID_SPREADS     = [0.0002, 0.0003, 0.0004, 0.0005, 0.0006, 0.0008, 0.001]


def _write_params(best: dict) -> None:
    """Merge backtest-suggested params into experiment_v6/params.json.

    Only the tunable keys produced by the backtest are updated; all other
    keys (circuit breakers, WS settings, dry_run, etc.) are preserved.
    Adds a _backtest_note key so you can trace where the values came from.
    """
    tunable_keys = {
        "half_spread_pct",
        "max_inventory_notional",
        "inventory_skew_factor",
    }

    if not _V6_PARAMS_FILE.exists():
        print(f"  Warning: {_V6_PARAMS_FILE} not found — cannot update.")
        return

    with _V6_PARAMS_FILE.open(encoding="utf-8") as fh:
        current = json.load(fh)

    changed: list[str] = []
    for k in tunable_keys:
        if k in best and best[k] != current.get(k):
            old = current.get(k)
            current[k] = best[k]
            changed.append(f"{k}: {old} → {best[k]}")

    current["_backtest_note"] = best.get("note", "")

    with _V6_PARAMS_FILE.open("w", encoding="utf-8") as fh:
        json.dump(current, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    if changed:
        print(f"\n  params.json updated ({_V6_PARAMS_FILE}):")
        for line in changed:
            print(f"    {line}")
    else:
        print(f"\n  params.json already has optimal values — no changes made.")


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
              skew: float, capital: float,
              update_params: bool = False) -> list[BacktestResult]:
    """Run grid search over spreads and return all results."""
    print(f"\n=== GRID SEARCH: {symbol} ({days}d) ===")
    candles = load_candles(symbol, days=days)
    if not candles:
        print(f"  No data for {symbol}.")
        return []
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
    print("\nSuggested live params:")
    for k, v in best.items():
        print(f"  {k}: {v}")

    if update_params:
        _write_params(best)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Market making backtest")
    parser.add_argument("--symbol",        default="BTCUSDT")
    parser.add_argument("--all",           action="store_true")
    parser.add_argument("--days",          type=int,   default=90)
    parser.add_argument("--spread",        type=float, default=0.0003)
    parser.add_argument("--inventory",     type=float, default=100.0)
    parser.add_argument("--skew",          type=float, default=0.5)
    parser.add_argument("--capital",       type=float, default=500.0)
    parser.add_argument("--grid",          action="store_true")
    parser.add_argument("--update-params", action="store_true",
                        help="Write best grid params to strategies/experiment_v6/params.json")
    parser.add_argument("--no-fetch",      action="store_true")
    args = parser.parse_args()

    if args.update_params and not args.grid:
        parser.error("--update-params requires --grid")

    symbols = _load_active_symbols() if args.all else [args.symbol]

    all_results: list[BacktestResult] = []
    for sym in symbols:
        if args.grid:
            # Pass update_params=False for individual symbols when --all is used;
            # we aggregate first and write once at the end.
            sym_results = _run_grid(
                sym, args.days, args.inventory, args.skew, args.capital,
                update_params=args.update_params and len(symbols) == 1,
            )
            all_results.extend(sym_results)
        else:
            _run_one(sym, args.days, args.spread, args.inventory, args.skew, args.capital)

    # When --all --grid --update-params: aggregate results across symbols and write once.
    if args.grid and args.update_params and len(symbols) > 1 and all_results:
        print("\n=== AGGREGATE: choosing best params across all symbols ===")
        best = suggest_params(all_results)
        print("Best aggregated params:")
        for k, v in best.items():
            print(f"  {k}: {v}")
        _write_params(best)


if __name__ == "__main__":
    main()
