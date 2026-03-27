"""CLI entry point for the backtesting engine.

MM strategy (default)
---------------------
  run-backtest --symbol BTCUSDT --days 90
  run-backtest --symbol BTCUSDT --days 90 --spread 0.0004 --inventory 150
  run-backtest --all --days 90
  run-backtest --symbol BTCUSDT --grid
  run-backtest --all --grid --update-params      # writes best to experiment_v6/params.json

Directional strategy (experiment_v7)
-------------------------------------
  run-backtest --directional --symbol BTCUSDT --days 90
  run-backtest --directional --all --days 90
  run-backtest --directional --symbol BTCUSDT --grid    # grid over momentum/vol thresholds
  run-backtest --directional --all --grid --update-v7-params  # writes best to experiment_v7/params.json

Options
-------
  --symbol   SYMBOL         single symbol to backtest (e.g. BTCUSDT)
  --all                     run all active symbols from the strategy symbol_list.csv
                            (v6 list for MM, v7 list for --directional)
  --days     N              days of history (default 90)
  --capital  USDT           initial capital (default 500)
  --no-fetch                skip API fetch, use only cached data

  MM options:
  --spread   PCT            half-spread as decimal (default 0.0003)
  --inventory USDT          max inventory in USDT (default 100)
  --skew     FACTOR         inventory skew factor 0-1 (default 0.5)
  --grid                    grid search over spreads
  --update-params           write best MM params to experiment_v6/params.json

  Directional options:
  --directional             use signal-driven directional strategy (v7)
  --tp    PCT               take-profit % (default 0.008)
  --sl    PCT               stop-loss % (default 0.004)
  --grid                    grid search over momentum_ratio and vol_mult
  --update-v7-params        write best directional params to experiment_v7/params.json
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
from backtest.directional_strategy import (
    run_directional_backtest,
    suggest_directional_params,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

_V6_SYMBOL_LIST   = Path("strategies/experiment_v6/symbol_list.csv")
_V7_SYMBOL_LIST   = Path("strategies/experiment_v7/symbol_list.csv")
_V6_PARAMS_FILE   = Path("strategies/experiment_v6/params.json")
_V7_PARAMS_FILE   = Path("strategies/experiment_v7/params.json")
_GRID_SPREADS     = [0.0002, 0.0003, 0.0004, 0.0005, 0.0006, 0.0008, 0.001]

# Directional grid: (momentum_ratio, vol_mult) combinations
_GRID_DIR = [
    (ratio, vol)
    for ratio in [0.4, 0.5, 0.6, 0.7, 0.8]
    for vol   in [1.0, 1.2, 1.5, 2.0]
]


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


def _write_v7_params(best: dict) -> None:
    """Merge directional backtest-suggested params into experiment_v7/params.json."""
    tunable_keys = {"tp_pct", "sl_pct", "momentum_ratio", "vol_mult", "lookback"}

    if not _V7_PARAMS_FILE.exists():
        print(f"  Warning: {_V7_PARAMS_FILE} not found — cannot update.")
        return

    with _V7_PARAMS_FILE.open(encoding="utf-8") as fh:
        current = json.load(fh)

    changed: list[str] = []
    for k in tunable_keys:
        if k in best and best[k] != current.get(k):
            old = current.get(k)
            current[k] = best[k]
            changed.append(f"{k}: {old} → {best[k]}")

    current["_backtest_note"] = best.get("note", "")

    with _V7_PARAMS_FILE.open("w", encoding="utf-8") as fh:
        json.dump(current, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    if changed:
        print(f"\n  experiment_v7/params.json updated ({_V7_PARAMS_FILE}):")
        for line in changed:
            print(f"    {line}")
    else:
        print(f"\n  params.json already has optimal values — no changes.")


def _run_dir_one(symbol: str, days: int, tp: float, sl: float, capital: float) -> None:
    print(f"\n=== DIRECTIONAL: {symbol} ({days}d) tp={tp*100:.2f}% sl={sl*100:.2f}% ===")
    candles = load_candles(symbol, days=days)
    if not candles:
        print(f"  No data for {symbol} — skipping.")
        return
    result = run_directional_backtest(
        symbol=symbol, candles=candles, tp_pct=tp, sl_pct=sl, initial_capital=capital)
    print(result.summary())
    print(f"  Win rate: {result.win_rate*100:.1f}%  Sharpe: {result.sharpe:.3f}")


def _run_dir_grid(symbol: str, days: int, tp: float, sl: float, capital: float,
                  update_v7: bool = False) -> list[BacktestResult]:
    print(f"\n=== DIRECTIONAL GRID: {symbol} ({days}d) ===")
    candles = load_candles(symbol, days=days)
    if not candles:
        print(f"  No data for {symbol}.")
        return []
    results = []
    for momentum_ratio, vol_mult in _GRID_DIR:
        r = run_directional_backtest(
            symbol=symbol, candles=candles,
            tp_pct=tp, sl_pct=sl,
            momentum_ratio=momentum_ratio, vol_mult=vol_mult,
            initial_capital=capital,
        )
        results.append(r)
        if r.n_trades >= 5:
            print(f"  mom={momentum_ratio:.1f}  vol_x={vol_mult:.1f}  "
                  f"net_pnl={r.net_pnl:+.4f}  trades={r.n_trades}  "
                  f"win={r.win_rate*100:.0f}%  sharpe={r.sharpe:.3f}")

    if not results:
        return []

    best = suggest_directional_params(results)
    print("\nBest directional params:")
    for k, v in best.items():
        print(f"  {k}: {v}")

    if update_v7:
        _write_v7_params(best)

    return results


def _load_active_symbols(csv_path: Path) -> list[str]:
    if not csv_path.exists():
        return []
    syms: list[str] = []
    with csv_path.open(encoding="utf-8") as fh:
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
    parser = argparse.ArgumentParser(description="Market making / directional backtest")
    parser.add_argument("--symbol",           default="BTCUSDT")
    parser.add_argument("--all",              action="store_true")
    parser.add_argument("--days",             type=int,   default=90)
    parser.add_argument("--capital",          type=float, default=500.0)
    parser.add_argument("--grid",             action="store_true")
    parser.add_argument("--no-fetch",         action="store_true")
    # ── MM-specific ───────────────────────────────────────────────────────────
    parser.add_argument("--spread",           type=float, default=0.0003)
    parser.add_argument("--inventory",        type=float, default=100.0)
    parser.add_argument("--skew",             type=float, default=0.5)
    parser.add_argument("--update-params",    action="store_true",
                        help="Write best MM grid params to experiment_v6/params.json")
    # ── Directional-specific ──────────────────────────────────────────────────
    parser.add_argument("--directional",      action="store_true",
                        help="Use signal-driven directional strategy (experiment_v7)")
    parser.add_argument("--tp",               type=float, default=0.008,
                        help="Take-profit pct for directional (default 0.008)")
    parser.add_argument("--sl",               type=float, default=0.004,
                        help="Stop-loss pct for directional (default 0.004)")
    parser.add_argument("--update-v7-params", action="store_true",
                        help="Write best directional grid params to experiment_v7/params.json")
    args = parser.parse_args()

    if args.update_params and not args.grid:
        parser.error("--update-params requires --grid")
    if args.update_v7_params and not (args.grid and args.directional):
        parser.error("--update-v7-params requires --directional --grid")

    sym_csv = _V7_SYMBOL_LIST if args.directional else _V6_SYMBOL_LIST
    symbols = _load_active_symbols(sym_csv) if args.all else [args.symbol]

    # ── Directional path ──────────────────────────────────────────────────────
    if args.directional:
        all_dir: list[BacktestResult] = []
        for sym in symbols:
            if args.grid:
                r = _run_dir_grid(
                    sym, args.days, args.tp, args.sl, args.capital,
                    update_v7=args.update_v7_params and len(symbols) == 1,
                )
                all_dir.extend(r)
            else:
                _run_dir_one(sym, args.days, args.tp, args.sl, args.capital)

        if args.grid and args.update_v7_params and len(symbols) > 1 and all_dir:
            print("\n=== AGGREGATE: choosing best directional params across all symbols ===")
            best = suggest_directional_params(all_dir)
            print("Best aggregated params:")
            for k, v in best.items():
                print(f"  {k}: {v}")
            _write_v7_params(best)
        return

    # ── MM path (default) ─────────────────────────────────────────────────────
    all_results: list[BacktestResult] = []
    for sym in symbols:
        if args.grid:
            sym_results = _run_grid(
                sym, args.days, args.inventory, args.skew, args.capital,
                update_params=args.update_params and len(symbols) == 1,
            )
            all_results.extend(sym_results)
        else:
            _run_one(sym, args.days, args.spread, args.inventory, args.skew, args.capital)

    if args.grid and args.update_params and len(symbols) > 1 and all_results:
        print("\n=== AGGREGATE: choosing best MM params across all symbols ===")
        best = suggest_params(all_results)
        print("Best aggregated params:")
        for k, v in best.items():
            print(f"  {k}: {v}")
        _write_params(best)


if __name__ == "__main__":
    main()
