# ── First-time setup ─────────────────────────────────────────────────────────

# Install / re-install the package and all dependencies (run once, or after pulling changes)
pip install -e .
pip install PyJWT

# ── experiment_v1 — Bybit (default) ──────────────────────────────────────────

# Run live (continuous, checks every 60 s)
run-strat experiment_v1

# One cycle only (smoke-test without leaving it running)
run-strat experiment_v1 --once

# Send an immediate position-update alert to @position_update_srabot
run-strat experiment_v1 --update

# Cancel every open (unfilled) order
run-strat experiment_v1 --cancel-all

# Close all open positions at market and wipe local state
run-strat experiment_v1 --close-all

# ── experiment_v1 — Upbit (spot, long-only) ───────────────────────────────────

run-strat experiment_v1 --exchange upbit
run-strat experiment_v1 --exchange upbit --once
run-strat experiment_v1 --exchange upbit --update
run-strat experiment_v1 --exchange upbit --cancel-all
run-strat experiment_v1 --exchange upbit --close-all

# ── experiment_v1 — Bithumb (spot, long-only) ────────────────────────────────

run-strat experiment_v1 --exchange bithumb
run-strat experiment_v1 --exchange bithumb --once
run-strat experiment_v1 --exchange bithumb --update
run-strat experiment_v1 --exchange bithumb --cancel-all
run-strat experiment_v1 --exchange bithumb --close-all

# ── experiment_v2 — Bybit (USDT-perp, 1H wick-reversal) ─────────────────────

# Run live (continuous, checks every 60 s)
run-strat experiment_v2

# One cycle only
run-strat experiment_v2 --once

# Send position-update alert
run-strat experiment_v2 --update

# Cancel every open (unfilled) order
run-strat experiment_v2 --cancel-all

# Close all open positions at market and wipe local state
run-strat experiment_v2 --close-all

# ── experiment_v2 — Bithumb (KRW spot, long-only) ────────────────────────────

run-strat experiment_v2 --exchange bithumb
run-strat experiment_v2 --exchange bithumb --once
run-strat experiment_v2 --exchange bithumb --update
run-strat experiment_v2 --exchange bithumb --cancel-all
run-strat experiment_v2 --exchange bithumb --close-all

# ── experiment_v3 — Bybit (USDT-perp, 1-min ATR trailing-queue scalper) ──────
#
#   Concept: 369 EMA alignment + volume spike → post a PostOnly limit order
#   TRAIL_OFFSET_ATR × ATR below (long) or above (short) the mark price.
#   Transparently refreshes stale orders every 15 s.  ATR-based TP/SL (2:1 R:R).
#   Signal expires after 5 min without a fill.  Never pays taker fees.
#
#   Key config: strategies/experiment_v3/params.json
#               strategies/experiment_v3/symbols_bybit.json

# Run live (continuous, checks every 15 s)
run-strat experiment_v3

# One cycle only (smoke-test)
run-strat experiment_v3 --once

# Send immediate position-update alert
run-strat experiment_v3 --update

# Cancel every open (unfilled) order
run-strat experiment_v3 --cancel-all

# Close all open positions at market and wipe local state
run-strat experiment_v3 --close-all

# ── experiment_v4 — Dual-TF scalp-reversal (Bybit + Bithumb) ─────────────────
#
#   Key config: strategies/experiment_v4/params.json
#               strategies/experiment_v4/symbols_bybit.json
#               strategies/experiment_v4/symbols_bithumb.json

# Bybit live
run-strat experiment_v4 --exchange bybit

# Bithumb live
run-strat experiment_v4 --exchange bithumb

# One cycle only
run-strat experiment_v4 --exchange bybit --once
run-strat experiment_v4 --exchange bithumb --once

# Send immediate position-update alert
run-strat experiment_v4 --exchange bybit --update
run-strat experiment_v4 --exchange bithumb --update

# Cancel every open (unfilled) order
run-strat experiment_v4 --exchange bybit --cancel-all
run-strat experiment_v4 --exchange bithumb --cancel-all

# Close all open positions at market and wipe local state
run-strat experiment_v4 --exchange bybit --close-all
run-strat experiment_v4 --exchange bithumb --close-all

# ── experiment_v5 — Conservative profile + runtime tuning (Bybit + Bithumb) ──
#
#   Key config: strategies/experiment_v5/params.json
#               strategies/experiment_v5/symbols_bybit.json
#               strategies/experiment_v5/symbols_bithumb.json
#
#   v5 supports runtime overrides without editing params.json:
#   --set key=value
#   --set exchanges.bybit.key=value
#   --set exchanges.bithumb.key=value
#
#   Common tuning keys:
#   entry_aggressiveness, vmult_scale, rsi_relax_step, bb_std_scale
#
#   Notes:
#   - Repeat --set multiple times for multiple overrides.
#   - Overrides are hot-reload-safe (applied every cycle).
#   - Restart strategy process after code/param changes.

