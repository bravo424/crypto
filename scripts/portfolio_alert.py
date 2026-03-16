"""
portfolio_alert.py — Bybit portfolio snapshot sent to Telegram every hour.

Usage:
    python scripts/portfolio_alert.py --account MT325748552
    python scripts/portfolio_alert.py --account MT325748552 --once
    python scripts/portfolio_alert.py --creds design/_cred_bybit_MT325748552 --once
"""
from __future__ import annotations

import argparse
import csv
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from pybit.unified_trading import HTTP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOGGER = logging.getLogger(__name__)
SEOUL = ZoneInfo("Asia/Seoul")


# ── credentials ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Creds:
    api_key: str
    api_secret: str
    label: str


def load_creds(path: Path) -> Creds:
    if not path.exists():
        raise FileNotFoundError(f"Credential file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        row = next(csv.DictReader(fh), None)
    if not row or not row.get("API_KEY") or not row.get("SECRET"):
        raise ValueError(f"Expected API_KEY,SECRET columns in {path}")
    return Creds(api_key=row["API_KEY"].strip(), api_secret=row["SECRET"].strip(), label=path.stem.lstrip("_cred_bybit_"))


# ── Bybit queries ─────────────────────────────────────────────────────────────

@dataclass
class WalletSnapshot:
    total_balance: float
    available_balance: float
    unrealized_pnl: float


@dataclass
class PositionSnapshot:
    symbol: str
    side: str
    size: float
    entry_price: float
    mark_price: float
    leverage: float
    unrealized_pnl: float
    pnl_pct: float
    liq_price: float


def fetch_wallet(session: HTTP) -> WalletSnapshot:
    resp = session.get_wallet_balance(accountType="UNIFIED")
    account = resp["result"]["list"][0]
    return WalletSnapshot(
        total_balance=float(account.get("totalWalletBalance") or 0),
        available_balance=float(account.get("totalAvailableBalance") or 0),
        unrealized_pnl=float(account.get("totalPerpUPL") or 0),
    )


def fetch_positions(session: HTTP) -> list[PositionSnapshot]:
    resp = session.get_positions(category="linear", settleCoin="USDT")
    positions: list[PositionSnapshot] = []
    for item in resp["result"]["list"]:
        size = float(item.get("size") or 0)
        if size == 0:
            continue
        entry = float(item.get("avgPrice") or 0)
        mark = float(item.get("markPrice") or 0)
        upnl = float(item.get("unrealisedPnl") or 0)
        cost = size * entry
        pnl_pct = (upnl / cost * 100) if cost else 0.0
        positions.append(
            PositionSnapshot(
                symbol=item["symbol"],
                side=item["side"],
                size=size,
                entry_price=entry,
                mark_price=mark,
                leverage=float(item.get("leverage") or 1),
                unrealized_pnl=upnl,
                pnl_pct=pnl_pct,
                liq_price=float(item.get("liqPrice") or 0),
            )
        )
    return sorted(positions, key=lambda p: p.symbol)


# ── message formatting ────────────────────────────────────────────────────────

def _pnl_str(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:,.2f}"


def _pnl_emoji(value: float) -> str:
    return "🟢" if value >= 0 else "🔴"


def build_message(wallet: WalletSnapshot, positions: list[PositionSnapshot], label: str) -> str:
    now_kst = datetime.now(tz=SEOUL).strftime("%Y-%m-%d %H:%M KST")
    total_pnl_pct = (wallet.unrealized_pnl / wallet.total_balance * 100) if wallet.total_balance else 0.0
    emoji = _pnl_emoji(wallet.unrealized_pnl)

    lines = [
        f"📊 <b>Portfolio · {label}</b>",
        f"🕐 {now_kst}",
        "",
        f"💰 Total balance:    <b>${wallet.total_balance:,.2f} USDT</b>",
        f"🏦 Available:         ${wallet.available_balance:,.2f} USDT",
        f"{emoji} Unrealized PnL: <b>{_pnl_str(wallet.unrealized_pnl)} USDT</b>  ({_pnl_str(total_pnl_pct)}%)",
    ]

    if positions:
        lines += ["", f"<b>Open positions ({len(positions)})</b>"]
        for pos in positions:
            side_label = "Long 🔺" if pos.side == "Buy" else "Short 🔻"
            liq = f"  Liq: ${pos.liq_price:,.4f}" if pos.liq_price else ""
            lines.append(
                f"• <b>{pos.symbol}</b> {side_label} ×{pos.leverage:.0f}\n"
                f"  Size: {pos.size}  Entry: ${pos.entry_price:,.4f}  Mark: ${pos.mark_price:,.4f}\n"
                f"  PnL: {_pnl_str(pos.unrealized_pnl)} USDT ({_pnl_str(pos.pnl_pct)}%){liq}"
            )
    else:
        lines += ["", "No open positions."]

    return "\n".join(lines)


# ── Telegram ──────────────────────────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=10,
    )
    resp.raise_for_status()


def load_telegram_token(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Telegram token file not found: {path}")
    return path.read_text(encoding="utf-8").strip()


# ── main ──────────────────────────────────────────────────────────────────────

def run_once(creds: Creds, telegram_token: str, chat_id: str, testnet: bool) -> None:
    session = HTTP(testnet=testnet, api_key=creds.api_key, api_secret=creds.api_secret)
    wallet = fetch_wallet(session)
    positions = fetch_positions(session)
    message = build_message(wallet, positions, label=creds.label)
    send_telegram(telegram_token, chat_id, message)
    LOGGER.info("Alert sent for account %s", creds.label)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hourly Bybit portfolio alert to Telegram")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--account",
        help="Account suffix matching design/_cred_bybit_<ACCOUNT>  e.g. MT325748552",
    )
    group.add_argument(
        "--creds",
        help="Direct path to the credential CSV file",
    )
    parser.add_argument("--once", action="store_true", help="Send one alert and exit")
    parser.add_argument("--interval", type=int, default=3600, help="Seconds between alerts (default 3600)")
    parser.add_argument("--testnet", action="store_true", help="Use Bybit testnet")
    parser.add_argument(
        "--telegram-creds",
        default="design/_cred_nasang_bot_token",
        help="Path to Telegram bot token file",
    )
    parser.add_argument("--chat-id", default=None, help="Telegram chat ID (overrides .env)")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    import os

    args = parse_args()

    creds_path = Path(args.creds) if args.creds else Path(f"design/_cred_bybit_{args.account}")
    creds = load_creds(creds_path)

    telegram_token = load_telegram_token(Path(args.telegram_creds))
    chat_id = args.chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        raise ValueError("Telegram chat ID required — pass --chat-id or set TELEGRAM_CHAT_ID in .env")

    if args.once:
        run_once(creds, telegram_token, chat_id, testnet=args.testnet)
        return

    LOGGER.info("Starting hourly portfolio alerts for account %s (interval %ss)", creds.label, args.interval)
    while True:
        try:
            run_once(creds, telegram_token, chat_id, testnet=args.testnet)
        except Exception as error:
            LOGGER.error("Alert failed: %s", error)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
