from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from upbit_bybit_bot.bybit_client import BybitClient
from upbit_bybit_bot.config import Settings
from upbit_bybit_bot.models import BybitInstrument, ListingSignal, PendingTrade
from upbit_bybit_bot.state import BotState
from upbit_bybit_bot.telegram_alerter import TelegramAlerter
from upbit_bybit_bot.upbit_client import UpbitNoticeClient

LOGGER = logging.getLogger(__name__)


class ListingTradeStrategy:
    def __init__(self, settings: Settings, upbit_client: UpbitNoticeClient, bybit_client: BybitClient) -> None:
        self.settings = settings
        self.upbit_client = upbit_client
        self.bybit_client = bybit_client
        self.alerter = TelegramAlerter.from_env(
            token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )

    def run_cycle(self, notice_limit: int = 30) -> None:
        state = BotState.load(self.settings.state_file)
        notices = self.upbit_client.list_recent_notices(limit=notice_limit)
        fresh_notices = [notice for notice in notices if not state.is_notice_seen(notice.notice_id)]

        LOGGER.info("Found %s new Upbit notices", len(fresh_notices))
        now = datetime.now(tz=UTC)
        for notice in reversed(fresh_notices):
            signals = self.upbit_client.extract_listing_signals(notice)
            state.mark_notice_seen(notice.notice_id)
            for signal in signals:
                # Skip signals whose listing time is already in the past
                if signal.listing_at and signal.listing_at.astimezone(UTC) <= now:
                    LOGGER.info(
                        "Skipping %s (%s): listing time %s is in the past",
                        signal.asset_symbol,
                        signal.notice_id,
                        signal.listing_at.isoformat(),
                    )
                    continue
                self._handle_signal(signal, state)

        self._process_pending_trades(state)
        state.save(self.settings.state_file)

    def _handle_signal(self, signal: ListingSignal, state: BotState) -> None:
        if state.is_trade_known(signal.notice_id, signal.asset_symbol):
            return

        bybit_symbol = f"{signal.asset_symbol}USDT"
        instrument = self.bybit_client.get_linear_instrument(bybit_symbol)
        if instrument is None:
            LOGGER.info("Skipping %s because %s is unavailable on Bybit linear market", signal.notice_id, bybit_symbol)
            return

        execute_at = self._determine_execute_at(signal, instrument)
        if signal.listing_at and datetime.now(tz=signal.listing_at.tzinfo) >= signal.listing_at:
            LOGGER.info("Skipping %s because Upbit listing time has already passed", bybit_symbol)
            return

        pending_trade = PendingTrade(
            notice_id=signal.notice_id,
            asset_symbol=signal.asset_symbol,
            bybit_symbol=bybit_symbol,
            execute_at=execute_at.isoformat(),
            source_title=signal.title,
            source_url=signal.url,
            listing_at=signal.listing_at.isoformat() if signal.listing_at else None,
        )

        if execute_at <= datetime.now(tz=UTC):
            self._execute_trade(pending_trade, state)
            return

        state.add_pending_trade(pending_trade)
        LOGGER.info("Queued %s for execution at %s", bybit_symbol, execute_at.isoformat())
        self._alert_listing_detected(pending_trade)

    def _process_pending_trades(self, state: BotState) -> None:
        now = datetime.now(tz=UTC)
        remaining: list[PendingTrade] = []

        for trade in state.pending_trades:
            listing_at = datetime.fromisoformat(trade.listing_at) if trade.listing_at else None
            if listing_at and now >= listing_at.astimezone(UTC):
                LOGGER.info("Dropping stale pending trade for %s", trade.bybit_symbol)
                continue
            if datetime.fromisoformat(trade.execute_at) <= now:
                self._execute_trade(trade, state)
                continue
            remaining.append(trade)

        state.pending_trades = remaining

    def _execute_trade(self, trade: PendingTrade, state: BotState) -> None:
        response = self.bybit_client.place_market_order(
            symbol=trade.bybit_symbol,
            side=self.settings.bybit_order_side,
            usdt_amount=self.settings.bybit_order_usdt,
            leverage=self.settings.bybit_leverage,
            dry_run=self.settings.dry_run,
        )
        state.mark_trade_executed(trade.notice_id, trade.asset_symbol)
        LOGGER.info("Executed trade for %s: %s", trade.bybit_symbol, response)

    def _alert_listing_detected(self, trade: PendingTrade) -> None:
        if self.alerter is None:
            return
        listing_time_str = f"\nUpbit listing: <b>{trade.listing_at}</b>" if trade.listing_at else ""
        message = (
            f"\U0001f7e2 <b>New listing signal detected</b>\n"
            f"Symbol: <b>{trade.bybit_symbol}</b>\n"
            f"Order will fire at: <b>{trade.execute_at}</b>"
            f"{listing_time_str}\n"
            f"Source: {trade.source_title}\n"
            f"{trade.source_url}"
        )
        self.alerter.send(message)

    def _determine_execute_at(self, signal: ListingSignal, instrument: BybitInstrument) -> datetime:
        target_time = signal.listing_at
        if target_time is None:
            target_time = instrument.continuous_trading_at.astimezone(signal.detected_at.tzinfo) if instrument.continuous_trading_at and signal.detected_at else None
        if target_time is None:
            target_time = instrument.launch_at.astimezone(signal.detected_at.tzinfo) if instrument.launch_at and signal.detected_at else None
        if target_time is None:
            return datetime.now(tz=UTC)
        return target_time.astimezone(UTC) - timedelta(minutes=self.settings.trade_lead_minutes)
