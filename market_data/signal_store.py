"""Thread-safe store for latest market signals per symbol."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class MarketSignal:
    symbol: str
    ts: float                     # Unix timestamp of last update

    # Order book
    bid: float = 0.0
    ask: float = 0.0
    mid: float = 0.0
    spread_pct: float = 0.0
    ob_imbalance: float = 0.0    # (bid_vol - ask_vol) / (bid_vol + ask_vol), top-5 levels

    # Trade flow (rolling windows)
    trade_pressure_30s: float = 0.0   # net buy ratio [-1, 1] over 30 s
    trade_pressure_5m:  float = 0.0   # net buy ratio [-1, 1] over 5 min

    # Derived direction
    trend_bias: Literal["Buy", "Sell"] | None = None


class SignalStore:
    """Thread-safe dictionary of the latest MarketSignal per symbol."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, MarketSignal] = {}

    def put(self, signal: MarketSignal) -> None:
        with self._lock:
            self._data[signal.symbol] = signal

    def get(self, symbol: str) -> MarketSignal | None:
        with self._lock:
            return self._data.get(symbol)

    def all(self) -> dict[str, MarketSignal]:
        with self._lock:
            return dict(self._data)

    def symbols(self) -> list[str]:
        with self._lock:
            return list(self._data.keys())


# Process-global singleton — imported by strategies and the WS client
_global_store = SignalStore()
