# Upbit to Bybit Listing Bot

This project watches Upbit Korea listing notices and opens a Bybit USDT perpetual position before the scheduled listing time when the matching perpetual market exists.

## What it does

- Loads Bybit credentials from the existing credential file in `design/`
- Renders the public Upbit notice page with Playwright because the notice board is client-rendered
- Detects listing notices such as `신규 거래지원 안내`
- Extracts listed asset symbols from the notice title and detail page
- Tries to extract the scheduled listing time from the notice body
- Checks whether `<ASSET>USDT` exists on Bybit linear contracts, including pre-listing contracts
- Places a market order on Bybit using a configurable USDT notional
- Persists seen notices and pending trades in `data/state.json`

## Safety defaults

The bot starts in dry-run mode by default. In dry-run mode it will scrape, plan, and log orders but it will not submit any live orders.

## Setup

1. Create a virtual environment.
2. Install the dependencies.
3. Install the Chromium browser used by Playwright.
4. Copy `.env.example` to `.env` if you want to override defaults.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
python -m playwright install chromium
```

## Configuration

Environment variables are loaded from `.env` when present.

- `BYBIT_CREDENTIALS_FILE`: CSV file with `API_KEY,SECRET`
- `BYBIT_TESTNET`: `true` or `false`
- `BYBIT_ORDER_SIDE`: `Buy` or `Sell`
- `BYBIT_ORDER_USDT`: notional size in USDT
- `BYBIT_LEVERAGE`: leverage to set before ordering
- `UPBIT_POLL_SECONDS`: notice polling interval
- `TRADE_LEAD_MINUTES`: how long before listing time the order should fire
- `STATE_FILE`: JSON file used for seen notices and pending trades
- `DRY_RUN`: when `true`, no live order is sent
- `HEADLESS_BROWSER`: when `true`, Chromium runs without UI

## Run

One cycle:

```powershell
listing-bot --once
```

Continuous mode:

```powershell
listing-bot
```

Debug with a visible browser window:

```powershell
listing-bot --once --headful
```

## Important limitations

- Upbit notice pages are rendered in the browser, so the scraper depends on Playwright rather than a simple HTTP request.
- Listing time extraction is regex-based because Upbit notice formatting varies. Some notices may require pattern refinement.
- The current strategy only opens a position. It does not place exit orders, stop losses, or risk limits.
- Upbit API credentials are loaded for future extension, but the current notice workflow uses public web pages.

## Suggested next hardening steps

1. Add stop-loss and take-profit management after entry.
2. Add Telegram or Slack alerts for detected listings and submitted orders.
3. Capture and store raw notice text for parser debugging.
4. Add a replay test suite with saved Upbit notice HTML snapshots.
