"""Directional strategy for backtesting experiment_v7 signal logic.

Since the backtest engine only has 1-minute OHLCV candles (no live order-book
or trade-flow data), we proxy the live WebSocket signals using candle-derived
momentum indicators that correlate with ob_imbalance and trade_pressure_5m:

  Signal proxy
  ------------
  Bullish: close > open for N of last M candles  AND  volume > vol_ma
           (momentum in both price and volume — proxy for positive ob_imbalance
           and positive trade pressure)

  Bearish: close < open for N of last M candles  AND  volume > vol_ma

Entry
-----
  Market order at next candle's OPEN price (taker fee, guaranteed fill).

Exit
----
  TP: limit order at entry × (1 + tp_pct) — fills when HIGH ≥ TP price.
      Charged maker fee.
  SL: market order at entry × (1 - sl_pct) — fills when LOW ≤ SL price.
      Charged taker fee.
  Max-hold: forced market close at next candle OPEN after max_hold_candles.

One position at a time; cooldown of cooldown_candles after each close.

Grid search
-----------
  Vary ob_imbalance_thresh proxy (mapped to momentum_ratio: N/M required)
  and pressure_thresh proxy (mapped to vol_mult: volume must be > vol_ma × mult).
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from backtest.engine import BacktestEngine, BacktestResult, Candle

MAKER_FEE = 0.0002
TAKER_FEE = 0.00055


@dataclass
class DirectionalParams:
    tp_pct:            float = 0.008    # 0.8% take-profit
    sl_pct:            float = 0.004    # 0.4% stop-loss
    max_hold_candles:  int   = 20       # force-close after N 1-min candles
    cooldown_candles:  int   = 5        # wait N candles after closing before re-entering
    lookback:          int   = 5        # how many candles to evaluate for momentum
    momentum_ratio:    float = 0.6      # fraction of lookback candles that must agree (proxy for ob_imbalance_thresh)
    vol_mult:          float = 1.2      # volume must be > MA × vol_mult (proxy for pressure_thresh)
    vol_ma_period:     int   = 20       # period for volume moving average


class DirectionalStrategy:
    """Candle-based directional scalp strategy wired to BacktestEngine.

    Proxies the live WebSocket signal (ob_imbalance + trade_pressure_5m)
    using candle momentum and volume surge.
    """

    def __init__(self, p: DirectionalParams | None = None) -> None:
        self._p        = p or DirectionalParams()
        self._position: str | None = None     # "Long" or "Short"
        self._entry_price: float   = 0.0
        self._tp_price:    float   = 0.0
        self._sl_price:    float   = 0.0
        self._hold_count:  int     = 0
        self._cooldown:    int     = 0
        self._closes:      deque[float] = deque(maxlen=self._p.lookback + 1)
        self._opens:       deque[float] = deque(maxlen=self._p.lookback + 1)
        self._volumes:     deque[float] = deque(maxlen=self._p.vol_ma_period)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _vol_ma(self) -> float:
        if not self._volumes:
            return 0.0
        return sum(self._volumes) / len(self._volumes)

    def _signal(self, candle: Candle) -> str | None:
        """Return 'Buy', 'Sell', or None based on momentum proxy."""
        if len(self._closes) < self._p.lookback + 1:
            return None

        closes = list(self._closes)
        opens  = list(self._opens)
        n      = self._p.lookback

        up_count   = sum(1 for i in range(-n, 0) if closes[i] > opens[i])
        down_count = sum(1 for i in range(-n, 0) if closes[i] < opens[i])
        vol_ma     = self._vol_ma()
        vol_surge  = (candle.volume > vol_ma * self._p.vol_mult) if vol_ma > 0 else False
        required   = math.ceil(n * self._p.momentum_ratio)

        if up_count >= required and vol_surge:
            return "Buy"
        if down_count >= required and vol_surge:
            return "Sell"
        return None

    # ── main callback ─────────────────────────────────────────────────────────

    def on_candle(self, engine: BacktestEngine, candle: Candle) -> None:
        self._closes.append(candle.close)
        self._opens.append(candle.open)
        self._volumes.append(candle.volume)

        if self._cooldown > 0:
            self._cooldown -= 1

        if self._position is not None:
            self._hold_count += 1

            # Check TP: fills at candle HIGH for long, candle LOW for short
            if self._position == "Long" and candle.high >= self._tp_price:
                fee  = self._tp_price * abs(engine.position_qty()) * MAKER_FEE
                engine.cancel_all()
                engine._capital += abs(engine.position_qty()) * (self._tp_price - self._entry_price) - fee  # type: ignore[attr-defined]
                engine._total_fees += fee  # type: ignore[attr-defined]
                engine._n_trades   += 1   # type: ignore[attr-defined]
                engine._trade_pnls.append(  # type: ignore[attr-defined]
                    abs(engine.position_qty()) * (self._tp_price - self._entry_price) - fee)
                engine._position.qty = 0.0  # type: ignore[attr-defined]
                self._reset()
                self._cooldown = self._p.cooldown_candles
                return

            if self._position == "Short" and candle.low <= self._tp_price:
                fee  = self._tp_price * abs(engine.position_qty()) * MAKER_FEE
                engine.cancel_all()
                engine._capital += abs(engine.position_qty()) * (self._entry_price - self._tp_price) - fee  # type: ignore[attr-defined]
                engine._total_fees += fee  # type: ignore[attr-defined]
                engine._n_trades   += 1   # type: ignore[attr-defined]
                engine._trade_pnls.append(  # type: ignore[attr-defined]
                    abs(engine.position_qty()) * (self._entry_price - self._tp_price) - fee)
                engine._position.qty = 0.0  # type: ignore[attr-defined]
                self._reset()
                self._cooldown = self._p.cooldown_candles
                return

            # Check SL: fires at candle LOW for long, candle HIGH for short
            if self._position == "Long" and candle.low <= self._sl_price:
                fee  = self._sl_price * abs(engine.position_qty()) * TAKER_FEE
                engine.cancel_all()
                engine._capital += abs(engine.position_qty()) * (self._sl_price - self._entry_price) - fee  # type: ignore[attr-defined]
                engine._total_fees += fee  # type: ignore[attr-defined]
                engine._n_trades   += 1   # type: ignore[attr-defined]
                engine._trade_pnls.append(  # type: ignore[attr-defined]
                    abs(engine.position_qty()) * (self._sl_price - self._entry_price) - fee)
                engine._position.qty = 0.0  # type: ignore[attr-defined]
                self._reset()
                self._cooldown = self._p.cooldown_candles
                return

            if self._position == "Short" and candle.high >= self._sl_price:
                fee  = self._sl_price * abs(engine.position_qty()) * TAKER_FEE
                engine.cancel_all()
                engine._capital += abs(engine.position_qty()) * (self._entry_price - self._sl_price) - fee  # type: ignore[attr-defined]
                engine._total_fees += fee  # type: ignore[attr-defined]
                engine._n_trades   += 1   # type: ignore[attr-defined]
                engine._trade_pnls.append(  # type: ignore[attr-defined]
                    abs(engine.position_qty()) * (self._entry_price - self._sl_price) - fee)
                engine._position.qty = 0.0  # type: ignore[attr-defined]
                self._reset()
                self._cooldown = self._p.cooldown_candles
                return

            # Max-hold: force close at this candle's open
            if self._hold_count >= self._p.max_hold_candles:
                close_px = candle.open
                fee      = close_px * abs(engine.position_qty()) * TAKER_FEE
                pnl = abs(engine.position_qty()) * (
                    (close_px - self._entry_price) if self._position == "Long"
                    else (self._entry_price - close_px)
                ) - fee
                engine.cancel_all()
                engine._capital    += pnl   # type: ignore[attr-defined]
                engine._total_fees += fee   # type: ignore[attr-defined]
                engine._n_trades   += 1     # type: ignore[attr-defined]
                engine._trade_pnls.append(pnl)  # type: ignore[attr-defined]
                engine._position.qty = 0.0  # type: ignore[attr-defined]
                self._reset()
                self._cooldown = self._p.cooldown_candles
            return

        # ── no position — check for entry ─────────────────────────────────────
        if self._cooldown > 0:
            return

        side = self._signal(candle)
        if side is None:
            return

        # Enter at this candle's close (simulates next-open market order)
        entry_px = candle.close
        qty      = 1.0   # normalised; PnL is in % of notional
        fee_entry = entry_px * qty * TAKER_FEE

        engine._capital    -= fee_entry   # type: ignore[attr-defined]
        engine._total_fees += fee_entry   # type: ignore[attr-defined]

        if side == "Buy":
            self._position   = "Long"
            self._tp_price   = entry_px * (1.0 + self._p.tp_pct)
            self._sl_price   = entry_px * (1.0 - self._p.sl_pct)
            engine._position.qty       = qty    # type: ignore[attr-defined]
            engine._position.avg_price = entry_px  # type: ignore[attr-defined]
            engine._n_buy += 1  # type: ignore[attr-defined]
        else:
            self._position   = "Short"
            self._tp_price   = entry_px * (1.0 - self._p.tp_pct)
            self._sl_price   = entry_px * (1.0 + self._p.sl_pct)
            engine._position.qty       = -qty   # type: ignore[attr-defined]
            engine._position.avg_price = entry_px  # type: ignore[attr-defined]
            engine._n_sell += 1  # type: ignore[attr-defined]

        self._entry_price = entry_px
        self._hold_count  = 0

    def _reset(self) -> None:
        self._position    = None
        self._entry_price = 0.0
        self._tp_price    = 0.0
        self._sl_price    = 0.0
        self._hold_count  = 0


def run_directional_backtest(
    symbol:           str,
    candles:          list[dict],
    tp_pct:           float = 0.008,
    sl_pct:           float = 0.004,
    max_hold_candles: int   = 20,
    cooldown_candles: int   = 5,
    lookback:         int   = 5,
    momentum_ratio:   float = 0.6,
    vol_mult:         float = 1.2,
    initial_capital:  float = 500.0,
) -> BacktestResult:
    """Run a full directional backtest and return the result."""
    p = DirectionalParams(
        tp_pct=tp_pct, sl_pct=sl_pct,
        max_hold_candles=max_hold_candles,
        cooldown_candles=cooldown_candles,
        lookback=lookback,
        momentum_ratio=momentum_ratio,
        vol_mult=vol_mult,
    )
    strat  = DirectionalStrategy(p)
    engine = BacktestEngine(symbol=symbol, initial_capital=initial_capital)
    engine.run(candles, strat.on_candle)
    result = engine.result()
    result.params = {
        "tp_pct":           tp_pct,
        "sl_pct":           sl_pct,
        "momentum_ratio":   momentum_ratio,
        "vol_mult":         vol_mult,
        "lookback":         lookback,
        "max_hold_candles": max_hold_candles,
    }
    return result


def suggest_directional_params(results: list[BacktestResult]) -> dict:
    """Pick the best directional backtest result by Sharpe × win_rate."""
    if not results:
        return {}
    # Score = sharpe × win_rate — rewards both consistency and directionality
    best = max(results, key=lambda r: r.sharpe * r.win_rate if r.n_trades >= 10 else -999)
    return {
        "tp_pct":         best.params.get("tp_pct", 0.008),
        "sl_pct":         best.params.get("sl_pct", 0.004),
        "momentum_ratio": best.params.get("momentum_ratio", 0.6),
        "vol_mult":       best.params.get("vol_mult", 1.2),
        "lookback":       best.params.get("lookback", 5),
        "note": (
            f"based on backtest net_pnl={best.net_pnl:+.4f} "
            f"sharpe={best.sharpe:.3f} win_rate={best.win_rate*100:.1f}% "
            f"trades={best.n_trades} symbol={best.symbol}"
        ),
    }
