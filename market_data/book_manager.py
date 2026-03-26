"""Maintains a lightweight L2 order book for each symbol.

Bybit sends `orderbook.1` snapshots (best bid+ask only) and `orderbook.50`
delta updates (top 50 levels).  We subscribe to `orderbook.50` so we can
compute top-N volume imbalance; the best bid/ask are always levels[0].
"""
from __future__ import annotations

import threading
from collections import defaultdict


class BookManager:
    """Tracks bid/ask levels per symbol and computes imbalance."""

    def __init__(self, depth: int = 5) -> None:
        self._depth = depth
        self._lock = threading.Lock()
        # {symbol: {"b": [[price, qty], ...], "a": [[price, qty], ...]}}
        self._books: dict[str, dict[str, list[list[float]]]] = defaultdict(
            lambda: {"b": [], "a": []}
        )

    # ── public interface ──────────────────────────────────────────────────────

    def on_snapshot(self, symbol: str, bids: list, asks: list) -> None:
        """Full snapshot — replace book entirely."""
        with self._lock:
            self._books[symbol] = {
                "b": [[float(p), float(q)] for p, q in bids],
                "a": [[float(p), float(q)] for p, q in asks],
            }

    def on_delta(self, symbol: str, bids: list, asks: list) -> None:
        """Incremental delta — apply Bybit delta format (qty=0 → remove)."""
        with self._lock:
            book = self._books[symbol]
            self._apply_side(book["b"], bids, descending=True)
            self._apply_side(book["a"], asks, descending=False)

    def best_bid(self, symbol: str) -> float:
        with self._lock:
            lvls = self._books[symbol]["b"]
            return lvls[0][0] if lvls else 0.0

    def best_ask(self, symbol: str) -> float:
        with self._lock:
            lvls = self._books[symbol]["a"]
            return lvls[0][0] if lvls else 0.0

    def imbalance(self, symbol: str) -> float:
        """(bid_vol - ask_vol) / (bid_vol + ask_vol) over top-N levels.

        Returns a value in [-1, 1].
        +1 = pure bid pressure (likely to go up).
        -1 = pure ask pressure (likely to go down).
        """
        with self._lock:
            bids = self._books[symbol]["b"][: self._depth]
            asks = self._books[symbol]["a"][: self._depth]
        bid_vol = sum(q for _, q in bids)
        ask_vol = sum(q for _, q in asks)
        total = bid_vol + ask_vol
        if total == 0:
            return 0.0
        return (bid_vol - ask_vol) / total

    def mid(self, symbol: str) -> float:
        bb = self.best_bid(symbol)
        ba = self.best_ask(symbol)
        if bb <= 0 or ba <= 0:
            return 0.0
        return (bb + ba) / 2.0

    def spread_pct(self, symbol: str) -> float:
        bb = self.best_bid(symbol)
        ba = self.best_ask(symbol)
        if bb <= 0 or ba <= 0:
            return 0.0
        return (ba - bb) / ((ba + bb) / 2.0)

    # ── internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _apply_side(levels: list[list[float]], updates: list,
                    descending: bool) -> None:
        price_map: dict[float, float] = {lvl[0]: lvl[1] for lvl in levels}
        for p, q in updates:
            p, q = float(p), float(q)
            if q == 0.0:
                price_map.pop(p, None)
            else:
                price_map[p] = q
        levels.clear()
        levels.extend(
            sorted(([p, q] for p, q in price_map.items()),
                   key=lambda x: x[0], reverse=descending)
        )
