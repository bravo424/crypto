# Architecture

## Overview

This is a multi-exchange crypto trading bot. It runs on Python 3.11+ and is installed as a local package (`upbit-bybit-listing-bot`). There are two independent systems sharing the same codebase:

1. **Listing Bot** — monitors Upbit new-listing notices and pre-emptively trades on Bybit perps before the pump
2. **Strategy Runner** — runs algorithmic signal-based strategies (currently `experiment_v1`) on Bybit, Upbit, or Bithumb

---

## Directory Structure

```
crypto/
├── pyproject.toml              # Package config & console-script entry points
├── requirements.txt            # Pinned deps
├── design/                     # Credentials (CSV: API_KEY,SECRET) — never committed
│   ├── _cred_bybit_MT325748552
│   ├── _cred_upbit
│   ├── _cred_bithumb
│   ├── _cred_nasang_bot_token       # Telegram bot for trade alerts
│   ├── _cred_position_bot_token     # Telegram bot for 30-min position updates
│   └── ip.txt
├── data/                       # Runtime state files (JSON, git-ignored)
│   ├── experiment_v1_bybit_state.json
│   ├── experiment_v1_upbit_state.json
│   ├── experiment_v1_bithumb_state.json
│   └── state.json                   # Listing bot state
├── src/
│   └── upbit_bybit_bot/        # Core shared library
│       ├── config.py           # load_settings() — reads .env + credential files
│       ├── models.py           # Shared dataclasses
│       ├── state.py            # State persistence helpers (listing bot)
│       ├── telegram_alerter.py # TelegramAlerter — wraps Bot API sendMessage
│       ├── bybit_client.py     # Bybit order execution (listing bot)
│       ├── upbit_client.py     # Upbit notice scraper (Playwright, listing bot)
│       ├── strategy.py         # ListingTradeStrategy — core listing-bot logic
│       └── main.py             # `listing-bot` console entry point
├── scripts/
│   ├── run_strategy.py         # `run-strat` entry point — loads any strategy by name
│   ├── strategy_hub.py         # `strategy-hub` entry point — list/manage strategies
│   ├── portfolio_alert.py      # Standalone Bybit portfolio snapshot → Telegram
│   └── HOWTORUN.md
└── strategies/
    ├── registry.json           # Catalogue of all strategies (metadata only)
    └── experiment_v1/          # Volume-spike momentum strategy
        ├── runner.py           # Main strategy loop
        ├── params.json         # Hot-reloadable strategy parameters
        ├── symbols_bybit.json  # Symbol list for Bybit
        ├── symbols_upbit.json  # Symbol list for Upbit
        ├── symbols_bithumb.json# Symbol list for Bithumb
        ├── upbit_exchange.py   # Upbit REST adapter (mimics pybit HTTP interface)
        └── bithumb_exchange.py # Bithumb REST adapter (mimics pybit HTTP interface)
```

---

## System 1: Listing Bot (`listing-bot`)

Monitors Upbit's listing notice board for new coin listings, then places entry orders on the corresponding Bybit USDT-perp before the pump. Uses Playwright to scrape the client-rendered Upbit page.

```
listing-bot
    └── upbit_bybit_bot/main.py
            ├── UpbitNoticeClient      (upbit_client.py)   — Playwright scraper
            ├── BybitClient            (bybit_client.py)   — order placement
            ├── ListingTradeStrategy   (strategy.py)       — decision logic
            └── TelegramAlerter        (telegram_alerter.py)
```

**Flow:** Poll Upbit notices → detect new listing → verify Bybit has the perp → place entry order → alert Telegram.

---

## System 2: Strategy Runner (`run-strat`)

Runs signal-based trading strategies on a 60-second loop. Currently one strategy: `experiment_v1`.

```
run-strat experiment_v1 [--exchange bybit|upbit|bithumb] [flags]
    └── scripts/run_strategy.py
            └── strategies/experiment_v1/runner.py
                    ├── Exchange session (one of):
                    │   ├── pybit HTTP            (Bybit perps)
                    │   ├── UpbitSession          (upbit_exchange.py)
                    │   └── BithumbSession        (bithumb_exchange.py)
                    ├── upbit_bybit_bot/config.py  — credentials + settings
                    └── upbit_bybit_bot/telegram_alerter.py
```

---

## experiment_v1 — Strategy Logic

### Signal

Every 60 s, for each symbol:
1. Fetch last 30 completed 5-min candles
2. **Volume-spike check**: `volume[last] / volume[prev] >= VOLUME_SPIKE` (default 1.20)
3. **Candle direction**: bullish → `Buy`, bearish → `Sell`
4. **369 EMA confirmation** (if `use_369: true`): price must stack with EMA-3 > EMA-6 > EMA-9 (long) or inverse (short). Both must agree with candle direction.

### Order Lifecycle (Bybit perps)

```
Signal fires
    → place_order (Limit, PostOnly, with takeProfit + stopLoss inline)
    → saved to state: open_positions + pending_orders
    → every tick: if order cancelled-as-filled (error 110001) → re-apply TP/SL
    → position appears in live get_positions → remove from pending_orders
    → TP or SL hit → position disappears from live → detect-close → Telegram alert
```

