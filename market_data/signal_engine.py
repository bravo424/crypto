"""Combines BookManager + TradeFlow into a typed MarketSignal.

Called by the WebSocket client once per book/trade update to refresh
the signal store for a symbol.
"""
from __future__ import annotations

import time
from typing import Literal

from market_data.book_manager import BookManager
from market_data.signal_store import MarketSignal, SignalStore
from market_data.trade_flow import TradeFlow

# Thresholds for deriving a simple trend_bias label.
# Require BOTH book imbalance AND trade pressure to agree.
_IMBALANCE_THRESH = 0.15    # ob_imbalance must exceed this in absolute value
_PRESSURE_THRESH  = 0.10    # 5m trade pressure must exceed this in absolute value


def _derive_trend_bias(ob_imbalance: float,
                       pressure_5m: float) -> Literal["Buy", "Sell"] | None:
    if ob_imbalance >= _IMBALANCE_THRESH and pressure_5m >= _PRESSURE_THRESH:
        return "Buy"
    if ob_imbalance <= -_IMBALANCE_THRESH and pressure_5m <= -_PRESSURE_THRESH:
        return "Sell"
    return None


class SignalEngine:
    """Refreshes the SignalStore on every book or trade update."""

    def __init__(self, book: BookManager, flow: TradeFlow,
                 store: SignalStore) -> None:
        self._book  = book
        self._flow  = flow
        self._store = store

    def refresh(self, symbol: str) -> None:
        """Recompute and publish the latest MarketSignal for `symbol`."""
        bid         = self._book.best_bid(symbol)
        ask         = self._book.best_ask(symbol)
        mid         = self._book.mid(symbol)
        spread_pct  = self._book.spread_pct(symbol)
        ob_imb      = self._book.imbalance(symbol)
        p30s        = self._flow.pressure_30s(symbol)
        p5m         = self._flow.pressure_5m(symbol)
        trend_bias  = _derive_trend_bias(ob_imb, p5m)

        sig = MarketSignal(
            symbol=symbol,
            ts=time.time(),
            bid=bid,
            ask=ask,
            mid=mid,
            spread_pct=spread_pct,
            ob_imbalance=ob_imb,
            trade_pressure_30s=p30s,
            trade_pressure_5m=p5m,
            trend_bias=trend_bias,
        )
        self._store.put(sig)