# Bybit live (default conservative)
run-strat experiment_v5 --exchange bybit

# Bithumb live (default conservative)
run-strat experiment_v5 --exchange bithumb

# Bybit balanced profile (more entries than default)
run-strat experiment_v5 --exchange bybit --set entry_aggressiveness=1

# Bybit active profile (even more entries)
run-strat experiment_v5 --exchange bybit --set entry_aggressiveness=2 --set vmult_scale=0.9 --set rsi_relax_step=2.5

# Exchange-scoped override example (from one launch command)
run-strat experiment_v5 --exchange bybit --set exchanges.bybit.entry_aggressiveness=2

# One cycle only
run-strat experiment_v5 --exchange bybit --once
run-strat experiment_v5 --exchange bithumb --once

# Send immediate position-update alert
run-strat experiment_v5 --exchange bybit --update
run-strat experiment_v5 --exchange bithumb --update

# Cancel every open (unfilled) order
run-strat experiment_v5 --exchange bybit --cancel-all
run-strat experiment_v5 --exchange bithumb --cancel-all

# Close all open positions at market and wipe local state
run-strat experiment_v5 --exchange bybit --close-all
run-strat experiment_v5 --exchange bithumb --close-all

# Clear loss-streak pause immediately (no restart needed, bot resumes next cycle)
run-strat experiment_v5 --exchange bybit --clear-pause
run-strat experiment_v5 --exchange bithumb --clear-pause

# ── experiment_v6 — Signal-enhanced market making (Bybit USDT-perp) ──────────
#
#   Concept: posts PostOnly bid + ask every 2s based on live order-book
#   imbalance and trade-flow signals.  Uses inventory skew to stay balanced.
#   Requires market_data WebSocket feed (starts automatically).
#
#   Key config: strategies/experiment_v6/params.json
#               market_data/symbol_list.csv
#
#   Fee viability (VIP 0): half_spread must be > 0.02% (break-even).
#   Default half_spread = 0.04% (full spread 0.08% — net ~0.04% per round trip).
#
#   Position management (TP / SL / timeout)
#   ─────────────────────────────────────────
#   Every open inventory position is actively managed each cycle:
#
#   tp_pct        (default 0.008 = 0.8%)
#     When unrealised PnL reaches +0.8%, cancel open quotes and place a
#     PostOnly limit close at best ask/bid to collect the maker rebate.
#     Quoting for the symbol is paused until the TP order resolves.
#
#   sl_pct        (default 0.005 = 0.5%)
#     When unrealised PnL drops to -0.5%, immediately close at market (IOC).
#     All open quotes for the symbol are cancelled first.
#
#   max_hold_min  (default 30)
#     If a position has been open for 30 minutes without resolving,
#     close at market regardless of PnL.  Prevents holding stale inventory
#     through major market moves.
#
#   Tune these in strategies/experiment_v6/params.json.

# Run live
run-strat experiment_v6

# Dry run (no real orders, prints quotes + TP/SL decisions to log)
run-strat experiment_v6 --dry-run

# One quoting cycle only (smoke-test)
run-strat experiment_v6 --once --dry-run

# Debug logging (shows every quote, signal value, and unrealised PnL)
run-strat experiment_v6 --debug --dry-run

# ── backtest ──────────────────────────────────────────────────────────────────
#
#   Downloads 3 months of 1-min OHLCV from Bybit and simulates the MM strategy.
#   Data is cached to data/historical/<SYMBOL>_1m.csv and reused on reruns.
#   VIP 0 fees applied: maker 0.02% per side.

# Single symbol, default params (90 days)
run-backtest --symbol BTCUSDT --days 90

# All symbols in market_data/symbol_list.csv
run-backtest --all --days 90

# Grid search over spreads to find the most profitable configuration
run-backtest --symbol SOLUSDT --grid --days 90

# Grid search AND write best params directly to strategies/experiment_v6/params.json
run-backtest --symbol SOLUSDT --grid --update-params

# Grid search across ALL symbols, aggregate results, then write best params
run-backtest --all --grid --update-params

# Custom spread and inventory (0.05% half-spread, 200 USDT max inventory)
run-backtest --symbol ETHUSDT --spread 0.0005 --inventory 200 --days 90

# Skip API fetch, use only already-cached data
run-backtest --symbol BTCUSDT --no-fetch

# ── listing bot ───────────────────────────────────────────────────────────────

# Run the Upbit listing-news scraper / Bybit order bot
listing-bot

# ── strategy hub ─────────────────────────────────────────────────────────────

# List all registered strategies
strategy-hub list