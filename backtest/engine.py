"""Custom event-driven backtesting engine for market making.

Replays 1-minute OHLCV candles as a synthetic 4-tick sequence:
  open → high or low (depending on direction guess) → low or high → close

Limit order fill model
-----------------------
  Buy  limit at price P fills if the candle LOW ≤ P.
  Sell limit at price P fills if the candle HIGH ≥ P.
  Fill price = the limit price (maker fill — realistic for limit books).

Fee model (VIP 0)
------------------
  Maker fee : 0.02% per side
  Taker fee : 0.055% per side  (used for market orders / forced unwinds)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class Candle:
    ts:       int     # Unix ms
    open:     float
    high:     float
    low:      float
    close:    float
    volume:   float


@dataclass
class Order:
    order_id: str
    side:     str      # "Buy" or "Sell"
    price:    float
    qty:      float
    is_taker: bool = False
    filled:   bool = False
    fill_price: float = 0.0


@dataclass
class Position:
    qty:        float = 0.0    # positive = long, negative = short
    avg_price:  float = 0.0

    def update(self, side: str, qty: float, fill_price: float) -> float:
        """Apply a fill and return the realised PnL (0 if increasing position)."""
        if side == "Buy":
            if self.qty < 0:
                # Closing short
                close_qty = min(qty, -self.qty)
                rpnl = close_qty * (self.avg_price - fill_price)
                self.qty      += close_qty
                qty           -= close_qty
                if self.qty == 0:
                    self.avg_price = 0.0
            if qty > 0:
                # Opening / increasing long
                total = self.qty + qty
                self.avg_price = (self.qty * self.avg_price + qty * fill_price) / total
                self.qty = total
                rpnl = 0.0
        else:  # Sell
            if self.qty > 0:
                close_qty = min(qty, self.qty)
                rpnl = close_qty * (fill_price - self.avg_price)
                self.qty      -= close_qty
                qty           -= close_qty
                if self.qty == 0:
                    self.avg_price = 0.0
            if qty > 0:
                total = abs(self.qty) + qty
                self.avg_price = (abs(self.qty) * self.avg_price + qty * fill_price) / total
                self.qty -= qty
                rpnl = 0.0
        return rpnl  # type: ignore[return-value]


@dataclass
class BacktestResult:
    symbol:         str
    days:           int
    total_pnl:      float
    total_fees:     float
    net_pnl:        float
    n_trades:       int
    n_buy:          int
    n_sell:         int
    max_drawdown:   float
    sharpe:         float
    win_rate:       float
    params:         dict = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"--- Backtest: {self.symbol} ({self.days}d) ---\n"
            f"  Net PnL   : {self.net_pnl:+.4f} USDT\n"
            f"  Total fees: {self.total_fees:.4f} USDT\n"
            f"  Trades    : {self.n_trades} (B:{self.n_buy} S:{self.n_sell})\n"
            f"  Win rate  : {self.win_rate*100:.1f}%\n"
            f"  Max DD    : {self.max_drawdown*100:.2f}%\n"
            f"  Sharpe    : {self.sharpe:.3f}\n"
        )


# ── Engine ────────────────────────────────────────────────────────────────────

class BacktestEngine:
    """Event-driven backtesting engine.

    Usage
    -----
      engine = BacktestEngine(symbol="BTCUSDT", initial_capital=1000.0)
      engine.run(candles, strategy_callback)
      result = engine.result()
    """

    MAKER_FEE = 0.0002   # 0.02%
    TAKER_FEE = 0.00055  # 0.055%

    def __init__(self, symbol: str, initial_capital: float = 1000.0) -> None:
        self._symbol   = symbol
        self._capital  = initial_capital
        self._position = Position()
        self._orders:  dict[str, Order] = {}
        self._next_id  = 0

        self._realised_pnl   = 0.0
        self._total_fees     = 0.0
        self._n_trades       = 0
        self._n_buy          = 0
        self._n_sell         = 0
        self._equity_curve:  list[float] = []
        self._trade_pnls:    list[float] = []

    # ── Strategy API ─────────────────────────────────────────────────────────

    def place_limit(self, side: str, price: float, qty: float) -> str:
        oid = f"bt_{self._next_id}"
        self._next_id += 1
        self._orders[oid] = Order(oid, side, price, qty)
        return oid

    def cancel(self, order_id: str) -> None:
        self._orders.pop(order_id, None)

    def cancel_all(self) -> None:
        self._orders.clear()

    def position_qty(self) -> float:
        return self._position.qty

    def mid_price(self) -> float:
        return self._last_mid

    # ── Simulation ────────────────────────────────────────────────────────────

    def run(self, candles: list[dict],
            on_candle: Callable[["BacktestEngine", Candle], None]) -> None:
        """Replay candles, call `on_candle` before each candle, then check fills."""
        self._last_mid = 0.0
        for raw in candles:
            c = Candle(**{k: raw[k] for k in ("ts", "open", "high", "low", "close", "volume")})
            self._last_mid = (c.high + c.low) / 2.0
            on_candle(self, c)
            self._process_fills(c)
            equity = self._capital + self._open_pnl(c.close)
            self._equity_curve.append(equity)

    def _process_fills(self, c: Candle) -> None:
        for oid, order in list(self._orders.items()):
            filled = False
            if order.side == "Buy" and c.low <= order.price:
                filled = True
                order.fill_price = order.price
            elif order.side == "Sell" and c.high >= order.price:
                filled = True
                order.fill_price = order.price

            if filled:
                fee = order.fill_price * order.qty * self.MAKER_FEE
                rpnl = self._position.update(order.side, order.qty, order.fill_price)
                self._realised_pnl += rpnl - fee
                self._total_fees   += fee
                self._capital      += rpnl - fee
                self._n_trades     += 1
                self._trade_pnls.append(rpnl - fee)
                if order.side == "Buy":
                    self._n_buy += 1
                else:
                    self._n_sell += 1
                order.filled = True
                del self._orders[oid]

    def _open_pnl(self, mark: float) -> float:
        if self._position.qty == 0:
            return 0.0
        if self._position.qty > 0:
            return self._position.qty * (mark - self._position.avg_price)
        return abs(self._position.qty) * (self._position.avg_price - mark)

    def result(self) -> BacktestResult:
        n = len(self._equity_curve)
        days = n / (24 * 60) if n else 1

        # Max drawdown
        peak = self._capital
        max_dd = 0.0
        for eq in self._equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak)

        # Sharpe (annualised, assume 1-min bars)
        if len(self._trade_pnls) > 1:
            import math
            avg = sum(self._trade_pnls) / len(self._trade_pnls)
            var = sum((p - avg) ** 2 for p in self._trade_pnls) / len(self._trade_pnls)
            std = math.sqrt(var)
            sharpe = (avg / std * math.sqrt(525_600)) if std > 0 else 0.0
        else:
            sharpe = 0.0

        wins = sum(1 for p in self._trade_pnls if p > 0)
        win_rate = (wins / len(self._trade_pnls)) if self._trade_pnls else 0.0

        return BacktestResult(
            symbol=self._symbol,
            days=int(days),
            total_pnl=self._realised_pnl,
            total_fees=self._total_fees,
            net_pnl=self._realised_pnl,
            n_trades=self._n_trades,
            n_buy=self._n_buy,
            n_sell=self._n_sell,
            max_drawdown=max_dd,
            sharpe=sharpe,
            win_rate=win_rate,
        )
