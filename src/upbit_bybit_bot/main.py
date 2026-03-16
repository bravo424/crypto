from __future__ import annotations

import argparse
import logging
import time
from dataclasses import replace

from upbit_bybit_bot.bybit_client import BybitClient
from upbit_bybit_bot.config import load_settings
from upbit_bybit_bot.strategy import ListingTradeStrategy
from upbit_bybit_bot.upbit_client import UpbitNoticeClient


def main() -> None:
    args = parse_args()
    settings = load_settings()
    if args.poll_seconds is not None:
        settings = replace(settings, upbit_poll_seconds=args.poll_seconds)
    if args.headful:
        settings = replace(settings, headless_browser=False)

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    strategy = ListingTradeStrategy(
        settings=settings,
        upbit_client=UpbitNoticeClient(headless=settings.headless_browser),
        bybit_client=BybitClient(credentials=settings.bybit_credentials, testnet=settings.bybit_testnet),
    )

    if args.once:
        strategy.run_cycle(notice_limit=args.notice_limit)
        return

    while True:
        strategy.run_cycle(notice_limit=args.notice_limit)
        time.sleep(settings.upbit_poll_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trade Bybit perpetuals using Upbit listing notices")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit")
    parser.add_argument("--headful", action="store_true", help="Run Playwright with a visible Chromium window")
    parser.add_argument("--poll-seconds", type=int, help="Override the polling interval")
    parser.add_argument("--notice-limit", type=int, default=30, help="How many recent notices to inspect each cycle")
    return parser.parse_args()


if __name__ == "__main__":
    main()
