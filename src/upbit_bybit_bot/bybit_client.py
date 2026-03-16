from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from pybit.exceptions import InvalidRequestError
from pybit.unified_trading import HTTP

from upbit_bybit_bot.config import ApiCredentials
from upbit_bybit_bot.models import BybitInstrument

LOGGER = logging.getLogger(__name__)


class BybitClient:
    def __init__(self, credentials: ApiCredentials, testnet: bool = False) -> None:
        self.session = HTTP(testnet=testnet, api_key=credentials.api_key, api_secret=credentials.api_secret)

    def get_linear_instrument(self, symbol: str) -> BybitInstrument | None:
        for params in ({}, {"status": "PreLaunch"}):
            try:
                response = self.session.get_instruments_info(category="linear", symbol=symbol, **params)
            except InvalidRequestError as error:
                LOGGER.debug("Bybit symbol not available (%s): %s", symbol, error)
                return None
            instrument = self._first_instrument(response)
            if instrument:
                return instrument
        return None

    def place_market_order(
        self,
        symbol: str,
        side: str,
        usdt_amount: float,
        leverage: int | None,
        dry_run: bool,
    ) -> dict:
        instrument = self.get_linear_instrument(symbol)
        if instrument is None:
            raise ValueError(f"Bybit symbol not found: {symbol}")

        if instrument.last_price is None:
            raise ValueError(f"Ticker price unavailable for {symbol}")

        quantity = self._calculate_order_quantity(
            usdt_amount=Decimal(str(usdt_amount)),
            last_price=Decimal(instrument.last_price),
            qty_step=Decimal(instrument.qty_step),
            min_order_qty=Decimal(instrument.min_order_qty),
            min_notional_value=Decimal(instrument.min_notional_value),
        )
        order_payload = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": self._format_decimal(quantity),
        }

        if dry_run:
            LOGGER.info("Dry run order payload: %s", order_payload)
            return {"dry_run": True, "payload": order_payload}

        if leverage is not None:
            self._set_leverage(symbol=symbol, leverage=leverage)

        return self.session.place_order(**order_payload)

    def _first_instrument(self, response: dict) -> BybitInstrument | None:
        instruments = response.get("result", {}).get("list", [])
        if not instruments:
            return None

        item = instruments[0]
        ticker = self.session.get_tickers(category="linear", symbol=item["symbol"])
        ticker_list = ticker.get("result", {}).get("list", [])
        last_price = ticker_list[0].get("lastPrice") if ticker_list else None
        launch_at = self._parse_timestamp(item.get("launchTime"))
        continuous_trading_at = self._extract_continuous_trading(item)
        lot_filter = item.get("lotSizeFilter", {})

        return BybitInstrument(
            symbol=item["symbol"],
            status=item["status"],
            is_pre_listing=bool(item.get("isPreListing")),
            launch_at=launch_at,
            continuous_trading_at=continuous_trading_at,
            qty_step=lot_filter.get("qtyStep", "1"),
            min_order_qty=lot_filter.get("minOrderQty", "1"),
            min_notional_value=lot_filter.get("minNotionalValue", "5"),
            last_price=last_price,
        )

    def _set_leverage(self, symbol: str, leverage: int) -> None:
        try:
            self.session.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        except Exception as error:  # noqa: BLE001
            LOGGER.info("Bybit leverage update skipped for %s: %s", symbol, error)

    @staticmethod
    def _extract_continuous_trading(item: dict) -> datetime | None:
        pre_listing_info = item.get("preListingInfo") or {}
        for phase in pre_listing_info.get("phases", []):
            if phase.get("phase") == "ContinuousTrading":
                return BybitClient._parse_timestamp(phase.get("startTime"))
        return None

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        timestamp = int(value)
        if timestamp <= 0:
            return None
        return datetime.fromtimestamp(timestamp / 1000, tz=UTC)

    @staticmethod
    def _calculate_order_quantity(
        usdt_amount: Decimal,
        last_price: Decimal,
        qty_step: Decimal,
        min_order_qty: Decimal,
        min_notional_value: Decimal,
    ) -> Decimal:
        raw_quantity = usdt_amount / last_price
        quantity = BybitClient._round_to_step(raw_quantity, qty_step, ROUND_DOWN)
        if quantity < min_order_qty:
            quantity = min_order_qty

        notional = quantity * last_price
        if notional < min_notional_value:
            quantity = BybitClient._round_to_step(min_notional_value / last_price, qty_step, ROUND_UP)

        return quantity

    @staticmethod
    def _round_to_step(value: Decimal, step: Decimal, rounding: str) -> Decimal:
        if step == 0:
            return value
        units = (value / step).to_integral_value(rounding=rounding)
        return units * step

    @staticmethod
    def _format_decimal(value: Decimal) -> str:
        return format(value.normalize(), "f")