TP/SL are **sent with the order** on Bybit (`takeProfit`/`stopLoss` in `place_order`). They are native exchange orders that survive bot restarts. A repair loop (2b) re-arms them on restart if missing.

### Order Lifecycle (Upbit / Bithumb spot)

```
Signal fires (Buy only — no shorts on spot)
    → place_order (Limit / post_only)
    → saved to state: open_positions + pending_orders
    → every tick: manual TP/SL enforcement (2c loop) — compare markPrice to stored levels
    → Bearish signal while holding → market Sell (signal exit)
    → TP hit (price >= tp_price) → market Sell
    → SL hit (price <= sl_price) → market Sell
```

No native TP/SL on spot exchanges — the bot enforces them each tick in the 2c loop.

### TP/SL Calculation

```
tp_price = entry × (1 + take_profit_pct)   # e.g. entry × 1.03
sl_price = entry × (1 - stop_loss_pct)     # e.g. entry × 0.95
```

Prices are percentages of the **entry price** (not margin). Configured directly in `params.json`.

### params.json (hot-reloadable)

| Key | Default | Description |
|---|---|---|
| `take_profit_pct` | `0.03` | TP = +3% from entry |
| `stop_loss_pct` | `0.05` | SL = −5% from entry |
| `notional_usdt` | `20.0` | USDT margin per trade (Bybit: × leverage for contract size) |
| `leverage` | `20` | Bybit leverage (ignored for spot) |
| `notional_cap_usdt` | `450.0` | Max total open notional — no new trades above this |
| `volume_spike` | `1.20` | Min volume ratio to trigger signal |
| `time_in_force` | `PostOnly` | `PostOnly` or `GTC` |
| `use_369` | `true` | Require 369 EMA stack confirmation |

---

## Exchange Adapters

Both `UpbitSession` and `BithumbSession` expose the same interface as `pybit.unified_trading.HTTP`, so `runner.py` is exchange-agnostic.

| Method | Bybit (pybit) | Upbit | Bithumb |
|---|---|---|---|
| `get_kline` | native | candles/minutes/{n} | candles/minutes/{n} |
| `get_tickers` | native | ticker | ticker |
| `get_instruments_info` | native | derived from tick price | derived from tick price |
| `get_positions` | native | accounts (coin balances) | accounts (coin balances) |
| `get_wallet_balance` | native | accounts (USDT balance) | accounts (KRW→USDT) |
| `place_order` | native | orders (bid/ask) | orders (bid/ask, KRW tick-snapped) |
| `cancel_order` | native | DELETE /order | DELETE /order |
| `set_leverage` | native | no-op | no-op |
| `set_trading_stop` | native | no-op | no-op |

**Bithumb KRW pricing:** All prices travel as USDT internally. `place_order` converts to KRW using a cached rate and snaps to Bithumb's official 호가단위 tick table before submitting.

---

## Credential Files

All credentials live under `design/` as CSV files with header `API_KEY,SECRET`. Telegram bot tokens are plain text files.

| File | Used by |
|---|---|
| `_cred_bybit_MT325748552` | Bybit trading (listing bot + experiment_v1) |
| `_cred_upbit` | Upbit spot trading (experiment_v1 --exchange upbit) |
| `_cred_bithumb` | Bithumb spot trading (experiment_v1 --exchange bithumb) |
| `_cred_nasang_bot_token` | Telegram trade open/close alerts |
| `_cred_position_bot_token` | Telegram 30-min position update alerts |

---

## State Files

| File | Contents |
|---|---|
| `data/experiment_v1_bybit_state.json` | open_positions, pending_orders, processed_candles |
| `data/experiment_v1_upbit_state.json` | same, for Upbit run |
| `data/experiment_v1_bithumb_state.json` | same, for Bithumb run |
| `data/state.json` | Listing bot state (processed notices) |

State is written to disk at the end of every loop tick. On restart the bot resumes from existing state — pending orders are cancelled if still unfilled after 60 s, and open positions on Bybit have their TP/SL re-validated.

---

## Console Entry Points

| Command | Source | Description |
|---|---|---|
| `listing-bot` | `src/upbit_bybit_bot/main.py` | Upbit listing scraper + Bybit order bot |
| `run-strat <name> [args]` | `scripts/run_strategy.py` | Run any strategy under `strategies/` |
| `strategy-hub list` | `scripts/strategy_hub.py` | List all registered strategies |
| `python scripts/portfolio_alert.py` | `scripts/portfolio_alert.py` | Hourly Bybit portfolio → Telegram |

### run-strat flags (experiment_v1)

| Flag | Effect |
|---|---|
| `--exchange bybit\|upbit\|bithumb` | Choose exchange (default: bybit) |
| `--once` | Single cycle then exit (smoke-test) |
| `--update` | Send immediate 30-min update to Telegram |
| `--cancel-all` | Cancel all open orders and clear pending state |
| `--close-all` | Market-close all positions and wipe state |
