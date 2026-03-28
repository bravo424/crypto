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
#               strategies/experiment_v5/symbol_list.csv  (Bybit WS for MD gate)
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
#               strategies/experiment_v6/symbol_list.csv  (symbols_csv in JSON)
#
#   Fee viability (VIP 0): half_spread must be > 0.02% (break-even).
#   Default half_spread = 0.04% (full spread 0.08% — net ~0.04% per round trip).
#
#   Leverage & margin mode
#   ──────────────────────
#   Set on startup automatically for every symbol.  Cross margin means all
#   positions share your full account balance as collateral — much safer for
#   market making than isolated (which liquidates each position independently).
#
#   leverage_default   3×  (all symbols unless overridden)
#   symbol_leverage    {"BTCUSDT": 5, "ETHUSDT": 5}  — liquid, tight-spread pairs
#   use_cross_margin   true  (cross) / false (isolated)
#
#   Position management (TP / SL / timeout)
#   ─────────────────────────────────────────
#   Primary TP and SL are placed on the exchange immediately when a position
#   fills (set_trading_stop API).  They execute even if the bot disconnects.
#
#   tp_pct        (default 0.008 = 0.8%)
#     Native exchange TP: close at +0.8% from entry price (mark price trigger).
#
#   sl_pct        (default 0.005 = 0.5%)
#     Native exchange SL: close at -0.5% from entry price (mark price trigger).
#     Emergency backup: if native SL somehow misses, bot fires a market close
#     at -1.0% (2× sl_pct) as a last resort.
#
#   max_hold_min  (default 30)
#     Bybit has no time-based stops, so the bot force-closes at market if a
#     position has been open for 30 minutes without resolving.
#
#   tp_cover_round_trip_fees  (default false)
#     When true, TP distance is max(tp_pct, fee_floor).  Fee floor is
#     2× maker (0.04%) + tp_fee_buffer_pct if both entry and TP exit are maker;
#     set tp_exit_assume_taker=true to use maker+taker (stricter floor).
#     Use a small tp_pct (e.g. 0.0005) with cover enabled to aim for net > 0
#     after fees.  SL (sl_pct) is always set the same way as before.
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

# ── experiment_v7 — Signal-Driven Directional Scalp (Bybit USDT-perp) ────────
#
#   WHY not MM (v6)?  At Bybit VIP 0, maker fee = +0.02% (you pay, no rebate).
#   MM round-trip = 0.04% before adverse selection.  directional scalp pays
#   0.075% per trade but targets 0.8% TP → net +0.725% per win.
#   Break-even win rate ≈ 41%.
#
#   Entry: ob_imbalance AND trade_pressure_5m both exceed thresholds.
#   (Micro signal is WebSocket book + trade flow — not 1m OHLC candles.)
#   Optional: htf_kline_minutes (3 / 5 / 15 …) — REST gate so the last *closed*
#   kline’s close vs the prior candle agrees with the side (fewer whipsaws).
#   Set htf_kline_minutes to 0 to disable.  htf_cache_sec avoids spamming kline API.
#   Execution: market order (taker, guaranteed fill).
#   Exit: native TP + SL via set_trading_stop on exchange immediately after fill.
#
#   Key config: strategies/experiment_v7/params.json
#               strategies/experiment_v7/symbol_list.csv  (symbols_csv in JSON)
#
#   Leverage: 5× for BTCUSDT/ETHUSDT, 3× for all others. Cross margin.
#   Position size: equity × max_risk_pct / sl_pct, capped at max_notional_usd.

# Run live
run-strat experiment_v7

# Dry run (no real orders — prints signal decisions to log)
run-strat experiment_v7 --dry-run

# One scan cycle only (smoke-test)
run-strat experiment_v7 --once --dry-run

# Debug logging (shows every signal value, ob_imbalance, trade pressure)
run-strat experiment_v7 --debug --dry-run

# ── backtest ──────────────────────────────────────────────────────────────────
#
#   Downloads 3 months of 1-min OHLCV from Bybit and simulates strategies.
#   Data is cached to data/historical/<SYMBOL>_1m.csv and reused on reruns.
#   VIP 0 fees applied: maker 0.02%, taker 0.055%.

# ── MM backtest (experiment_v6) ───────────────────────────────────────────────

# Single symbol, default params (90 days)
run-backtest --symbol BTCUSDT --days 90

# All symbols in strategies/experiment_v6/symbol_list.csv (v7 list if --directional)
run-backtest --all --days 90

# Grid search over spreads to find the most profitable MM configuration
run-backtest --symbol SOLUSDT --grid --days 90

# Grid search AND write best params directly to strategies/experiment_v6/params.json
run-backtest --symbol SOLUSDT --grid --update-params

# Grid search across ALL symbols, aggregate results, then write best params
run-backtest --all --grid --update-params

# Custom spread and inventory (0.05% half-spread, 200 USDT max inventory)
run-backtest --symbol ETHUSDT --spread 0.0005 --inventory 200 --days 90

# ── Directional backtest (experiment_v7) ──────────────────────────────────────
#
#   Proxies live ob_imbalance + trade_pressure_5m using candle momentum
#   (N of last M candles agree in direction + volume surge).
#   Entry at market (taker fee), TP as limit (maker fee), SL at market.

# Single symbol, default params
run-backtest --directional --symbol BTCUSDT --days 90

# All symbols
run-backtest --directional --all --days 90

# Grid search over momentum_ratio × vol_mult combinations
run-backtest --directional --symbol SOLUSDT --grid --days 90

# Grid search AND write best params to strategies/experiment_v7/params.json
run-backtest --directional --symbol SOLUSDT --grid --update-v7-params

# Grid all symbols then write best aggregated params
run-backtest --directional --all --grid --update-v7-params

# Custom TP/SL
run-backtest --directional --symbol BTCUSDT --tp 0.010 --sl 0.005 --days 90

# Skip API fetch (use cached data only)
run-backtest --directional --symbol BTCUSDT --no-fetch

# ── listing bot ───────────────────────────────────────────────────────────────

# Run the Upbit listing-news scraper / Bybit order bot
listing-bot

# ── strategy hub ─────────────────────────────────────────────────────────────

# List all registered strategies
strategy-hub list