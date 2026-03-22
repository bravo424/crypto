"""
experiment_v4 — 1-min / 5-min dual-timeframe mean-reversion scalp.

Target: ≥10% net PnL per day on Bybit (450 USDT) and Bithumb (100,000 KRW).

Strategy design
---------------
  1. 5-min HTF trend gate (EMA9 / EMA21 + price-vs-EMA9 confirmation) applied to
     BOTH exchanges:
       Buy  gate: EMA9 > EMA21 AND last 5m close > EMA9  (confirmed uptrend)
       Sell gate: EMA9 < EMA21 AND last 5m close < EMA9  (confirmed downtrend)
       None      : trend ambiguous — no trade.
  2. On the 1-min chart, wait for a pullback/overshoot AGAINST the band:
       Long  : price ≤ lower BB AND RSI(7) < RSI_OS  → oversold dip inside an uptrend
       Short : price ≥ upper BB AND RSI(7) > RSI_OB  → overbought spike inside a downtrend
  3. Reversal wick confirmation: the candle must show early recovery pressure
     (lower wick ≥ 25% of range for Long, upper wick ≥ 25% for Short).
  4. Entry: PostOnly limit at TRAIL_OFFSET_ATR × ATR below mark (Buy) or above (Sell).
     This intentionally waits for a slightly deeper dip/bounce fill — coherent with
     mean-reversion entries and pays maker fees only.
  5. Exit:
       Bybit   — native TP/SL (MarkPrice trigger).
       Bithumb — GTC limit sell at TP; SL enforced by per-tick price poll.

Why mean-reversion (not momentum)
-----------------------------------
  The PostOnly entry places the limit BELOW mark for a Buy.  On a momentum
  breakout (price going up fast) this limit never fills or only fills when the
  breakout reverses — the worst possible entry.  Mean-reversion entries (buying
  a dip) fill naturally as price overshoots downward and then recovers.
  The HTF gate (both exchanges) ensures we only buy dips inside confirmed
  uptrends and short bounces inside confirmed downtrends.
  Bithumb is long-only (spot), so it only acts on Buy signals.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path

from dotenv import load_dotenv
from pybit.exceptions import InvalidRequestError
from pybit.unified_trading import HTTP

from upbit_bybit_bot.config import load_settings
from upbit_bybit_bot.telegram_alerter import TelegramAlerter

LOGGER = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
if str(HERE.parent / "experiment_v1") not in sys.path:
    sys.path.insert(0, str(HERE.parent / "experiment_v1"))
from bithumb_exchange import BithumbSession  # noqa: E402

STATE_FILE      = Path("data/experiment_v4_bybit_state.json")
CHECK_INTERVAL  = 30       # seconds — 16 syms × 1.0s sleep = 16s burst; 30s gap ≈ 0.53 calls/s average, well under Bybit 10/s
UPDATE_INTERVAL = 1800     # 30-min Telegram position summary

# ── mutable globals (hot-reloaded from params.json every tick) ────────────────
CANDLE_INTERVAL:      str   = "1"
HTF_INTERVAL:         str   = "5"
ATR_PERIOD:           int   = 14
RSI_PERIOD:           int   = 7
RSI_OB:               float = 70.0
RSI_OS:               float = 30.0
BB_PERIOD:            int   = 20
BB_STD:               float = 2.0
VMULT:                float = 1.5
VLOOKBACK:            int   = 10
EMA_FAST:             int   = 9
EMA_SLOW:             int   = 21
TP_ATR_MULT:          float = 1.8
SL_ATR_MULT:          float = 0.9
TRAIL_OFFSET_ATR:     float = 0.15
STALE_MULT:           float = 2.5
MAX_ORDER_AGE_MIN:    int   = 3
MAX_SL_PCT:           float = 0.04
MAX_TP_PCT:           float = 0.08
MIN_SL_PCT:           float = 0.003   # minimum SL distance from entry (prevents near-zero SL)
MIN_ATR_PCT:          float = 0.0002
LEVERAGE:             int   = 10
MAXRISKPCT:           float = 0.04
FIXED_NOTIONAL_USDT:  float = 0.0
NOTIONAL_CAP_USDT:    float = 430.0
NORDERSPERHOUR:       int   = 20
MAX_OPEN_POSITIONS:   int   = 5
TIME_IN_FORCE:        str   = "PostOnly"
BITHUMB_TICK_EXIT:    int   = 3
BITHUMB_NOTIONAL_KRW: float = 95000.0
BITHUMB_SL_FAILOVER_SEC: int = 12   # if Bithumb SL-limit doesn't fill quickly, force market exit
MAX_DAILY_DRAWDOWN:   float = 0.20   # halt new entries if day's equity drops > 5%
VOL_REGIME_RATIO:     float = 0.35   # 1m_ATR/5m_ATR threshold: above → volatile (use 1m), below → quiet (use 5m)
PROFIT_LOCK_TRIGGER_PCT: float = 0.02  # once profit ≥ 2%, move SL to lock-in level
PROFIT_LOCK_SL_PCT:      float = 0.01  # locked SL = entry + 1% (never below entry)
MACRO_INTERVAL:        str   = "60"      # 1h candles for second trend gate
MAX_LOSS_STREAK:       int   = 3         # consecutive SL hits before pause
LOSS_STREAK_PAUSE_MIN: int   = 60        # pause duration in minutes
PANIC_DROP_PCT:        float = 0.03      # BTC 4h drop % that blocks Long entries
FEE_RATE:              float = 0.0002    # per-side maker fee; TP floor = 2×fee_rate×entry

_CANDLE_INTERVAL_SECS: int  = 60


def load_params(exchange: str = "bybit") -> None:
    global CANDLE_INTERVAL, HTF_INTERVAL, ATR_PERIOD, RSI_PERIOD, RSI_OB, RSI_OS
    global BB_PERIOD, BB_STD, VMULT, VLOOKBACK, EMA_FAST, EMA_SLOW
    global TP_ATR_MULT, SL_ATR_MULT, TRAIL_OFFSET_ATR, STALE_MULT, MAX_ORDER_AGE_MIN
    global MAX_SL_PCT, MAX_TP_PCT, MIN_ATR_PCT, LEVERAGE, MAXRISKPCT
    global FIXED_NOTIONAL_USDT, NOTIONAL_CAP_USDT, NORDERSPERHOUR, MAX_OPEN_POSITIONS
    global TIME_IN_FORCE, BITHUMB_TICK_EXIT, BITHUMB_NOTIONAL_KRW, BITHUMB_SL_FAILOVER_SEC, _CANDLE_INTERVAL_SECS
    global MAX_DAILY_DRAWDOWN, VOL_REGIME_RATIO, PROFIT_LOCK_TRIGGER_PCT, PROFIT_LOCK_SL_PCT
    global MIN_SL_PCT
    global MACRO_INTERVAL, MAX_LOSS_STREAK, LOSS_STREAK_PAUSE_MIN, PANIC_DROP_PCT
    global FEE_RATE
    path = HERE / "params.json"
    with path.open(encoding="utf-8") as fh:
        p = json.load(fh)
    overrides = p.get("exchanges", {}).get(exchange, {})
    p = {**p, **overrides}
    CANDLE_INTERVAL      = str(p.get("candle_interval",        CANDLE_INTERVAL))
    HTF_INTERVAL         = str(p.get("htf_interval",           HTF_INTERVAL))
    ATR_PERIOD           = int(p.get("atr_period",             ATR_PERIOD))
    RSI_PERIOD           = int(p.get("rsi_period",             RSI_PERIOD))
    RSI_OB               = float(p.get("rsi_ob",               RSI_OB))
    RSI_OS               = float(p.get("rsi_os",               RSI_OS))
    BB_PERIOD            = int(p.get("bb_period",              BB_PERIOD))
    BB_STD               = float(p.get("bb_std",               BB_STD))
    VMULT                = float(p.get("vmult",                VMULT))
    VLOOKBACK            = int(p.get("vlookback",              VLOOKBACK))
    EMA_FAST             = int(p.get("ema_fast",               EMA_FAST))
    EMA_SLOW             = int(p.get("ema_slow",               EMA_SLOW))
    TP_ATR_MULT          = float(p.get("tp_atr_mult",          TP_ATR_MULT))
    SL_ATR_MULT          = float(p.get("sl_atr_mult",          SL_ATR_MULT))
    TRAIL_OFFSET_ATR     = float(p.get("trail_offset_atr",     TRAIL_OFFSET_ATR))
    STALE_MULT           = float(p.get("stale_mult",           STALE_MULT))
    MAX_ORDER_AGE_MIN    = int(p.get("max_order_age_min",      MAX_ORDER_AGE_MIN))
    MAX_SL_PCT           = float(p.get("max_sl_pct",           MAX_SL_PCT))
    MAX_TP_PCT           = float(p.get("max_tp_pct",           MAX_TP_PCT))
    MIN_SL_PCT           = float(p.get("min_sl_pct",           MIN_SL_PCT))
    MIN_ATR_PCT          = float(p.get("min_atr_pct",          MIN_ATR_PCT))
    LEVERAGE             = int(p.get("leverage",               LEVERAGE))
    MAXRISKPCT           = float(p.get("maxriskpct",           MAXRISKPCT))
    FIXED_NOTIONAL_USDT  = float(p.get("fixed_notional_usdt",  FIXED_NOTIONAL_USDT))
    NOTIONAL_CAP_USDT    = float(p.get("notional_cap_usdt",    NOTIONAL_CAP_USDT))
    NORDERSPERHOUR       = int(p.get("nordersperhour",         NORDERSPERHOUR))
    MAX_OPEN_POSITIONS   = int(p.get("max_open_positions",     MAX_OPEN_POSITIONS))
    TIME_IN_FORCE        = str(p.get("time_in_force",          TIME_IN_FORCE))
    BITHUMB_TICK_EXIT    = int(p.get("bithumb_tick_exit",      BITHUMB_TICK_EXIT))
    BITHUMB_NOTIONAL_KRW = float(p.get("bithumb_notional_krw", BITHUMB_NOTIONAL_KRW))
    BITHUMB_SL_FAILOVER_SEC = int(p.get("bithumb_sl_failover_sec", BITHUMB_SL_FAILOVER_SEC))
    MAX_DAILY_DRAWDOWN   = float(p.get("max_daily_drawdown_pct", MAX_DAILY_DRAWDOWN))
    VOL_REGIME_RATIO     = float(p.get("vol_regime_ratio",      VOL_REGIME_RATIO))
    PROFIT_LOCK_TRIGGER_PCT = float(p.get("profit_lock_trigger_pct", PROFIT_LOCK_TRIGGER_PCT))
    PROFIT_LOCK_SL_PCT      = float(p.get("profit_lock_sl_pct",      PROFIT_LOCK_SL_PCT))
    MACRO_INTERVAL          = str(p.get("macro_interval",         MACRO_INTERVAL))
    MAX_LOSS_STREAK         = int(p.get("max_loss_streak",        MAX_LOSS_STREAK))
    LOSS_STREAK_PAUSE_MIN   = int(p.get("loss_streak_pause_min",  LOSS_STREAK_PAUSE_MIN))
    PANIC_DROP_PCT          = float(p.get("panic_drop_pct",       PANIC_DROP_PCT))
    FEE_RATE                = float(p.get("fee_rate",             FEE_RATE))
    _CANDLE_INTERVAL_SECS   = int(CANDLE_INTERVAL) * 60


# ── symbols ───────────────────────────────────────────────────────────────────

def load_symbols(exchange: str = "bybit") -> list[tuple[str, int | None]]:
    path = HERE / f"symbols_{exchange}.json"
    if not path.exists():
        raise FileNotFoundError(f"Symbol file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        entries = json.load(fh)["symbols"]
    result: list[tuple[str, int | None]] = []
    for entry in entries:
        if isinstance(entry, str):
            result.append((entry, None))
        else:
            result.append((entry["symbol"], entry.get("leverage")))
    return result


# ── persistent state ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {
        "open_positions":         {},
        "pending_orders":         {},
        "bithumb_pending_entries": {},
        "signal_times":           {},
        "processed_candles":      {},
        "last_update_alert":      None,
        "daily_start_equity":     None,
        "daily_date":             None,
        "loss_streak":            0,
        "loss_streak_pause_until": None,
    }


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ── indicator maths ───────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return float("nan")
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _atr(highs: list[float], lows: list[float], closes: list[float],
         period: int) -> float:
    if len(highs) < period + 1:
        return float("nan")
    trs = [
        max(highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]))
        for i in range(1, len(highs))
    ]
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _rsi(closes: list[float], period: int) -> float:
    """Wilder RSI.  Needs at least period+1 values."""
    if len(closes) < period + 1:
        return float("nan")
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _bollinger(closes: list[float], period: int, std: float) -> tuple[float, float, float]:
    """Return (mid, upper, lower) Bollinger Bands on the last `period` closes."""
    if len(closes) < period:
        return float("nan"), float("nan"), float("nan")
    window = closes[-period:]
    mid    = sum(window) / period
    var    = sum((x - mid) ** 2 for x in window) / period
    sigma  = var ** 0.5
    return mid, mid + std * sigma, mid - std * sigma


# ── 5-min HTF trend ───────────────────────────────────────────────────────────

def check_htf_trend(session, symbol: str) -> str | None:
    """Return 'Buy', 'Sell', or None.

    Requires BOTH conditions to be true simultaneously:
      Buy  : EMA9 > EMA21  AND  last 5m close > EMA9
      Sell : EMA9 < EMA21  AND  last 5m close < EMA9

    Requiring price to still be on the correct side of EMA9 filters out the
    1-2 hour window after a trend reversal where EMA9/21 still look bullish
    but price has already broken through EMA9 — exactly when mean-reversion
    Buys become falling-knife trades.
    Returns None (skip trade) whenever the picture is ambiguous.
    """
    limit = EMA_SLOW + 5
    try:
        resp = session.get_kline(category="linear", symbol=symbol,
                                 interval=HTF_INTERVAL, limit=limit)
    except Exception:
        return None
    candles = resp["result"]["list"]
    if len(candles) < limit:
        return None
    closed = list(reversed(candles[1:]))
    closes = [float(c[4]) for c in closed]
    fast = _ema(closes, EMA_FAST)
    slow = _ema(closes, EMA_SLOW)
    if fast != fast or slow != slow:
        return None
    last_close = closes[-1]
    if fast > slow and last_close > fast:
        return "Buy"
    if fast < slow and last_close < fast:
        return "Sell"
    return None   # trend ambiguous — skip


# ── 1h macro trend gate ───────────────────────────────────────────────────────

def check_macro_trend(session, symbol: str) -> str | None:
    """1h EMA9/EMA21 macro trend gate — second, higher-timeframe confirmation.

    Buy  signals allowed only when 1h EMA9 > EMA21 (macro uptrend).
    Sell signals allowed only when 1h EMA9 < EMA21 (macro downtrend).
    Returns None if data unavailable — callers treat None as 'no opinion, allow'.
    """
    limit = EMA_SLOW + 5
    try:
        resp = session.get_kline(category="linear", symbol=symbol,
                                 interval=MACRO_INTERVAL, limit=limit)
    except Exception:
        return None
    candles = resp["result"]["list"]
    if len(candles) < limit:
        return None
    closed = list(reversed(candles[1:]))
    closes = [float(c[4]) for c in closed]
    fast = _ema(closes, EMA_FAST)
    slow = _ema(closes, EMA_SLOW)
    if fast != fast or slow != slow:
        return None
    if fast > slow:
        return "Buy"
    if fast < slow:
        return "Sell"
    return None


def _is_panic_market(session) -> bool:
    """Return True if BTCUSDT has dropped > PANIC_DROP_PCT over the last
    4 macro-timeframe candles (default: 4 × 1h = 4h lookback).

    Blocks Long entries when the broader market is in confirmed freefall —
    mean-reversion Buys fail statistically in panic conditions.
    Returns False on any error so trading is not unnecessarily blocked.
    """
    if PANIC_DROP_PCT <= 0:
        return False
    try:
        resp = session.get_kline(category="linear", symbol="BTCUSDT",
                                 interval=MACRO_INTERVAL, limit=6)
    except Exception:
        return False
    candles = resp["result"]["list"]
    if len(candles) < 5:
        return False
    closed = list(reversed(candles[1:]))   # oldest → newest, excludes open candle
    price_ref = float(closed[-4][4])       # close 4 candles ago
    price_now = float(closed[-1][4])       # most recent closed candle
    if price_ref <= 0:
        return False
    drop = (price_ref - price_now) / price_ref
    if drop >= PANIC_DROP_PCT:
        LOGGER.info("Panic market: BTC -%.2f%% over last 4×%sh candles",
                    drop * 100, MACRO_INTERVAL)
    return drop >= PANIC_DROP_PCT


# ── adaptive-timeframe signal ────────────────────────────────────────────────

def check_signal(session, symbol: str,
                interval: str | None = None) -> tuple[str | None, str, float]:
    """Evaluate the last closed candle on `interval` for a mean-reversion entry.

    `interval` defaults to CANDLE_INTERVAL ("1" min).  Pass "5" for the 5-min check.
    Returns (side_or_None, candle_ts_ms_str, atr).
    ATR is always returned even when no signal fires — callers use it for the
    volatility-regime calculation.

    Signal conditions (all must be true):
      1. Volume spike ≥ VMULT × rolling avg.
      2a. Long  : RSI(7) < RSI_OS AND close ≤ lower BB  (oversold dip)
      2b. Short : RSI(7) > RSI_OB AND close ≥ upper BB  (overbought spike)
      3. Reversal wick: lower wick ≥ 25% of range (Long) or upper wick ≥ 25% (Short),
         confirming intra-bar recovery pressure before the candle closed.
    """
    _interval = interval or CANDLE_INTERVAL
    need = max(BB_PERIOD, ATR_PERIOD + 1, RSI_PERIOD + 1, VLOOKBACK + 2) + 5
    try:
        resp = session.get_kline(category="linear", symbol=symbol,
                                 interval=_interval, limit=need)
    except Exception as err:
        LOGGER.warning("get_kline failed %s (%sm): %s", symbol, _interval, err)
        return None, "", float("nan")

    candles = resp["result"]["list"]
    if len(candles) < need:
        return None, "", float("nan")

    closed  = list(reversed(candles[1:]))   # oldest→newest; [0] still forming
    highs   = [float(c[2]) for c in closed]
    lows    = [float(c[3]) for c in closed]
    closes  = [float(c[4]) for c in closed]
    volumes = [float(c[5]) for c in closed]
    candle_ts = str(candles[1][0])

    # Compute ATR upfront — always returned so callers can detect the vol regime
    # even when the signal itself doesn't fire.
    atr = _atr(highs, lows, closes, ATR_PERIOD)

    # 1. Volume spike
    avg_vol = sum(volumes[-(VLOOKBACK + 1):-1]) / VLOOKBACK if VLOOKBACK > 0 else 1.0
    if avg_vol <= 0 or volumes[-1] < VMULT * avg_vol:
        return None, candle_ts, atr

    # 2. RSI + Bollinger Band extreme
    rsi_val   = _rsi(closes, RSI_PERIOD)
    _mid, bb_up, bb_lo = _bollinger(closes, BB_PERIOD, BB_STD)
    price     = closes[-1]

    if rsi_val != rsi_val:   # nan
        return None, candle_ts, atr

    if rsi_val < RSI_OS and price <= bb_lo:
        raw_side: str | None = "Buy"   # oversold dip — mean-reversion Long
    elif rsi_val > RSI_OB and price >= bb_up:
        raw_side = "Sell"              # overbought spike — mean-reversion Short
    else:
        return None, candle_ts, atr

    # Reversal wick confirmation: the candle must show early recovery pressure,
    # not close at the very extreme (which would be trend continuation).
    # Long  → lower wick ≥ 25% of range: buyers stepped in during the candle.
    # Short → upper wick ≥ 25% of range: sellers rejected the high.
    _c_range = highs[-1] - lows[-1]
    if _c_range > 0:
        if raw_side == "Buy"  and (closes[-1] - lows[-1])  / _c_range < 0.25:
            return None, candle_ts, atr
        if raw_side == "Sell" and (highs[-1] - closes[-1]) / _c_range < 0.25:
            return None, candle_ts, atr

    if atr != atr or atr <= 0:
        return None, candle_ts, float("nan")

    # HTF check is intentionally NOT done here — caller does it only when
    # side is not None, saving one kline call per symbol per tick.
    return raw_side, candle_ts, atr


# ── instrument / price helpers ────────────────────────────────────────────────

def get_instrument(session, symbol: str) -> dict | None:
    try:
        resp = session.get_instruments_info(category="linear", symbol=symbol)
    except InvalidRequestError:
        return None
    items = resp.get("result", {}).get("list", [])
    return items[0] if items else None


def get_mark_price(session, symbol: str) -> float | None:
    resp  = session.get_tickers(category="linear", symbol=symbol)
    items = resp.get("result", {}).get("list", [])
    if not items:
        return None
    return float(items[0].get("markPrice") or 0) or None


def get_wallet_equity(session) -> float:
    resp = session.get_wallet_balance(accountType="UNIFIED")
    return float(resp["result"]["list"][0].get("totalEquity") or 0)


def get_open_positions(session) -> dict[str, dict]:
    resp = session.get_positions(category="linear", settleCoin="USDT")
    return {
        item["symbol"]: item
        for item in resp["result"]["list"]
        if float(item.get("size") or 0) > 0
    }


# ── price rounding ────────────────────────────────────────────────────────────

def _round_down(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def _round_up(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_UP) * tick


def _round_qty(qty: Decimal, step: Decimal) -> Decimal:
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def _trail_price(side: str, mark: float, atr: float, tick_size: str) -> Decimal:
    p      = Decimal(str(mark))
    tick   = Decimal(tick_size)
    offset = Decimal(str(TRAIL_OFFSET_ATR)) * Decimal(str(atr))
    if side == "Buy":
        return _round_down(p - offset, tick)
    return _round_up(p + offset, tick)


def _is_stale(pending: dict, mark: float) -> bool:
    limit_price = float(pending.get("limit_price", mark))
    atr         = float(pending.get("atr", 0))
    if atr <= 0:
        return False
    return abs(mark - limit_price) > STALE_MULT * TRAIL_OFFSET_ATR * atr


# ── TP/SL calculation ─────────────────────────────────────────────────────────

def calc_tp_sl(side: str, entry: float, atr: float,
               tick_size: str) -> tuple[str, str]:
    tick   = Decimal(tick_size)
    e      = Decimal(str(entry))
    tp_d   = Decimal(str(atr)) * Decimal(str(TP_ATR_MULT))
    sl_d   = Decimal(str(atr)) * Decimal(str(SL_ATR_MULT))
    if side == "Buy":
        tp = _round_down(e + tp_d, tick)
        sl = _round_down(e - sl_d, tick)
        # cap: TP not more than MAX_TP_PCT above entry, SL not more than MAX_SL_PCT below
        tp = min(tp, _round_down(e * (1 + Decimal(str(MAX_TP_PCT))), tick))
        sl = max(sl, _round_down(e * (1 - Decimal(str(MAX_SL_PCT))), tick))
        # floor: SL must be at least MIN_SL_PCT below entry (never too tight)
        sl = min(sl, _round_down(e * (1 - Decimal(str(MIN_SL_PCT))), tick))
        # fee floor: TP must be far enough above entry to cover open+close fees
        if FEE_RATE > 0:
            tp = max(tp, _round_up(e * (1 + Decimal(str(2 * FEE_RATE))), tick))
    else:
        tp = _round_down(e - tp_d, tick)
        # Short SL is above entry — use _round_up to stay safely above entry
        sl = _round_up(e + sl_d, tick)
        tp = max(tp, _round_down(e * (1 - Decimal(str(MAX_TP_PCT))), tick))
        sl = min(sl, _round_up(e * (1 + Decimal(str(MAX_SL_PCT))), tick))
        # floor: SL must be at least MIN_SL_PCT above entry
        sl = max(sl, _round_up(e * (1 + Decimal(str(MIN_SL_PCT))), tick))
        # fee floor: TP must be far enough below entry to cover open+close fees
        if FEE_RATE > 0:
            tp = min(tp, _round_down(e * (1 - Decimal(str(2 * FEE_RATE))), tick))
    return format(tp.normalize(), "f"), format(sl.normalize(), "f")


# ── position sizing ───────────────────────────────────────────────────────────

def calc_qty(equity: float, sl_distance: float, qty_step: str,
             min_qty: float, entry: float,
             notional_cap: float | None = None) -> str:
    if sl_distance <= 0:
        return "0"
    cap = notional_cap if notional_cap is not None else NOTIONAL_CAP_USDT
    if FIXED_NOTIONAL_USDT > 0:
        capped = min(FIXED_NOTIONAL_USDT, cap)
    else:
        capped = min(MAXRISKPCT * equity / sl_distance * entry, cap)
    step  = Decimal(qty_step)
    qty   = _round_qty(Decimal(str(capped)) / Decimal(str(entry)), step)
    min_d = Decimal(str(min_qty))
    if qty < min_d:
        qty = min_d
    return format(qty.normalize(), "f")


# ── frequency guard ───────────────────────────────────────────────────────────

def _freq_ok(state: dict, symbol: str, now: datetime) -> bool:
    window_start = now - timedelta(hours=1)
    times  = state["signal_times"].get(symbol, [])
    recent = [t for t in times
              if datetime.fromisoformat(t).replace(tzinfo=UTC) >= window_start]
    state["signal_times"][symbol] = recent
    if len(recent) >= NORDERSPERHOUR:
        return False
    if MAX_OPEN_POSITIONS > 0:
        total = len(state["open_positions"]) + len(state.get("pending_orders", {}))
        if total >= MAX_OPEN_POSITIONS:
            return False
    return True


def _record_signal(state: dict, symbol: str, now: datetime) -> None:
    state["signal_times"].setdefault(symbol, []).append(now.isoformat())


# ── profit-lock (breakeven) stop ─────────────────────────────────────────────

def _check_profit_lock(session, sym: str, tracked: dict, current: float,
                       is_spot: bool, dry_run: bool,
                       alerter=None) -> None:
    """Once unrealised profit ≥ PROFIT_LOCK_TRIGGER_PCT, move SL to entry + PROFIT_LOCK_SL_PCT.

    Only fires once per position (flag 'profit_locked' stored in tracked dict).
    For Bybit: calls set_trading_stop to move the exchange-native SL.
    For Bithumb: updates sl_price in state so the SL-poll loop picks it up.

    State is updated BEFORE the exchange API call so the SL-poll / repair loop
    use the locked value even if the API call fails this cycle (it will retry
    next cycle because profit_locked is not set until the call succeeds).
    """
    if tracked.get("profit_locked"):
        return
    if PROFIT_LOCK_TRIGGER_PCT <= 0:
        return

    entry = float(tracked.get("entry_price") or 0)
    side  = tracked.get("side", "Buy")
    if entry <= 0 or current <= 0:
        return

    if side == "Buy":
        profit_pct = (current - entry) / entry
        locked_sl  = entry * (1 + PROFIT_LOCK_SL_PCT)
    else:
        profit_pct = (entry - current) / entry
        locked_sl  = entry * (1 - PROFIT_LOCK_SL_PCT)

    if profit_pct < PROFIT_LOCK_TRIGGER_PCT:
        return

    inst      = get_instrument(session, sym)
    tick_size = inst.get("priceFilter", {}).get("tickSize", "0.01") if inst else "0.01"
    tick      = Decimal(tick_size)
    locked_sl_str = format(_round_down(Decimal(str(locked_sl)), tick).normalize(), "f")

    msg = (f"🔒 <b>Profit-lock</b> {side} {sym}\n"
           f"profit <b>{profit_pct*100:.2f}%</b> ≥ {PROFIT_LOCK_TRIGGER_PCT*100:.1f}% trigger\n"
           f"SL moved → <code>{locked_sl_str}</code>  (entry+{PROFIT_LOCK_SL_PCT*100:.1f}%)")
    LOGGER.info("Profit-lock triggered %s %s: profit=%.2f%%  SL → %s  (entry+%.1f%%)",
                side, sym, profit_pct * 100, locked_sl_str, PROFIT_LOCK_SL_PCT * 100)
    if alerter:
        try:
            alerter.send(msg)
        except Exception:
            pass

    # Update sl_price now so SL-poll / repair loop use locked value even on API failure
    tracked["sl_price"] = locked_sl_str

    if not dry_run:
        if not is_spot:
            # Include takeProfit so Bybit doesn't interpret the omission as "clear TP".
            # Without it, set_trading_stop(stopLoss=x) resets native TP → 0, which
            # triggers the repair loop to re-place the TP order every subsequent cycle.
            tp_price_str = tracked.get("tp_price", "")
            try:
                kw = dict(
                    category="linear", symbol=sym,
                    stopLoss=locked_sl_str,
                    slTriggerBy="MarkPrice",
                    positionIdx=0,
                )
                if tp_price_str:
                    kw["takeProfit"]   = tp_price_str
                    kw["tpTriggerBy"]  = "MarkPrice"
                session.set_trading_stop(**kw)
            except Exception as err:
                LOGGER.warning("profit-lock set_trading_stop failed %s (will retry): %s", sym, err)
                return  # sl_price already updated; profit_locked not set → retries next cycle
    else:
        LOGGER.info("[DRY RUN] profit-lock would move SL → %s for %s", locked_sl_str, sym)

    # Mark as done only after successful API call (or dry-run / Bithumb)
    tracked["profit_locked"] = True


# ── 5-min ATR for TP/SL sizing ──────────────────────────────────────────────

def _get_htf_atr(session, symbol: str) -> float:
    """Return ATR(14) computed on 5-min candles.

    Used for TP/SL distances so that stops are sized to 5-min volatility
    rather than 1-min noise.  Falls back to nan on any error; callers
    should fall back to the 1-min ATR in that case.
    """
    need = ATR_PERIOD + 2
    try:
        resp = session.get_kline(category="linear", symbol=symbol,
                                 interval=HTF_INTERVAL, limit=need)
    except Exception:
        return float("nan")
    candles = resp.get("result", {}).get("list", [])
    if len(candles) < need:
        return float("nan")
    closed = list(reversed(candles[1:]))
    highs  = [float(c[2]) for c in closed]
    lows   = [float(c[3]) for c in closed]
    closes = [float(c[4]) for c in closed]
    return _atr(highs, lows, closes, ATR_PERIOD)


# ── clock sync ────────────────────────────────────────────────────────────────

def _sync_pybit_clock(testnet: bool = False) -> None:
    import pybit._helpers as _h
    tmp      = HTTP(testnet=testnet)
    resp     = tmp.get_server_time()
    srv_ms   = int(resp["result"]["timeNano"]) // 1_000_000
    local_ms = _h.generate_timestamp()
    offset   = srv_ms - local_ms
    LOGGER.info("Clock sync: offset=%+dms", offset)
    _orig = _h.generate_timestamp
    _h.generate_timestamp = lambda: _orig() + offset


# ── place / refresh entry order (Bybit) ──────────────────────────────────────

def _place_entry_bybit(session: HTTP, symbol: str, side: str, atr: float,
                       dry_run: bool, lev_override: int | None,
                       now: datetime, signal_at_iso: str | None = None) -> dict | None:
    lev  = lev_override if lev_override is not None else LEVERAGE
    inst = get_instrument(session, symbol)
    if not inst:
        return None
    mark = get_mark_price(session, symbol)
    if mark is None:
        return None

    lot       = inst.get("lotSizeFilter", {})
    tick_size = inst.get("priceFilter", {}).get("tickSize", "0.01")
    qty_step  = lot.get("qtyStep", "1")
    min_qty   = float(lot.get("minOrderQty", "0"))
    min_not   = float(lot.get("minNotionalValue", "1"))

    if MIN_ATR_PCT > 0 and atr / mark < MIN_ATR_PCT:
        LOGGER.warning("Skipping %s: ATR %.4f%% < min %.4f%%",
                       symbol, atr / mark * 100, MIN_ATR_PCT * 100)
        return None

    try:
        equity = get_wallet_equity(session)
    except Exception as err:
        LOGGER.warning("Cannot fetch equity for %s: %s", symbol, err)
        return None

    limit_px = _trail_price(side, mark, atr, tick_size)

    # Use 5-min ATR for TP/SL so stops survive 1-min noise while the
    # mean-reversion trade has time to complete on the 5-min timescale.
    # 1-min ATR is still used above for the entry offset (precision fill).
    htf_atr = _get_htf_atr(session, symbol)
    if not (htf_atr == htf_atr and htf_atr > 0):  # NaN or zero
        LOGGER.warning("Skipping %s: HTF ATR unavailable (%.6f) — not trading without proper TP/SL sizing",
                       symbol, htf_atr)
        return None
    tp_sl_atr = htf_atr
    LOGGER.debug("%s ATR  1m=%.6f  htf=%.6f", symbol, atr, htf_atr)

    tp_price, sl_price = calc_tp_sl(side, float(limit_px), tp_sl_atr, tick_size)

    sl_dist = abs(float(limit_px) - float(sl_price))
    if sl_dist <= 0:
        return None

    qty_str  = calc_qty(equity, sl_dist, qty_step, min_qty, float(limit_px))
    if float(qty_str) * float(limit_px) < min_not:
        LOGGER.warning("Skipping %s: notional below exchange minimum", symbol)
        return None

    signal_at = signal_at_iso or now.isoformat()

    if dry_run:
        LOGGER.info("[DRY RUN] QUEUE %s %s  limit=%s  tp=%s  sl=%s  1m_atr=%.6f  5m_atr=%.6f",
                    side, symbol, limit_px, tp_price, sl_price, atr, tp_sl_atr)
        return {
            "order_id":    "dry",
            "placed_at":   now.isoformat(),
            "signal_at":   signal_at,
            "limit_price": str(limit_px),
            "side":        side,
            "atr":         atr,
            "tp_price":    tp_price,
            "sl_price":    sl_price,
            "qty":         qty_str,
        }

    try:
        session.set_leverage(category="linear", symbol=symbol,
                             buyLeverage=str(lev), sellLeverage=str(lev))
    except Exception:
        pass

    # SL only via native stopLoss (guaranteed fill, taker fee acceptable for safety).
    # TP is placed separately as a GTC limit sell after the position fills — maker fee.
    resp = session.place_order(
        category="linear", symbol=symbol,
        side=side, orderType="Limit",
        price=str(limit_px), qty=qty_str,
        timeInForce=TIME_IN_FORCE,
        stopLoss=sl_price,
        slTriggerBy="MarkPrice",
    )
    order_id = resp["result"]["orderId"]
    LOGGER.info("Queued %s %s  limit=%s  sl=%s  (TP placed as GTC limit after fill)  atr=%.6f  qty=%s  id=%s",
                side, symbol, limit_px, sl_price, atr, qty_str, order_id)
    return {
        "order_id":    order_id,
        "placed_at":   now.isoformat(),
        "signal_at":   signal_at,
        "limit_price": str(limit_px),
        "side":        side,
        "atr":         atr,
        "tp_price":    tp_price,
        "sl_price":    sl_price,
        "qty":         qty_str,
    }


# ── TP/SL repair (Bybit post-restart) ────────────────────────────────────────

def _apply_tp_sl(session: HTTP, symbol: str, state: dict,
                 tp: str, sl: str, mark: float | None = None) -> None:
    """Set / repair TP+SL for a Bybit position.

    Two layers of protection:
    1. Native takeProfit + stopLoss on the position via set_trading_stop.
       This is always visible on the Bybit UI and fires as a market order
       (taker fee) — acts as a guaranteed safety net.
    2. GTC limit TP order in the open-orders list — fills as a maker order
       for lower fees.  Placed once and tracked in state.
       The native TP fires only if the limit order didn't execute first.

    mark — current mark price.  When provided, the native TP call is skipped
    if mark has already crossed the TP level (Bybit would reject it).  The GTC
    limit order still handles execution in that case.  Pass None to skip the
    check (e.g. repair loop where position is known healthy).
    """
    tracked  = state.get("open_positions", {}).get(symbol, {})
    pos_side = tracked.get("side", "Buy")

    # ── Layer 1: native TP + SL on the position ──────────────────────────────
    # Bybit requires: Long TP > mark price, Short TP < mark price.
    # If mark has already crossed TP (fast market), skip to avoid API rejection;
    # the GTC limit order placed in Layer 2 will still close the position.
    _native_tp_ok = True
    if mark is not None:
        tp_f = float(tp)
        if (pos_side == "Buy" and tp_f <= mark) or \
           (pos_side == "Sell" and tp_f >= mark):
            LOGGER.info("Skip native TP %s: TP %.4f already crossed mark %.4f for %s",
                        symbol, tp_f, mark, pos_side)
            _native_tp_ok = False
    if _native_tp_ok:
        try:
            session.set_trading_stop(
                category="linear", symbol=symbol,
                takeProfit=tp, stopLoss=sl,
                tpTriggerBy="MarkPrice", slTriggerBy="MarkPrice",
                positionIdx=0,
            )
            if symbol in state.get("open_positions", {}):
                state["open_positions"][symbol]["tp_price"] = tp
                state["open_positions"][symbol]["sl_price"] = sl
            LOGGER.info("Native TP/SL set %s  tp=%s  sl=%s", symbol, tp, sl)
        except Exception as err:
            if "34040" not in str(err):
                LOGGER.warning("set_trading_stop failed for %s: %s", symbol, err)
    # ── Layer 2: GTC limit TP order (maker fee) ───────────────────────────────
    if tracked.get("tp_order_id"):
        return  # already placed
    qty        = tracked.get("qty", "0")
    close_side = "Sell" if pos_side == "Buy" else "Buy"
    try:
        tp_resp = session.place_order(
            category="linear", symbol=symbol,
            side=close_side, orderType="Limit",
            price=tp, qty=qty,
            timeInForce="GTC",
            reduceOnly=True,
        )
        state["open_positions"][symbol]["tp_order_id"] = tp_resp["result"]["orderId"]
        LOGGER.info("GTC limit TP placed %s  price=%s  id=%s",
                    symbol, tp, state["open_positions"][symbol]["tp_order_id"])
    except Exception as err:
        LOGGER.warning("GTC limit TP failed %s: %s  (native TP is still set)", symbol, err)


# ── Bithumb entry + TP limit sell ─────────────────────────────────────────────

def _open_bithumb_position(session: BithumbSession, symbol: str, side: str,
                            atr: float, dry_run: bool, now: datetime,
                            state: dict) -> dict | None:
    """Buy (spot only — long only) at market, compute TP/SL, place GTC sell at TP."""
    if side == "Sell":
        LOGGER.debug("Bithumb is long-only; skipping Short signal for %s", symbol)
        return None

    try:
        _bal        = session.get_wallet_balance(accountType="UNIFIED")
        _bal_item   = _bal["result"]["list"][0]
        equity_usdt = float(_bal_item.get("totalEquity") or 0)
        avail_usdt  = float(_bal_item.get("totalAvailableBalance") or 0)
        # Cap notional at 97% of free cash to absorb KRW/USDT rate slippage.
        # Using total equity would overshoot when rate ticks up after sizing.
        bithumb_cap = avail_usdt * 0.97
        if NOTIONAL_CAP_USDT > 0:
            bithumb_cap = min(bithumb_cap, NOTIONAL_CAP_USDT)
    except Exception as err:
        LOGGER.warning("Cannot fetch equity for %s: %s", symbol, err)
        return None

    mark = get_mark_price(session, symbol)
    if mark is None:
        return None

    inst       = get_instrument(session, symbol)
    if not inst:
        return None
    lot        = inst.get("lotSizeFilter", {})
    tick_size  = inst.get("priceFilter", {}).get("tickSize", "0.01")
    qty_step   = lot.get("qtyStep", "0.00000001")
    min_qty    = float(lot.get("minOrderQty",    "0.00000001"))
    min_not    = float(lot.get("minNotionalValue", "1"))

    # Use HTF ATR for TP/SL — if unavailable, skip rather than use tiny 1m ATR.
    htf_atr = _get_htf_atr(session, symbol)
    if not (htf_atr == htf_atr and htf_atr > 0):
        LOGGER.warning("Skipping Bithumb %s: HTF ATR unavailable — not trading without proper TP/SL sizing",
                       symbol)
        return None
    tp_sl_atr = htf_atr

    tp_price, sl_price = calc_tp_sl("Buy", mark, tp_sl_atr, tick_size)
    sl_dist = abs(mark - float(sl_price))
    if sl_dist <= 0:
        return None

    LOGGER.debug("Bithumb sizing %s: equity=%.2f avail=%.2f cap=%.2f  mark=%.4f  1m_atr=%.6f  5m_atr=%.6f",
                 symbol, equity_usdt, avail_usdt, bithumb_cap, mark, atr, tp_sl_atr)
    qty_str = calc_qty(equity_usdt, sl_dist, qty_step, min_qty, mark,
                       notional_cap=bithumb_cap)
    # Bithumb enforces a \u20a95,000 minimum order amount.
    # Reject before hitting the API if the sized notional is below that.
    try:
        _krw_rate = session._krw_usdt_rate()
        _krw_spend = float(qty_str) * mark * _krw_rate
    except Exception:
        _krw_spend = float(qty_str) * mark * 1450.0
    if _krw_spend < 5000:
        LOGGER.warning("Bithumb %s: KRW spend \u20a9%.0f below minimum \u20a95,000 — skip",
                       symbol, _krw_spend)
        return None
    if float(qty_str) * mark < min_not:
        LOGGER.warning("Bithumb %s: notional too small", symbol)
        return None

    # Snap TP to the KRW tick grid; also enforce a minimum of BITHUMB_TICK_EXIT ticks
    # above entry so small-tick coins still get a usable target.
    # SL is intentionally NOT overridden here — it remains ATR-based so that BTC
    # (tick ≈ ₩1,000) doesn't get a microscopically tight SL (7 ticks = ₩7,000 = 0.006%).
    if BITHUMB_TICK_EXIT > 0:
        try:
            rate      = session._krw_usdt_rate()
            market_id = session.to_market(symbol)
            krw_tick  = session._live_krw_tick(market_id)
            entry_krw = round(mark * rate / krw_tick) * krw_tick
            # ATR-based TP snapped to KRW grid, with a floor of BITHUMB_TICK_EXIT ticks
            atr_tp_krw  = round(float(tp_price) * rate / krw_tick) * krw_tick
            min_tp_krw  = entry_krw + BITHUMB_TICK_EXIT * krw_tick
            final_tp_krw = max(atr_tp_krw, min_tp_krw)
            tp_price  = str(final_tp_krw / rate)
            LOGGER.info("Bithumb tick-snap %s: entry≈₩%d  tp=₩%d  sl=%s USDT (ATR)  tick_floor=%d",
                        symbol, entry_krw, final_tp_krw, sl_price, BITHUMB_TICK_EXIT)
        except Exception as err:
            LOGGER.warning("tick-snap calc failed %s: %s", symbol, err)

    if dry_run:
        LOGGER.info("[DRY RUN] Bithumb limit BUY %s  limit≈%s  qty=%s  tp=%s  sl=%s",
                    symbol, mark, qty_str, tp_price, sl_price)
        # Dry run: pretend order filled immediately so we can see the position
        return {
            "symbol":      symbol,
            "side":        "Buy",
            "entry_price": mark,
            "qty":         qty_str,
            "tp_price":    tp_price,
            "sl_price":    sl_price,
            "open_time":   now.isoformat(),
            "tp_order_id": None,
        }

    # Limit buy — maker fee (bithumb_exchange.py converts USDT price → KRW and snaps to tick)
    limit_px_usdt = float(_trail_price("Buy", mark, atr, tick_size))
    resp          = session.place_order(
        category="linear", symbol=symbol,
        side="Buy", orderType="Limit",
        price=limit_px_usdt,
        qty=qty_str,
    )
    entry_order_id = resp["result"]["orderId"]
    LOGGER.info("Bithumb limit BUY queued %s  limit=%.6f USDT  qty=%s  id=%s",
                symbol, limit_px_usdt, qty_str, entry_order_id)

    # Return pending-entry dict — TP sell is placed once buy fill is confirmed (step 3b)
    return {
        "entry_order_id": entry_order_id,
        "symbol":         symbol,
        "side":           "Buy",
        "limit_price":    str(limit_px_usdt),
        "qty":            qty_str,
        "tp_price":       tp_price,
        "sl_price":       sl_price,
        "placed_at":      now.isoformat(),
        "atr":            atr,
    }


# ── Telegram alerts ───────────────────────────────────────────────────────────

def _sgn(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v:,.2f}"


def _pnl_emoji(v: float) -> str:
    return "🟢" if v >= 0 else "🔴"


def alert_open(alerter, info: dict, dry_run: bool, exchange: str = "bybit") -> None:
    if alerter is None:
        return
    label = "Long 🔺" if info["side"] == "Buy" else "Short 🔻"
    dry   = "  [DRY RUN]" if dry_run else ""
    ex    = exchange.upper()
    alerter.send(
        f"🟢 <b>Position opened{dry}</b>\n"
        f"Strategy: <b>experiment_v4 · {ex}</b>\n"
        f"Symbol: <b>{info['symbol']}</b>  {label}\n"
        f"Entry ≈ ${float(info['entry_price']):,.4f}  Qty: {info['qty']}\n"
        f"TP: ${info.get('tp_price', '?')}   SL: ${info.get('sl_price', '?')}"
    )


def alert_close(alerter, tracked: dict, exit_price: float,
                reason: str, dry_run: bool, exchange: str = "bybit") -> None:
    if alerter is None:
        return
    side    = tracked["side"]
    entry   = float(tracked["entry_price"])
    qty_f   = float(tracked["qty"])
    pnl     = (exit_price - entry) * qty_f if side == "Buy" else (entry - exit_price) * qty_f
    pnl_pct = pnl / (entry * qty_f) * 100 if entry and qty_f else 0.0
    label   = "Long 🔺" if side == "Buy" else "Short 🔻"
    dry     = "  [DRY RUN]" if dry_run else ""
    ex      = exchange.upper()
    alerter.send(
        f"{_pnl_emoji(pnl)} <b>Position closed{dry}</b>  [{reason}]\n"
        f"Strategy: <b>experiment_v4 · {ex}</b>\n"
        f"Symbol: <b>{tracked['symbol']}</b>  {label}\n"
        f"Entry: ${entry:,.4f}   Exit: ${exit_price:,.4f}\n"
        f"PnL: {_sgn(pnl)} USDT  ({_sgn(pnl_pct)}%)"
    )


def alert_update(alerter, positions: dict, session=None, exchange: str = "bybit") -> None:
    if alerter is None:
        return
    now_kst  = datetime.now().strftime("%Y-%m-%d %H:%M KST")
    is_krw   = exchange == "bithumb"
    krw_rate = 1.0
    if is_krw and session is not None:
        try:
            krw_rate = session._krw_usdt_rate()
        except Exception:
            krw_rate = 1450.0

    def _fmt_price(u: float) -> str:
        return f"₩{u * krw_rate:,.0f}" if is_krw else f"${u:,.4f}"

    def _fmt_pnl(u: float) -> str:
        return f"{_sgn(u * krw_rate)} KRW" if is_krw else f"{_sgn(u)} USDT"

    balance_line = "💰 Balance: (unavailable)\n"
    if session is not None:
        for _ in range(2):
            try:
                acct  = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]
                total = float(acct.get("totalEquity") or 0)
                avail = float(acct.get("totalAvailableBalance") or 0)
                if is_krw:
                    balance_line = (f"💰 Balance: <b>₩{total * krw_rate:,.0f}</b>  "
                                    f"Avail: ₩{avail * krw_rate:,.0f}\n")
                else:
                    upnl = float(acct.get("totalPerpUPL") or 0)
                    balance_line = (f"💰 Balance: <b>${total:,.2f}</b>  "
                                    f"Avail: ${avail:,.2f}  uPnL: {_sgn(upnl)} USDT\n")
                break
            except Exception:
                time.sleep(2)

    min_not_usdt = 10.0 / krw_rate if is_krw else 1.0
    positions = {
        sym: p for sym, p in positions.items()
        if float(p.get("markPrice") or 0) > 0
        and float(p.get("size") or 0) * float(p.get("markPrice") or 0) >= min_not_usdt
    }
    if not positions:
        alerter.send(f"📊 <b>experiment_v4 · {exchange.upper()} — 30-min update</b>\n"
                     f"🕐 {now_kst}\n{balance_line}No open positions.")
        return
    lines = [f"📊 <b>experiment_v4 · {exchange.upper()} — 30-min update</b>",
             f"🕐 {now_kst}", balance_line.strip(), ""]
    for sym, pos in positions.items():
        side  = pos["side"]
        upnl  = float(pos.get("unrealisedPnl") or 0)
        entry = float(pos.get("avgPrice") or 0)
        mark  = float(pos.get("markPrice") or 0)
        size  = float(pos.get("size") or 0)
        cost  = size * entry
        pct   = upnl / cost * 100 if cost else 0.0
        label = "Long 🔺" if side == "Buy" else "Short 🔻"
        lines.append(
            f"{_pnl_emoji(upnl)} <b>{sym}</b>  {label}\n"
            f"  Entry {_fmt_price(entry)}  Mark {_fmt_price(mark)}  Qty {size}\n"
            f"  PnL {_fmt_pnl(upnl)}  ({_sgn(pct)}%)"
        )
    alerter.send("\n".join(lines))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    load_params()

    parser = argparse.ArgumentParser(
        description="experiment_v4: 1m/5m dual-timeframe scalp-reversal (Bybit + Bithumb)")
    parser.add_argument("--once",       action="store_true")
    parser.add_argument("--update",     action="store_true")
    parser.add_argument("--cancel-all", action="store_true")
    parser.add_argument("--close-all",  action="store_true")
    parser.add_argument("--exchange",   choices=["bybit", "bithumb"], default="bybit")
    parser.add_argument("--debug",      action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for _noisy in ("urllib3", "pybit", "requests", "httpcore", "httpx"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    exchange = args.exchange
    is_spot  = exchange == "bithumb"

    global STATE_FILE
    STATE_FILE = Path(f"data/experiment_v4_{exchange}_state.json")
    load_params(exchange)

    settings = load_settings()
    if is_spot:
        if not settings.bithumb_credentials:
            raise SystemExit("Bithumb credentials not found. Check BITHUMB_CREDENTIALS_FILE in .env.")
        session = BithumbSession(
            access_key=settings.bithumb_credentials.api_key,
            secret_key=settings.bithumb_credentials.api_secret,
        )
    else:
        _sync_pybit_clock(testnet=settings.bybit_testnet)
        session = HTTP(
            testnet=settings.bybit_testnet,
            api_key=settings.bybit_credentials.api_key,
            api_secret=settings.bybit_credentials.api_secret,
            recv_window=20000,
        )

    alerter = TelegramAlerter.from_env(
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )
    _pos_token_path = Path(os.getenv("POSITION_BOT_CREDENTIALS_FILE",
                                     "design/_cred_position_bot_token"))
    position_bot_token = (
        _pos_token_path.read_text(encoding="utf-8").strip()
        if _pos_token_path.exists() else None
    )
    position_alerter = TelegramAlerter.from_env(
        token=position_bot_token,
        chat_id=os.getenv("POSITION_BOT_CHAT_ID") or None,
    )

    if args.update:
        alert_update(position_alerter, get_open_positions(session), session, exchange)
        return

    if args.cancel_all:
        session.cancel_all_orders(category="linear", settleCoin="USDT")
        state = load_state()
        state["pending_orders"] = {}
        save_state(state)
        LOGGER.info("All orders cancelled.")
        return

    if args.close_all:
        positions = get_open_positions(session)
        session.cancel_all_orders(category="linear", settleCoin="USDT")
        for sym, pos in positions.items():
            close_side = "Sell" if pos["side"] == "Buy" else "Buy"
            try:
                session.place_order(
                    category="linear", symbol=sym, side=close_side,
                    orderType="Market", qty=pos["size"],
                    reduceOnly=True, timeInForce="IOC",
                )
                LOGGER.info("Closed %s %s qty=%s", close_side, sym, pos["size"])
            except Exception as err:
                LOGGER.error("Close failed %s: %s", sym, err)
        state = load_state()
        state["open_positions"] = {}
        state["pending_orders"] = {}
        save_state(state)
        return

    symbols = load_symbols(exchange)
    LOGGER.info("experiment_v4 started | exchange=%s | %d symbols | dry_run=%s",
                exchange, len(symbols), settings.dry_run)

    state          = load_state()
    last_update_at = datetime.now(tz=UTC) - timedelta(seconds=UPDATE_INTERVAL)
    _fetch_fail_streak   = 0
    _FETCH_ALERT_THRESHOLD = 3

    while True:
        now = datetime.now(tz=UTC)
        load_params(exchange)

        # ── 1. Fetch live positions ───────────────────────────────────────────
        try:
            live_positions = get_open_positions(session)
            _fetch_fail_streak = 0
        except Exception as err:
            _fetch_fail_streak += 1
            LOGGER.warning("Position fetch failed (streak=%d): %s", _fetch_fail_streak, err)
            if _fetch_fail_streak == _FETCH_ALERT_THRESHOLD:
                position_alerter.send(
                    f"⚠️ <b>experiment_v4 · {exchange}</b>: position fetch failed "
                    f"{_fetch_fail_streak}x\n<code>{str(err)[:200]}</code>"
                )
            if args.once:
                break
            time.sleep(CHECK_INTERVAL)
            continue

        # ── 2. Bybit: process pending (trail-queue) orders ────────────────────
        if not is_spot:
            state.setdefault("pending_orders", {})
            for sym in list(state["pending_orders"].keys()):
                pending   = state["pending_orders"][sym]
                signal_at = datetime.fromisoformat(pending["signal_at"]).replace(tzinfo=UTC)
                age_min   = (now - signal_at).total_seconds() / 60

                # a. Expired
                if age_min >= MAX_ORDER_AGE_MIN:
                    if pending.get("order_id") and pending["order_id"] != "dry":
                        try:
                            session.cancel_order(category="linear", symbol=sym,
                                                 orderId=pending["order_id"])
                        except Exception:
                            pass
                    LOGGER.info("Signal expired for %s after %.1f min", sym, age_min)
                    del state["pending_orders"][sym]
                    continue

                # b. Filled
                if sym in live_positions:
                    pos   = live_positions[sym]
                    entry = float(pos.get("avgPrice") or pending["limit_price"])
                    tp, sl = calc_tp_sl(pending["side"], entry,
                                        float(pending.get("atr", 0)), "0.01")
                    tp_price = pending.get("tp_price", tp)
                    sl_price = pending.get("sl_price", sl)
                    qty      = pending.get("qty", pos.get("size", "0"))
                    state["open_positions"][sym] = {
                        "symbol":      sym,
                        "side":        pending["side"],
                        "entry_price": entry,
                        "qty":         qty,
                        "tp_price":    tp_price,
                        "sl_price":    sl_price,
                        "open_time":   now.isoformat(),
                        "tp_order_id": None,
                    }
                    # Set native TP+SL on position AND place GTC limit TP (maker fee)
                    if not settings.dry_run:
                        _mark = float(live_positions[sym].get("markPrice") or 0) or None
                        _apply_tp_sl(session, sym, state, tp_price, sl_price, mark=_mark)
                    _record_signal(state, sym, now)
                    alert_open(alerter, state["open_positions"][sym],
                               settings.dry_run, exchange)
                    del state["pending_orders"][sym]
                    continue

                # c. Stale — nullify for re-queue in step 4
                if pending.get("order_id") and pending["order_id"] != "dry":
                    mark = get_mark_price(session, sym)
                    if mark and _is_stale(pending, mark):
                        try:
                            session.cancel_order(category="linear", symbol=sym,
                                                 orderId=pending["order_id"])
                            LOGGER.info("Stale cancel %s (limit=%s mark=%.4f)",
                                        sym, pending["limit_price"], mark)
                        except Exception as err:
                            if "110001" in str(err):
                                LOGGER.info("Already filled %s (late cancel)", sym)
                            else:
                                LOGGER.warning("Cancel error %s: %s", sym, err)
                        state["pending_orders"][sym]["order_id"] = None

        # ── 3. Detect position closes (Bybit TP/SL hit) ──────────────────────
        if not is_spot:
            for sym, tracked in list(state["open_positions"].items()):
                if sym in live_positions or sym in state.get("pending_orders", {}):
                    continue
                # Cancel GTC TP limit order — no-op if TP already filled as maker
                tp_oid = tracked.get("tp_order_id")
                if tp_oid and not settings.dry_run:
                    try:
                        session.cancel_order(category="linear", symbol=sym, orderId=tp_oid)
                    except Exception:
                        pass  # already filled (TP hit) or expired — OK
                exit_price = get_mark_price(session, sym) or float(tracked.get("entry_price", 0))
                # Track win/loss for consecutive-loss halt
                _side_d  = tracked.get("side", "Buy")
                _entry_d = float(tracked.get("entry_price", 0))
                _won     = (exit_price > _entry_d) if _side_d == "Buy" else (exit_price < _entry_d)
                if _won:
                    state["loss_streak"] = 0
                else:
                    state["loss_streak"] = state.get("loss_streak", 0) + 1
                    if state["loss_streak"] >= MAX_LOSS_STREAK and not settings.dry_run:
                        _pu = now + timedelta(minutes=LOSS_STREAK_PAUSE_MIN)
                        state["loss_streak_pause_until"] = _pu.isoformat()
                        LOGGER.warning("Loss streak %d — pausing new entries for %d min",
                                       state["loss_streak"], LOSS_STREAK_PAUSE_MIN)
                        try:
                            alerter.send(
                                f"🚫 <b>experiment_v4 · {exchange}</b>\n"
                                f"<b>{state['loss_streak']} consecutive SL hits</b>\n"
                                f"Pausing new entries for {LOSS_STREAK_PAUSE_MIN} min "
                                f"(until {_pu.strftime('%H:%M UTC')})"
                            )
                        except Exception:
                            pass
                LOGGER.info("Position closed: %s  exit≈%.4f  (%s)",
                            sym, exit_price, "WIN" if _won else "LOSS")
                alert_close(alerter, tracked, exit_price, "TP/SL hit",
                            settings.dry_run, exchange)
                del state["open_positions"][sym]

            # Repair missing native TP/SL or GTC limit TP after restart
            for sym, pos in live_positions.items():
                tracked     = state["open_positions"].get(sym, {})
                tp          = tracked.get("tp_price", "")
                sl          = tracked.get("sl_price", "")
                if not (tp and sl):
                    continue
                has_sl       = bool(pos.get("stopLoss")   and float(pos["stopLoss"])   != 0)
                has_tp       = bool(pos.get("takeProfit") and float(pos["takeProfit"]) != 0)
                has_tp_order = bool(tracked.get("tp_order_id"))
                if has_sl and has_tp and has_tp_order:
                    continue   # all OK
                LOGGER.info("Repairing %s for %s  (has_sl=%s has_tp=%s has_tp_order=%s)",
                            sym, sym, has_sl, has_tp, has_tp_order)
                _mark = float(pos.get("markPrice") or 0) or None
                _apply_tp_sl(session, sym, state, tp, sl, mark=_mark)

            # Profit-lock: for each open Bybit position check if we should move SL
            for sym, tracked in list(state["open_positions"].items()):
                if sym not in live_positions:
                    continue
                pos     = live_positions[sym]
                current = float(pos.get("markPrice") or 0)
                if current > 0:
                    _check_profit_lock(session, sym, tracked, current,
                                       is_spot=False, dry_run=settings.dry_run,
                                       alerter=alerter)

        # ── 3b. Bithumb: per-tick TP fill check + SL enforcement ─────────────
        if is_spot:
            # Fill check for pending limit buy entries
            state.setdefault("bithumb_pending_entries", {})
            for sym in list(state["bithumb_pending_entries"].keys()):
                pending = state["bithumb_pending_entries"][sym]
                placed  = datetime.fromisoformat(pending["placed_at"]).replace(tzinfo=UTC)
                age_min = (now - placed).total_seconds() / 60

                # Expire unfilled entries after MAX_ORDER_AGE_MIN
                if age_min >= MAX_ORDER_AGE_MIN:
                    oid = pending.get("entry_order_id")
                    if oid:
                        try:
                            session.cancel_order(category="linear", symbol=sym, orderId=oid)
                        except Exception:
                            pass
                    LOGGER.info("Bithumb limit entry expired %s after %.1f min", sym, age_min)
                    del state["bithumb_pending_entries"][sym]
                    continue

                # Poll fill status
                oid = pending.get("entry_order_id")
                if not oid:
                    continue
                try:
                    order = session.get_order(orderId=oid)
                except Exception as err:
                    LOGGER.warning("get_order failed %s %s: %s", sym, oid, err)
                    continue
                if order.get("state") != "done":
                    continue  # still pending

                # Entry filled — place TP sell and open position
                qty_str  = pending["qty"]
                tp_price = pending["tp_price"]
                sl_price = pending["sl_price"]
                entry_px = float(pending.get("limit_price") or pending.get("entry_price") or 0)
                tp_order_id: str | None = None
                try:
                    sell_resp = session.place_order(
                        category="linear", symbol=sym,
                        side="Sell", orderType="Limit",
                        price=tp_price, qty=qty_str, timeInForce="GTC",
                    )
                    tp_order_id = sell_resp["result"]["orderId"]
                    LOGGER.info("Bithumb TP sell placed %s  price=%s  id=%s",
                                sym, tp_price, tp_order_id)
                except Exception as err:
                    LOGGER.error("Bithumb TP sell failed %s: %s", sym, err)

                pos_info = {
                    "symbol":      sym,
                    "side":        "Buy",
                    "entry_price": entry_px,
                    "qty":         qty_str,
                    "tp_price":    tp_price,
                    "sl_price":    sl_price,
                    "open_time":   now.isoformat(),
                    "tp_order_id": tp_order_id,
                }
                state["open_positions"][sym] = pos_info
                del state["bithumb_pending_entries"][sym]
                LOGGER.info("Bithumb entry filled %s  entry=%.6f", sym, entry_px)
                alert_open(alerter, pos_info, settings.dry_run, exchange)

            configured_syms = {s for s, _ in symbols}
            for sym, tracked in list(state["open_positions"].items()):
                if sym not in live_positions or sym not in configured_syms:
                    continue
                current = get_mark_price(session, sym)
                if current is None:
                    continue

                # SL limit order already queued — poll for fill before anything else
                _sl_oid = tracked.get("sl_order_id")
                if _sl_oid:
                    if not settings.dry_run:
                        try:
                            _sl_ord = session.get_order(orderId=_sl_oid)
                            _sl_st  = _sl_ord.get("state")
                            if _sl_st == "done":
                                _sl_exec = float(
                                    _sl_ord.get("price") or tracked.get("sl_price", current))
                                state["loss_streak"] = state.get("loss_streak", 0) + 1
                                if state["loss_streak"] >= MAX_LOSS_STREAK:
                                    _pu = now + timedelta(minutes=LOSS_STREAK_PAUSE_MIN)
                                    state["loss_streak_pause_until"] = _pu.isoformat()
                                    LOGGER.warning("Loss streak %d — pausing %d min",
                                                   state["loss_streak"], LOSS_STREAK_PAUSE_MIN)
                                    try:
                                        alerter.send(
                                            f"🚫 <b>experiment_v4 · {exchange}</b>\n"
                                            f"<b>{state['loss_streak']} consecutive SL hits</b>\n"
                                            f"Pausing new entries for {LOSS_STREAK_PAUSE_MIN} min "
                                            f"(until {_pu.strftime('%H:%M UTC')})"
                                        )
                                    except Exception:
                                        pass
                                LOGGER.info("Bithumb SL limit filled %s  exec=%.6f",
                                            sym, _sl_exec)
                                alert_close(alerter, tracked, _sl_exec, "SL hit",
                                            settings.dry_run, exchange)
                                del state["open_positions"][sym]
                                state["pending_orders"].pop(sym, None)
                                tracked.pop("sl_order_placed_at", None)
                            elif _sl_st in ("cancel", "cancelled"):
                                tracked.pop("sl_order_id", None)
                                tracked.pop("sl_order_placed_at", None)
                                LOGGER.warning("Bithumb SL limit cancelled %s — will re-trigger",
                                               sym)
                            else:
                                # Still waiting: promote to market close after timeout.
                                _placed_iso = tracked.get("sl_order_placed_at")
                                _elapsed = None
                                if _placed_iso:
                                    try:
                                        _placed_dt = datetime.fromisoformat(_placed_iso).replace(tzinfo=UTC)
                                        _elapsed = (now - _placed_dt).total_seconds()
                                    except Exception:
                                        _elapsed = None
                                if BITHUMB_SL_FAILOVER_SEC > 0 and _elapsed is not None \
                                   and _elapsed >= BITHUMB_SL_FAILOVER_SEC:
                                    LOGGER.warning(
                                        "Bithumb SL limit timeout %s: %.1fs >= %ds — market failover",
                                        sym, _elapsed, BITHUMB_SL_FAILOVER_SEC,
                                    )
                                    try:
                                        session.cancel_order(category="linear", symbol=sym, orderId=_sl_oid)
                                    except Exception:
                                        pass
                                    qty = live_positions[sym].get("size") or tracked.get("qty")
                                    try:
                                        session.place_order(
                                            category="linear", symbol=sym,
                                            side="Sell", orderType="Market",
                                            qty=qty, timeInForce="IOC",
                                        )
                                        state["loss_streak"] = state.get("loss_streak", 0) + 1
                                        if state["loss_streak"] >= MAX_LOSS_STREAK:
                                            _pu = now + timedelta(minutes=LOSS_STREAK_PAUSE_MIN)
                                            state["loss_streak_pause_until"] = _pu.isoformat()
                                            LOGGER.warning("Loss streak %d — pausing %d min",
                                                           state["loss_streak"], LOSS_STREAK_PAUSE_MIN)
                                            try:
                                                alerter.send(
                                                    f"🚫 <b>experiment_v4 · {exchange}</b>\n"
                                                    f"<b>{state['loss_streak']} consecutive SL hits</b>\n"
                                                    f"Pausing new entries for {LOSS_STREAK_PAUSE_MIN} min "
                                                    f"(until {_pu.strftime('%H:%M UTC')})"
                                                )
                                            except Exception:
                                                pass
                                        LOGGER.info("Bithumb SL hit %s (timeout market failover): %.6f",
                                                    sym, current)
                                        alert_close(alerter, tracked, current, "SL hit",
                                                    settings.dry_run, exchange)
                                        del state["open_positions"][sym]
                                        state["pending_orders"].pop(sym, None)
                                    except Exception as _sl_mkt_err:
                                        LOGGER.error("Bithumb timeout market SL failed %s: %s",
                                                     sym, _sl_mkt_err)
                                    tracked.pop("sl_order_id", None)
                                    tracked.pop("sl_order_placed_at", None)
                        except Exception as _sl_err:
                            LOGGER.warning("Bithumb SL order poll %s: %s", sym, _sl_err)
                    continue  # skip profit-lock / TP / SL checks while SL order is active

                # Profit-lock: move SL once profit ≥ trigger threshold
                _check_profit_lock(session, sym, tracked, current,
                                   is_spot=True, dry_run=settings.dry_run,
                                   alerter=alerter)

                # Check TP order fill
                tp_oid = tracked.get("tp_order_id")
                if tp_oid:
                    try:
                        order = session.get_order(orderId=tp_oid)
                        if order.get("state") == "done":
                            exec_price = float(
                                order.get("price") or tracked.get("tp_price", current))
                            state["loss_streak"] = 0
                            LOGGER.info("Bithumb TP filled %s: price=%s", sym, exec_price)
                            alert_close(alerter, tracked, exec_price, "TP hit",
                                        settings.dry_run, exchange)
                            del state["open_positions"][sym]
                            state["pending_orders"].pop(sym, None)
                            continue
                    except Exception as err:
                        LOGGER.warning("get_order failed %s %s: %s", sym, tp_oid, err)

                # SL check — limit sell for lower maker fee; tracked until fill
                sl = float(tracked.get("sl_price") or 0)
                if sl and 0 < current <= sl:
                    if tp_oid and not settings.dry_run:
                        try:
                            session.cancel_order(category="linear", symbol=sym,
                                                 orderId=tp_oid)
                        except Exception:
                            pass
                    qty = live_positions[sym].get("size") or tracked.get("qty")
                    if not settings.dry_run:
                        try:
                            sl_resp = session.place_order(
                                category="linear", symbol=sym,
                                side="Sell", orderType="Limit",
                                price=str(current), qty=qty,
                            )
                            tracked["sl_order_id"] = sl_resp["result"]["orderId"]
                            tracked["sl_order_placed_at"] = now.isoformat()
                            LOGGER.info(
                                "Bithumb SL triggered %s at %.6f — limit sell queued id=%s",
                                sym, current, tracked["sl_order_id"])
                            # Hard failover: wait briefly for maker fill, then force market exit.
                            if BITHUMB_SL_FAILOVER_SEC > 0:
                                _deadline = time.time() + BITHUMB_SL_FAILOVER_SEC
                                _filled = False
                                while time.time() < _deadline:
                                    time.sleep(min(2.0, max(0.5, BITHUMB_SL_FAILOVER_SEC / 4)))
                                    try:
                                        _sl_now = session.get_order(orderId=tracked["sl_order_id"])
                                    except Exception:
                                        continue
                                    _st = _sl_now.get("state")
                                    if _st == "done":
                                        _sl_exec = float(
                                            _sl_now.get("price") or tracked.get("sl_price", current))
                                        state["loss_streak"] = state.get("loss_streak", 0) + 1
                                        if state["loss_streak"] >= MAX_LOSS_STREAK:
                                            _pu = now + timedelta(minutes=LOSS_STREAK_PAUSE_MIN)
                                            state["loss_streak_pause_until"] = _pu.isoformat()
                                            LOGGER.warning("Loss streak %d — pausing %d min",
                                                           state["loss_streak"], LOSS_STREAK_PAUSE_MIN)
                                            try:
                                                alerter.send(
                                                    f"🚫 <b>experiment_v4 · {exchange}</b>\n"
                                                    f"<b>{state['loss_streak']} consecutive SL hits</b>\n"
                                                    f"Pausing new entries for {LOSS_STREAK_PAUSE_MIN} min "
                                                    f"(until {_pu.strftime('%H:%M UTC')})"
                                                )
                                            except Exception:
                                                pass
                                        LOGGER.info("Bithumb SL limit filled %s  exec=%.6f",
                                                    sym, _sl_exec)
                                        alert_close(alerter, tracked, _sl_exec, "SL hit",
                                                    settings.dry_run, exchange)
                                        del state["open_positions"][sym]
                                        state["pending_orders"].pop(sym, None)
                                        tracked.pop("sl_order_id", None)
                                        tracked.pop("sl_order_placed_at", None)
                                        _filled = True
                                        break
                                    if _st in ("cancel", "cancelled"):
                                        tracked.pop("sl_order_id", None)
                                        tracked.pop("sl_order_placed_at", None)
                                        break
                                if _filled:
                                    continue
                                if tracked.get("sl_order_id"):
                                    LOGGER.warning(
                                        "Bithumb SL limit not filled within %ds for %s — market failover",
                                        BITHUMB_SL_FAILOVER_SEC, sym,
                                    )
                                    try:
                                        session.cancel_order(
                                            category="linear", symbol=sym,
                                            orderId=tracked["sl_order_id"],
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        session.place_order(
                                            category="linear", symbol=sym,
                                            side="Sell", orderType="Market",
                                            qty=qty, timeInForce="IOC",
                                        )
                                        state["loss_streak"] = state.get("loss_streak", 0) + 1
                                        if state["loss_streak"] >= MAX_LOSS_STREAK:
                                            _pu = now + timedelta(minutes=LOSS_STREAK_PAUSE_MIN)
                                            state["loss_streak_pause_until"] = _pu.isoformat()
                                            LOGGER.warning("Loss streak %d — pausing %d min",
                                                           state["loss_streak"], LOSS_STREAK_PAUSE_MIN)
                                            try:
                                                alerter.send(
                                                    f"🚫 <b>experiment_v4 · {exchange}</b>\n"
                                                    f"<b>{state['loss_streak']} consecutive SL hits</b>\n"
                                                    f"Pausing new entries for {LOSS_STREAK_PAUSE_MIN} min "
                                                    f"(until {_pu.strftime('%H:%M UTC')})"
                                                )
                                            except Exception:
                                                pass
                                        LOGGER.info("Bithumb SL hit %s (market failover): %.6f",
                                                    sym, current)
                                        alert_close(alerter, tracked, current, "SL hit",
                                                    settings.dry_run, exchange)
                                        del state["open_positions"][sym]
                                        state["pending_orders"].pop(sym, None)
                                    except Exception as _mkt_err:
                                        LOGGER.error("Bithumb market failover failed %s: %s",
                                                     sym, _mkt_err)
                                    tracked.pop("sl_order_id", None)
                                    tracked.pop("sl_order_placed_at", None)
                        except Exception as _err:
                            LOGGER.error("Bithumb SL limit failed %s: %s — market fallback",
                                         sym, _err)
                            try:
                                session.place_order(
                                    category="linear", symbol=sym,
                                    side="Sell", orderType="Market",
                                    qty=qty, timeInForce="IOC",
                                )
                                state["loss_streak"] = state.get("loss_streak", 0) + 1
                                if state["loss_streak"] >= MAX_LOSS_STREAK:
                                    _pu = now + timedelta(minutes=LOSS_STREAK_PAUSE_MIN)
                                    state["loss_streak_pause_until"] = _pu.isoformat()
                                    LOGGER.warning("Loss streak %d — pausing %d min",
                                                   state["loss_streak"], LOSS_STREAK_PAUSE_MIN)
                                    try:
                                        alerter.send(
                                            f"🚫 <b>experiment_v4 · {exchange}</b>\n"
                                            f"<b>{state['loss_streak']} consecutive SL hits</b>\n"
                                            f"Pausing new entries for {LOSS_STREAK_PAUSE_MIN} min "
                                            f"(until {_pu.strftime('%H:%M UTC')})"
                                        )
                                    except Exception:
                                        pass
                                LOGGER.info("Bithumb SL hit %s (market fallback): %.6f",
                                            sym, current)
                                alert_close(alerter, tracked, current, "SL hit",
                                            settings.dry_run, exchange)
                                del state["open_positions"][sym]
                                state["pending_orders"].pop(sym, None)
                            except Exception as _err2:
                                LOGGER.error("Bithumb market SL fallback failed %s: %s",
                                             sym, _err2)
                    else:
                        # Dry run: treat as immediate fill
                        state["loss_streak"] = state.get("loss_streak", 0) + 1
                        if state["loss_streak"] >= MAX_LOSS_STREAK:
                            LOGGER.warning("Loss streak %d — pausing %d min",
                                           state["loss_streak"], LOSS_STREAK_PAUSE_MIN)
                        LOGGER.info("[DRY RUN] Bithumb SL hit %s: limit sell at %.6f",
                                    sym, current)
                        alert_close(alerter, tracked, current, "SL hit",
                                    settings.dry_run, exchange)
                        del state["open_positions"][sym]
                        state["pending_orders"].pop(sym, None)

            # Detect Bithumb position closes (balance gone)
            for sym, tracked in list(state["open_positions"].items()):
                if sym in live_positions:
                    continue
                if sym in state.get("pending_orders", {}):
                    continue
                # If an SL limit order was queued, use its fill price and reason
                _reason     = "closed"
                _exit_price = None
                _sl_oid     = tracked.get("sl_order_id")
                if _sl_oid and not settings.dry_run:
                    try:
                        _sl_ord = session.get_order(orderId=_sl_oid)
                        if _sl_ord.get("state") == "done":
                            _exit_price = float(_sl_ord.get("price") or 0) or None
                            _reason     = "SL hit"
                            state["loss_streak"] = state.get("loss_streak", 0) + 1
                            if state["loss_streak"] >= MAX_LOSS_STREAK:
                                _pu = now + timedelta(minutes=LOSS_STREAK_PAUSE_MIN)
                                state["loss_streak_pause_until"] = _pu.isoformat()
                                LOGGER.warning("Loss streak %d — pausing %d min",
                                               state["loss_streak"], LOSS_STREAK_PAUSE_MIN)
                                try:
                                    alerter.send(
                                        f"🚫 <b>experiment_v4 · {exchange}</b>\n"
                                        f"<b>{state['loss_streak']} consecutive SL hits</b>\n"
                                        f"Pausing new entries for {LOSS_STREAK_PAUSE_MIN} min "
                                        f"(until {_pu.strftime('%H:%M UTC')})"
                                    )
                                except Exception:
                                    pass
                    except Exception as _e:
                        LOGGER.warning("SL order fetch on detect-close %s: %s", sym, _e)
                if _exit_price is None:
                    _exit_price = (get_mark_price(session, sym)
                                   or float(tracked.get("entry_price", 0)))
                LOGGER.info("Bithumb position gone: %s  exit≈%.4f  (%s)",
                            sym, _exit_price, _reason)
                alert_close(alerter, tracked, _exit_price, _reason,
                            settings.dry_run, exchange)
                del state["open_positions"][sym]

        # ── 3c. Daily drawdown circuit breaker (Bybit only) ──────────────────
        if not is_spot and MAX_DAILY_DRAWDOWN > 0:
            _today = now.strftime("%Y-%m-%d")
            if state.get("daily_date") != _today:
                # New trading day — snapshot opening equity
                try:
                    state["daily_start_equity"] = get_wallet_equity(session)
                    state["daily_date"]          = _today
                    LOGGER.info("New trading day — equity snapshot: %.4f USDT",
                                state["daily_start_equity"])
                except Exception:
                    pass
            _start_eq = float(state.get("daily_start_equity") or 0)
            if _start_eq > 0:
                try:
                    _curr_eq = get_wallet_equity(session)
                    _dd_pct  = (_start_eq - _curr_eq) / _start_eq
                    if _dd_pct >= MAX_DAILY_DRAWDOWN:
                        LOGGER.warning(
                            "Daily drawdown %.1f%% ≥ limit %.0f%% — halting new entries "
                            "(start=%.4f  now=%.4f)",
                            _dd_pct * 100, MAX_DAILY_DRAWDOWN * 100,
                            _start_eq, _curr_eq,
                        )
                        save_state(state)
                        if args.once:
                            break
                        time.sleep(CHECK_INTERVAL)
                        continue
                except Exception:
                    pass

        # ── 3d. Consecutive-loss pause ────────────────────────────────────────
        _pause_until_str = state.get("loss_streak_pause_until")
        if _pause_until_str:
            _pu = datetime.fromisoformat(_pause_until_str).replace(tzinfo=UTC)
            if now < _pu:
                remaining_min = int((_pu - now).total_seconds() / 60) + 1
                LOGGER.info("Loss-streak pause — %d min remaining — skipping scan cycle",
                            remaining_min)
                save_state(state)
                if args.once:
                    break
                time.sleep(CHECK_INTERVAL)
                continue
            else:
                state["loss_streak_pause_until"] = None
                state["loss_streak"] = 0
                LOGGER.info("Loss-streak pause expired — resuming trading")

        # ── 4. Signal scan + stale re-queue ──────────────────────────────────
        expected_ts = str((int(now.timestamp() // _CANDLE_INTERVAL_SECS) - 1)
                           * _CANDLE_INTERVAL_SECS * 1000)
        n_open    = len(live_positions)
        n_pending = len(state.get("pending_orders", {}))
        LOGGER.info("── scan cycle  open=%d  pending=%d  candle_ts=%s",
                    n_open, n_pending, expected_ts)

        # Panic check: called once per cycle using BTCUSDT as market proxy
        _panic_mode = (not is_spot) and _is_panic_market(session)
        if _panic_mode:
            LOGGER.info("── PANIC MODE: BTC down >%.0f%% in last 4×%sh — Long entries blocked",
                        PANIC_DROP_PCT * 100, MACRO_INTERVAL)

        scanned = skipped_open = skipped_pending = skipped_freq = skipped_candle = signals = 0
        blocked_no_signal = blocked_htf = blocked_macro = blocked_panic = 0

        for symbol, lev_override in symbols:
            has_open    = symbol in state["open_positions"] or symbol in live_positions
            has_pending = (symbol in state.get("pending_orders", {})) or \
                          (is_spot and symbol in state.get("bithumb_pending_entries", {}))

            if has_open:
                LOGGER.debug("  %-20s  skip: position open", symbol)
                skipped_open += 1
                continue

            # Bybit: re-queue stale (order_id nulled in step 2c)
            if not is_spot and has_pending:
                pending = state["pending_orders"][symbol]
                if pending.get("order_id") is not None:
                    LOGGER.debug("  %-20s  pending order active  id=%s",
                                 symbol, pending["order_id"])
                    skipped_pending += 1
                    continue
                atr      = float(pending.get("atr", 0))
                sig_side = pending["side"]
                sig_at   = pending["signal_at"]
                LOGGER.debug("  %-20s  re-queuing stale %s order", symbol, sig_side)
                if atr > 0:
                    new_entry = _place_entry_bybit(
                        session, symbol, sig_side, atr,
                        settings.dry_run, lev_override, now,
                        signal_at_iso=sig_at,
                    )
                    if new_entry:
                        state["pending_orders"][symbol] = new_entry
                    else:
                        del state["pending_orders"][symbol]
                else:
                    del state["pending_orders"][symbol]
                continue

            if has_pending:
                skipped_pending += 1
                LOGGER.debug("  %-20s  skip: bithumb pending order", symbol)
                continue

            if not _freq_ok(state, symbol, now):
                skipped_freq += 1
                LOGGER.debug("  %-20s  skip: frequency guard", symbol)
                continue

            # Skip if we already evaluated this candle for this symbol
            if expected_ts == state["processed_candles"].get(symbol):
                skipped_candle += 1
                LOGGER.debug("  %-20s  skip: candle already scanned", symbol)
                continue

            LOGGER.debug("  %-20s  scanning kline …", symbol)
            scanned += 1

            # ── Adaptive dual-timeframe signal detection ──────────────────────
            # Always check both 1-min and 5-min candles.
            # Regime is determined by 1m_ATR / 5m_ATR:
            #   ≥ VOL_REGIME_RATIO → volatile market  → 1-min signals are real
            #   <  VOL_REGIME_RATIO → quiet market     → use 5-min only
            # Priority: both-agree > 5m-only > 1m-only-in-volatile
            try:
                side_1m, candle_ts, atr_1m = check_signal(session, symbol, "1")
            except Exception as err:
                LOGGER.warning("  %-20s  1m kline failed: %s", symbol, err)
                time.sleep(1.0)
                continue
            time.sleep(0.5)

            try:
                side_5m, candle_ts_5m, atr_5m = check_signal(session, symbol, "5")
            except Exception as err:
                LOGGER.warning("  %-20s  5m kline failed: %s", symbol, err)
                side_5m, atr_5m = None, float("nan")
            time.sleep(0.5)

            if candle_ts and candle_ts == state["processed_candles"].get(symbol):
                LOGGER.debug("  %-20s  skip: candle seen mid-scan", symbol)
                continue
            if candle_ts:
                state["processed_candles"][symbol] = candle_ts

            # Determine volatility regime
            _valid_1m = (atr_1m == atr_1m and atr_1m > 0)
            _valid_5m = (atr_5m == atr_5m and atr_5m > 0)
            vol_ratio = (atr_1m / atr_5m) if (_valid_1m and _valid_5m) else 0.0
            is_volatile = vol_ratio >= VOL_REGIME_RATIO

            if side_1m is not None and side_5m == side_1m:
                # Both timeframes agree — strongest signal
                side, atr, regime = side_1m, atr_1m, "both"
            elif side_5m is not None:
                # 5-min signal only — reliable in any regime
                side, atr, regime = side_5m, atr_5m if not _valid_1m else atr_1m, "5m-only"
            elif side_1m is not None and is_volatile:
                # 1-min signal in a genuinely volatile market
                side, atr, regime = side_1m, atr_1m, "1m-volatile"
            else:
                side = None
                atr  = atr_1m if _valid_1m else atr_5m
                regime = "none"

            if side is None:
                blocked_no_signal += 1
                LOGGER.debug("  %-20s  no signal  regime=%s  vol_ratio=%.2f  1m_atr=%.6f  5m_atr=%.6f",
                             symbol, regime, vol_ratio,
                             atr_1m if _valid_1m else 0, atr_5m if _valid_5m else 0)
                continue

            LOGGER.debug("  %-20s  regime=%s  vol_ratio=%.2f  1m=%s  5m=%s",
                         symbol, regime, vol_ratio, side_1m, side_5m)

            # Only call HTF now that we have a 1-min signal — avoids a kline
            # call for every symbol on every tick (vast majority have no signal).
            # HTF gate applied to BOTH Bybit and Bithumb.
            # Bithumb is long-only (spot) so Short signals are already blocked in
            # _open_bithumb_position, but the HTF gate still prevents buying dips
            # in confirmed downtrends.
            htf_side = check_htf_trend(session, symbol)
            if htf_side != side:
                blocked_htf += 1
                # None != side → also skips when trend is ambiguous
                LOGGER.info("  %-20s  HTF gate: 5m=%s vs 1m=%s — skip",
                            symbol, htf_side, side)
                continue

            # 1h macro trend gate — lenient: None = no strong opinion, allow trade
            macro_side = check_macro_trend(session, symbol)
            if macro_side is not None and macro_side != side:
                blocked_macro += 1
                LOGGER.info("  %-20s  Macro(1h) gate: %s vs sig=%s — skip",
                            symbol, macro_side, side)
                continue

            # Panic filter: block Long entries when BTC is in confirmed freefall
            if _panic_mode and side == "Buy":
                blocked_panic += 1
                LOGGER.info("  %-20s  Panic filter: skip Long in panic market", symbol)
                continue

            signals += 1
            LOGGER.info("Signal → %s %s  regime=%s  atr=%.6f", side, symbol, regime, atr)

            if is_spot:
                pos_info = _open_bithumb_position(
                    session, symbol, side, atr,
                    settings.dry_run, now, state,
                )
                if pos_info:
                    if "entry_order_id" in pos_info:
                        # Real limit buy queued — track as pending entry
                        state.setdefault("bithumb_pending_entries", {})[symbol] = pos_info
                        _record_signal(state, symbol, now)
                        LOGGER.info("Bithumb limit buy queued %s  limit=%s",
                                    symbol, pos_info.get("limit_price"))
                    else:
                        # Dry-run: treat as immediate open
                        state["open_positions"][symbol] = pos_info
                        _record_signal(state, symbol, now)
                        alert_open(alerter, pos_info, settings.dry_run, exchange)
            else:
                entry_info = _place_entry_bybit(
                    session, symbol, side, atr,
                    settings.dry_run, lev_override, now,
                )
                if entry_info:
                    state["pending_orders"][symbol] = entry_info

        LOGGER.info(
            "── cycle end  scanned=%d  signals=%d  blocked(no_signal=%d htf=%d macro=%d panic=%d)  "
            "skipped(open=%d pending=%d freq=%d candle=%d)",
            scanned, signals, blocked_no_signal, blocked_htf, blocked_macro, blocked_panic,
            skipped_open, skipped_pending, skipped_freq, skipped_candle,
        )

        # ── 5. 30-min update ─────────────────────────────────────────────────
        if (now - last_update_at).total_seconds() >= UPDATE_INTERVAL:
            alert_update(position_alerter, live_positions, session, exchange)
            last_update_at = now
            state["last_update_alert"] = now.isoformat()

        save_state(state)

        if args.once:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
