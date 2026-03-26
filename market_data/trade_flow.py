"""Rolling-window trade flow aggregator.

Tracks net buy/sell volume over configurable windows and computes
a normalised trade pressure score in [-1, 1].
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class _Trade:
    ts: float       # Unix seconds
    side: str       # "Buy" or "Sell"
    qty: float      # base quantity
    price: float


class TradeFlow:
    """Aggregates raw trades into rolling-window buy/sell pressure metrics."""

    def __init__(self,
                 window_30s: float = 30.0,
                 window_5m: float = 300.0) -> None:
        self._w30 = window_30s
        self._w5m = window_5m
        self._lock = threading.Lock()
        # {symbol: deque[_Trade]}
        self._trades: dict[str, deque[_Trade]] = {}

    def on_trade(self, symbol: str, side: str, qty: float, price: float,
                 ts: float | None = None) -> None:
        """Record a new public trade."""
        t = _Trade(ts=ts or time.time(), side=side, qty=qty, price=price)
        with self._lock:
            if symbol not in self._trades:
                self._trades[symbol] = deque()
            self._trades[symbol].append(t)

    def pressure(self, symbol: str, window: float) -> float:
        """Net buy ratio over `window` seconds.

        Returns a value in [-1, 1].
        +1 = all buys.  -1 = all sells.  0 = balanced.
        """
        now = time.time()
        cutoff = now - window
        with self._lock:
            q = self._trades.get(symbol)
            if not q:
                return 0.0
            # Prune stale entries older than the larger window
            max_w = max(self._w30, self._w5m)
            while q and q[0].ts < now - max_w:
                q.popleft()
            trades = [t for t in q if t.ts >= cutoff]
        buy_vol = sum(t.qty for t in trades if t.side == "Buy")
        sell_vol = sum(t.qty for t in trades if t.side == "Sell")
        total = buy_vol + sell_vol
        if total == 0:
            return 0.0
        return (buy_vol - sell_vol) / total

    def pressure_30s(self, symbol: str) -> float:
        return self.pressure(symbol, self._w30)

    def pressure_5m(self, symbol: str) -> float:
        return self.pressure(symbol, self._w5m)
