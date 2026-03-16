from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class ApiCredentials:
    api_key: str
    api_secret: str


@dataclass(frozen=True, slots=True)
class Settings:
    bybit_credentials: ApiCredentials
    upbit_credentials: ApiCredentials | None
    bithumb_credentials: ApiCredentials | None
    bybit_testnet: bool
    bybit_order_side: str
    bybit_order_usdt: float
    bybit_leverage: int | None
    upbit_poll_seconds: int
    trade_lead_minutes: int
    state_file: Path
    dry_run: bool
    log_level: str
    headless_browser: bool
    telegram_bot_token: str | None
    telegram_chat_id: str | None


def load_settings() -> Settings:
    load_dotenv()

    bybit_path   = Path(os.getenv("BYBIT_CREDENTIALS_FILE",   "config/bybit_subaccount1.csv"))
    upbit_path   = Path(os.getenv("UPBIT_CREDENTIALS_FILE",   "config/upbit.csv"))
    bithumb_path = Path(os.getenv("BITHUMB_CREDENTIALS_FILE", "config/bithumb.csv"))
    dry_run = _as_bool(os.getenv("DRY_RUN", "true"))

    bybit_credentials   = _load_credentials(bybit_path)
    upbit_credentials   = _load_credentials(upbit_path)   if upbit_path.exists()   else None
    bithumb_credentials = _load_credentials(bithumb_path) if bithumb_path.exists() else None

    telegram_token_path = Path(os.getenv("TELEGRAM_CREDENTIALS_FILE", "design/_cred_nasang_bot_token"))
    telegram_bot_token = _load_token_file(telegram_token_path)

    return Settings(
        bybit_credentials=bybit_credentials,
        upbit_credentials=upbit_credentials,
        bithumb_credentials=bithumb_credentials,
        bybit_testnet=_as_bool(os.getenv("BYBIT_TESTNET", "false")),
        bybit_order_side=os.getenv("BYBIT_ORDER_SIDE", "Buy"),
        bybit_order_usdt=float(os.getenv("BYBIT_ORDER_USDT", "50")),
        bybit_leverage=_optional_int(os.getenv("BYBIT_LEVERAGE", "2")),
        upbit_poll_seconds=int(os.getenv("UPBIT_POLL_SECONDS", "3600")),
        trade_lead_minutes=int(os.getenv("TRADE_LEAD_MINUTES", "15")),
        state_file=Path(os.getenv("STATE_FILE", "data/state.json")),
        dry_run=dry_run,
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        headless_browser=_as_bool(os.getenv("HEADLESS_BROWSER", "true")),
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
    )


def _load_credentials(path: Path) -> ApiCredentials:
    if not path.exists():
        raise FileNotFoundError(f"Credential file not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        row = next(reader, None)

    if not row or not row.get("API_KEY") or not row.get("SECRET"):
        raise ValueError(f"Credential file must contain API_KEY,SECRET columns: {path}")

    return ApiCredentials(api_key=row["API_KEY"].strip(), api_secret=row["SECRET"].strip())


def _load_token_file(path: Path) -> str | None:
    if not path.exists():
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return int(cleaned)
