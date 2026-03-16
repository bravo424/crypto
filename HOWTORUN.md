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

# ── listing bot ───────────────────────────────────────────────────────────────

# Run the Upbit listing-news scraper / Bybit order bot
listing-bot

# ── strategy hub ─────────────────────────────────────────────────────────────

# List all registered strategies
strategy-hub list