"""Market making strategy wired to BacktestEngine.

Strategy logic
--------------
  Every candle:
    1. Cancel stale quotes.
    2. Compute mid price from candle open.
    3. Apply inventory skew to half-spread.
    4. Post bid at  mid - half_spread - inventory_skew
       Post ask at  mid + half_spread + inventory_skew
    5. Hard inventory limit: skip quoting if position too large.

Minimum viable spread
---------------------
  Round-trip maker fee = 2 × 0.02% = 0.04%
  Add profit target (default 0.02%) → min half_spread = 0.03%
  So full spread = 0.06% of mid.

Outputs
-------
  After run(), call `suggest_params()` to get a dict of recommended
  live params for experiment_v6/params.json.
"""
from __future__ import annotations

from backtest.engine import BacktestEngine, BacktestResult, Candle

MAKER_FEE = 0.0002    # 0.02%
MIN_HALF_SPREAD_PCT = MAKER_FEE + 0.0001   # just above break-even


class MMStrategy:
    """Signal-agnostic symmetric market maker."""

    def __init__(self,
                 half_spread_pct:       float = 0.0003,   # 0.03%
                 max_inventory_usd:     float = 100.0,
                 inventory_skew_factor: float = 0.5,
                 quote_qty_usd:         float = 20.0) -> None:

        if half_spread_pct < MIN_HALF_SPREAD_PCT:
            half_spread_pct = MIN_HALF_SPREAD_PCT

        self.half_spread_pct       = half_spread_pct
        self.max_inventory_usd     = max_inventory_usd
        self.inventory_skew_factor = inventory_skew_factor
        self.quote_qty_usd         = quote_qty_usd

        self._bid_id: str | None = None
        self._ask_id: str | None = None

    def on_candle(self, engine: BacktestEngine, candle: Candle) -> None:
        # Cancel previous quotes every bar
        if self._bid_id:
            engine.cancel(self._bid_id)
            self._bid_id = None
        if self._ask_id:
            engine.cancel(self._ask_id)
            self._ask_id = None

        mid = candle.open
        if mid <= 0:
            return

        # Inventory skew: if long, widen ask / tighten bid to sell more
        pos_qty   = engine.position_qty()
        pos_usd   = pos_qty * mid
        skew_raw  = pos_usd / self.max_inventory_usd   # normalised in [-1, 1]
        skew_raw  = max(-1.0, min(1.0, skew_raw))
        skew_adj  = skew_raw * self.inventory_skew_factor * self.half_spread_pct

        # Skip quoting if inventory too extreme
        if abs(pos_usd) >= self.max_inventory_usd:
            return

        bid_price = mid * (1 - self.half_spread_pct - skew_adj)
        ask_price = mid * (1 + self.half_spread_pct - skew_adj)
        qty       = self.quote_qty_usd / mid

        self._bid_id = engine.place_limit("Buy",  round(bid_price, 8), qty)
        self._ask_id = engine.place_limit("Sell", round(ask_price, 8), qty)


def run_mm_backtest(symbol: str, candles: list[dict],
                    half_spread_pct: float = 0.0003,
                    max_inventory_usd: float = 100.0,
                    inventory_skew_factor: float = 0.5,
                    quote_qty_usd: float = 20.0,
                    initial_capital: float = 500.0) -> BacktestResult:
    """Run a full MM backtest and return the result."""
    strategy = MMStrategy(
        half_spread_pct=half_spread_pct,
        max_inventory_usd=max_inventory_usd,
        inventory_skew_factor=inventory_skew_factor,
        quote_qty_usd=quote_qty_usd,
    )
    engine = BacktestEngine(symbol=symbol, initial_capital=initial_capital)
    engine.run(candles, strategy.on_candle)
    result = engine.result()
    result.params = {
        "half_spread_pct":       half_spread_pct,
        "max_inventory_usd":     max_inventory_usd,
        "inventory_skew_factor": inventory_skew_factor,
        "quote_qty_usd":         quote_qty_usd,
    }
    return result


def suggest_params(results: list[BacktestResult]) -> dict:
    """Pick the best-performing result and return suggested live params."""
    if not results:
        return {}
    best = max(results, key=lambda r: r.net_pnl)
    return {
        "half_spread_pct":        best.params.get("half_spread_pct", 0.0003),
        "max_inventory_notional": best.params.get("max_inventory_usd", 100.0),
        "inventory_skew_factor":  best.params.get("inventory_skew_factor", 0.5),
        "note": f"based on backtest net_pnl={best.net_pnl:+.4f} "
                f"sharpe={best.sharpe:.3f} symbol={best.symbol}",
    }
