"""
experiment_v2 — 1-hour wick-reversal strategy.

Supported exchanges: bybit (USDT-perp), bithumb (KRW spot, long-only).

Signal (all conditions must hold):
  1. Volume spike   : current 1H candle volume ≥ VMULT × avg over VLOOKBACK candles.
  2. Wick reversal  : Long  → lower-wick ≥ WICK_THRESH × range, close > open, close
                              above body midpoint.
                      Short → mirror. (Bithumb: Sell signals skipped — spot only.)
  3. Trend filter   : Long if close > EMA(EMA_PERIOD). Short if close < EMA.

Risk / sizing:
  - SL    : candle extreme ± ATR × SLATRMULT.
  - TP    : entry ± risk_distance × TPR.
  - Size  : (MAXRISKPCT × equity) / risk_distance, capped at NOTIONAL_CAP_USDT.
  - Bithumb tick-exit: when BITHUMB_TICK_EXIT > 0, TP/SL are set at ±N KRW ticks
            from entry instead of ATR-based levels.

Execution:
  - Bybit  : native TP/SL on the order (MarkPrice trigger).
  - Bithumb: GTC limit sell at TP price placed on fill; SL enforced by price polling.
  - All parameters hot-reloadable from params.json every tick.
  - Telegram alert on open / close, 30-min position summary.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

import yaml

from dotenv import load_dotenv
from pybit.exceptions import InvalidRequestError
from pybit.unified_trading import HTTP

from upbit_bybit_bot.config import load_settings
from upbit_bybit_bot.telegram_alerter import TelegramAlerter

LOGGER = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
# make this strategy's own modules importable when run via installed package
# bithumb_exchange.py is in experiment_v1 directory
if str(HERE.parent / "experiment_v1") not in sys.path:
    sys.path.insert(0, str(HERE.parent / "experiment_v1"))
from upbit_exchange import UpbitSession      # noqa: E402
from bithumb_exchange import BithumbSession  # noqa: E402

STATE_FILE = Path("data/experiment_v2_state.json")

CHECK_INTERVAL  = 60      # seconds between signal checks (overridden by trading_frequency.yaml)
UPDATE_INTERVAL = 1800    # 30 minutes between position-update Telegram alerts

# ── frequency globals (hot-reloaded from trading_frequency.yaml) ─────────────
MIN_BARS_BETWEEN_TRADES: int = 3   # 1H bars to wait after a trade before re-entering
MAX_OPEN_POSITIONS:      int = 3   # max concurrent open positions across all symbols

# ── mutable globals (hot-reloaded from params.json) ──────────────────────────
EMA_PERIOD:        int   = 50
ATR_PERIOD:        int   = 14
VMULT:             float = 2.0
VLOOKBACK:         int   = 10
WICK_THRESH:       float = 0.6
SLATRMULT:         float = 0.8
TPR:               float = 1.8
MAXRISKPCT:        float = 0.01    # 1 %
NORDERSPERHOUR:    int   = 2
NOTIONAL_CAP_USDT: float = 500.0
LEVERAGE:          int   = 5
TIME_IN_FORCE:     str   = "PostOnly"
BITHUMB_TICK_EXIT: int   = 0   # 0 = disabled; >0 = exit at ±N KRW ticks from entry


def load_frequency_config(exchange: str = "bybit") -> None:
    """Read trading_frequency.yaml; apply optional per-exchange overrides.
    Hot-reloaded every tick so changes take effect without restarting."""
    global NORDERSPERHOUR, MIN_BARS_BETWEEN_TRADES, MAX_OPEN_POSITIONS, CHECK_INTERVAL
    path = HERE / "trading_frequency.yaml"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    overrides = cfg.get("exchanges", {}).get(exchange, {})
    cfg = {**cfg, **overrides}
    NORDERSPERHOUR          = int(cfg.get("nordersperhour",          NORDERSPERHOUR))
    MIN_BARS_BETWEEN_TRADES = int(cfg.get("min_bars_between_trades", MIN_BARS_BETWEEN_TRADES))
    MAX_OPEN_POSITIONS      = int(cfg.get("max_open_positions",      MAX_OPEN_POSITIONS))
    CHECK_INTERVAL          = int(cfg.get("check_interval_seconds",  CHECK_INTERVAL))


def load_params(exchange: str = "bybit") -> None:
    """Read params.json; apply optional per-exchange overrides from
    params["exchanges"][exchange] on top of base values."""
    global EMA_PERIOD, ATR_PERIOD, VMULT, VLOOKBACK, WICK_THRESH
    global SLATRMULT, TPR, MAXRISKPCT, NORDERSPERHOUR, NOTIONAL_CAP_USDT
    global LEVERAGE, TIME_IN_FORCE, BITHUMB_TICK_EXIT, MIN_BARS_BETWEEN_TRADES
    path = HERE / "params.json"
    with path.open(encoding="utf-8") as fh:
        p = json.load(fh)
    overrides = p.get("exchanges", {}).get(exchange, {})
    p = {**p, **overrides}
    EMA_PERIOD        = int(p.get("ema_period",         EMA_PERIOD))
    ATR_PERIOD        = int(p.get("atr_period",         ATR_PERIOD))
    VMULT             = float(p.get("vmult",            VMULT))
    VLOOKBACK         = int(p.get("vlookback",          VLOOKBACK))
    WICK_THRESH       = float(p.get("wick_thresh",      WICK_THRESH))
    SLATRMULT         = float(p.get("slatrmult",        SLATRMULT))
    TPR               = float(p.get("tpr",              TPR))
    MAXRISKPCT        = float(p.get("maxriskpct",       MAXRISKPCT))
    NORDERSPERHOUR    = int(p.get("nordersperhour",     NORDERSPERHOUR))
    NOTIONAL_CAP_USDT = float(p.get("notional_cap_usdt", NOTIONAL_CAP_USDT))
    LEVERAGE          = int(p.get("leverage",           LEVERAGE))
    TIME_IN_FORCE     = str(p.get("time_in_force",      TIME_IN_FORCE))
    BITHUMB_TICK_EXIT = int(p.get("bithumb_tick_exit",  BITHUMB_TICK_EXIT))
    MIN_BARS_BETWEEN_TRADES = int(p.get("min_bars_between_trades", MIN_BARS_BETWEEN_TRADES))


# ── symbols ───────────────────────────────────────────────────────────────────

def load_symbols(exchange: str = "bybit") -> list[tuple[str, int | None]]:
    path = HERE / f"symbols_{exchange}.json"
    if not path.exists():
        raise FileNotFoundError(f"Symbol file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        entries = json.load(fh)["symbols"]
    result = []
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
        "open_positions": {},
        "pending_orders": {},
        "processed_candles": {},
        "signal_times": {},       # symbol → list[ISO timestamp] of recent opens
        "last_update_alert": None,
    }


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ── indicator maths ───────────────────────────────────────────────────────────

def _ema(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return float("nan")
    k = 2.0 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def _atr(highs: list[float], lows: list[float], closes: list[float],
         period: int) -> float:
    """Wilder-smooth ATR over `period` bars (needs at least period+1 bars)."""
    if len(highs) < period + 1:
        return float("nan")
    trs = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        trs.append(tr)
    # seed with simple mean of first `period` TRs, then Wilder-smooth
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


# ── signal detection ──────────────────────────────────────────────────────────

def check_signal(
    session: HTTP,
    symbol: str,
) -> tuple[str | None, str, dict]:
    """Evaluate the last closed 1H candle for entry signal.

    Returns (side_or_None, candle_start_time_str, tags_dict).
    tags_dict contains reason codes for logging.
    """
    # Need enough candles for EMA(50) + ATR(14) warmup
    limit = max(EMA_PERIOD, ATR_PERIOD) + 5
    resp = session.get_kline(category="linear", symbol=symbol,
                              interval="60", limit=limit)
    candles = resp["result"]["list"]
    # Bybit: newest first. candles[0] is still forming; candles[1] is last closed.
    if len(candles) < limit:
        return None, "", {"reject": "insufficient_candles"}

    # Build chronological arrays (oldest → newest closed)
    closed = list(reversed(candles[1:]))   # exclude still-forming candle[0]
    opens   = [float(c[1]) for c in closed]
    highs   = [float(c[2]) for c in closed]
    lows    = [float(c[3]) for c in closed]
    closes  = [float(c[4]) for c in closed]
    volumes = [float(c[5]) for c in closed]

    candle_ts = str(candles[1][0])

    signal_candle_idx = -1    # last element = most recent closed candle
    o = opens[signal_candle_idx]
    h = highs[signal_candle_idx]
    l = lows[signal_candle_idx]
    c = closes[signal_candle_idx]
    vol = volumes[signal_candle_idx]

    tags: list[str] = []
    reject: str | None = None

    # ── 1. Volume spike ───────────────────────────────────────────────────────
    avg_vol = sum(volumes[-(VLOOKBACK + 1):-1]) / VLOOKBACK if VLOOKBACK > 0 else 1.0
    if avg_vol > 0 and vol >= VMULT * avg_vol:
        tags.append("volume_spike")
    else:
        reject = "volume_spike_failed"
        return None, candle_ts, {"reject": reject, "tags": tags}

    # ── 2. Wick reversal shape ────────────────────────────────────────────────
    candle_range = h - l
    if candle_range == 0:
        return None, candle_ts, {"reject": "zero_range", "tags": tags}

    body_top    = max(o, c)
    body_bot    = min(o, c)
    lower_wick  = body_bot - l
    upper_wick  = h - body_top
    body_mid    = (body_top + body_bot) / 2.0

    long_wick  = (lower_wick >= WICK_THRESH * candle_range
                  and c > o
                  and c > body_mid)
    short_wick = (upper_wick >= WICK_THRESH * candle_range
                  and c < o
                  and c < body_mid)

    if long_wick:
        candle_side: str | None = "Buy"
        tags.append("wick_reversal_long")
    elif short_wick:
        candle_side = "Sell"
        tags.append("wick_reversal_short")
    else:
        reject = "wick_reversal_failed"
        return None, candle_ts, {"reject": reject, "tags": tags}

    # ── 3. Trend filter ───────────────────────────────────────────────────────
    ema_val = _ema(closes, EMA_PERIOD)
    if candle_side == "Buy"  and c <= ema_val:
        return None, candle_ts, {"reject": "trend_filter_failed_long",  "tags": tags}
    if candle_side == "Sell" and c >= ema_val:
        return None, candle_ts, {"reject": "trend_filter_failed_short", "tags": tags}
    tags.append("trend_ok")

    return candle_side, candle_ts, {"tags": tags, "reject": None,
                                    "atr": _atr(highs, lows, closes, ATR_PERIOD),
                                    "candle": {"o": o, "h": h, "l": l, "c": c}}


# ── instrument / price helpers ────────────────────────────────────────────────

def get_instrument(session: HTTP, symbol: str) -> dict | None:
    try:
        resp = session.get_instruments_info(category="linear", symbol=symbol)
    except InvalidRequestError:
        return None
    items = resp.get("result", {}).get("list", [])
    return items[0] if items else None


def _sync_pybit_clock(testnet: bool = False) -> None:
    """Patch pybit's timestamp generator to compensate for local clock skew.

    Bybit rejects requests if the local timestamp is more than 1 000 ms in the
    future OR more than recv_window ms in the past.  Windows clocks can drift
    badly in either direction, so we measure the offset against Bybit's server
    time (public endpoint, no auth) and bake the correction directly into
    pybit's internal helper so every subsequent signed request uses the right
    timestamp automatically.
    """
    import pybit._helpers as _pybit_helpers
    tmp = HTTP(testnet=testnet)
    resp = tmp.get_server_time()
    server_ms = int(resp["result"]["timeNano"]) // 1_000_000
    local_ms  = _pybit_helpers.generate_timestamp()
    offset_ms = server_ms - local_ms
    LOGGER.info("Bybit clock sync: server=%d local=%d offset=%+dms", server_ms, local_ms, offset_ms)
    _orig = _pybit_helpers.generate_timestamp
    _pybit_helpers.generate_timestamp = lambda: _orig() + offset_ms


def get_mark_price(session: HTTP, symbol: str) -> float | None:
    resp = session.get_tickers(category="linear", symbol=symbol)
    items = resp.get("result", {}).get("list", [])
    if not items:
        return None
    return float(items[0].get("markPrice") or 0) or None


def get_wallet_equity(session: HTTP) -> float:
    resp = session.get_wallet_balance(accountType="UNIFIED")
    acct = resp["result"]["list"][0]
    return float(acct.get("totalEquity") or 0)


def get_open_positions(session) -> dict[str, dict]:
    resp = session.get_positions(category="linear", settleCoin="USDT")
    return {
        item["symbol"]: item
        for item in resp["result"]["list"]
        if float(item.get("size") or 0) > 0
    }


# ── order / size helpers ──────────────────────────────────────────────────────

def _round_qty(qty: Decimal, step: Decimal) -> Decimal:
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def _round_price(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def calc_sl_tp(side: str, entry: float, atr: float, candle_low: float,
               candle_high: float, tick_size: str) -> tuple[str, str, float]:
    """Return (sl_price, tp_price, risk_distance) as strings.

    SL = candle extreme ± ATR × SLATRMULT.
    TP = entry + risk_distance × TPR.
    risk_distance = |entry - sl|.
    """
    tick = Decimal(tick_size)
    e = Decimal(str(entry))
    atr_off = Decimal(str(atr)) * Decimal(str(SLATRMULT))

    if side == "Buy":
        sl = _round_price(Decimal(str(candle_low))  - atr_off, tick)
        risk = e - sl
        tp = _round_price(e + risk * Decimal(str(TPR)), tick)
    else:
        sl = _round_price(Decimal(str(candle_high)) + atr_off, tick)
        risk = sl - e
        tp = _round_price(e - risk * Decimal(str(TPR)), tick)

    risk_f = float(risk)
    return format(sl.normalize(), "f"), format(tp.normalize(), "f"), risk_f


def calc_qty(equity: float, risk_distance: float, qty_step: str,
             min_qty: float, entry: float) -> str:
    """Size = (MAXRISKPCT × equity) / risk_distance, capped by NOTIONAL_CAP_USDT."""
    if risk_distance <= 0:
        return "0"
    raw_notional = MAXRISKPCT * equity / risk_distance * entry
    capped = min(raw_notional, NOTIONAL_CAP_USDT)
    step = Decimal(qty_step)
    qty = _round_qty(Decimal(str(capped)) / Decimal(str(entry)), step)
    min_qty_d = Decimal(str(min_qty))
    if qty < min_qty_d:
        qty = min_qty_d
    return format(qty.normalize(), "f")


# ── frequency guard ───────────────────────────────────────────────────────────

def _check_frequency(state: dict, symbol: str, now: datetime,
                     live_positions: dict | None = None) -> bool:
    """Return True if all frequency constraints allow a new position.

    Checks (in order):
      1. nordersperhour  — rolling 1-hour entry count per symbol.
      2. min_bars_between_trades — hours since last entry on this symbol.
      3. max_open_positions — total concurrent positions across all symbols.
    """
    # 1. rolling hourly cap
    window_start = now - timedelta(hours=1)
    times = state["signal_times"].get(symbol, [])
    recent = [t for t in times if datetime.fromisoformat(t).replace(tzinfo=UTC) >= window_start]
    state["signal_times"][symbol] = recent
    if len(recent) >= NORDERSPERHOUR:
        return False

    # 2. minimum bars (hours) since last entry on this symbol
    if MIN_BARS_BETWEEN_TRADES > 0:
        all_times = state["signal_times"].get(symbol, [])
        if all_times:
            last_entry = max(
                datetime.fromisoformat(t).replace(tzinfo=UTC) for t in all_times
            )
            hours_since = (now - last_entry).total_seconds() / 3600
            if hours_since < MIN_BARS_BETWEEN_TRADES:
                return False

    # 3. global open-position cap
    if MAX_OPEN_POSITIONS > 0 and live_positions is not None:
        total_open = len(live_positions) + len(state.get("pending_orders", {}))
        if total_open >= MAX_OPEN_POSITIONS:
            return False

    return True


def _record_signal_time(state: dict, symbol: str, now: datetime) -> None:
    state["signal_times"].setdefault(symbol, []).append(now.isoformat())


# ── trade execution ───────────────────────────────────────────────────────────

def open_position(session, symbol: str, side: str,
                  signal_meta: dict, dry_run: bool,
                  leverage_override: int | None = None,
                  is_spot: bool = False) -> dict | None:
    lev = 1 if is_spot else (leverage_override if leverage_override is not None else LEVERAGE)
    inst = get_instrument(session, symbol)
    if not inst:
        LOGGER.warning("Instrument not found: %s", symbol)
        return None

    mark_price = get_mark_price(session, symbol)
    if mark_price is None:
        LOGGER.warning("No mark price for %s", symbol)
        return None

    atr          = signal_meta["atr"]
    candle       = signal_meta["candle"]
    lot          = inst.get("lotSizeFilter", {})
    tick_size    = inst.get("priceFilter", {}).get("tickSize", "0.01")
    qty_step     = lot.get("qtyStep", "1")
    min_qty      = float(lot.get("minOrderQty", "0"))
    min_notional = float(lot.get("minNotionalValue", "1"))

    if atr <= 0 or atr != atr:   # nan / zero guard
        LOGGER.warning("Skipping %s: invalid ATR=%.6f", symbol, atr)
        return None

    sl_price, tp_price, risk_dist = calc_sl_tp(
        side, mark_price, atr, candle["l"], candle["h"], tick_size,
    )

    if risk_dist <= 0:
        LOGGER.warning("Skipping %s: risk_distance=%.6f <= 0", symbol, risk_dist)
        return None

    try:
        equity = get_wallet_equity(session)
    except Exception as err:
        LOGGER.warning("Could not fetch equity for %s: %s — skipping", symbol, err)
        return None

    qty_str  = calc_qty(equity, risk_dist, qty_step, min_qty, mark_price)
    qty_f    = float(qty_str)
    notional = qty_f * mark_price

    if notional < min_notional:
        LOGGER.warning("Skipping %s: notional $%.2f below min $%.2f",
                       symbol, notional, min_notional)
        return None

    entry_price_str = format(
        _round_price(Decimal(str(mark_price)), Decimal(tick_size)).normalize(), "f"
    )

    if dry_run:
        LOGGER.info("[DRY RUN] OPEN %s %s  qty=%s  entry≈%s  tp=%s  sl=%s  atr=%.4f  tags=%s",
                    side, symbol, qty_str, entry_price_str, tp_price, sl_price, atr,
                    signal_meta.get("tags", []))
        return {"symbol": symbol, "side": side, "qty": qty_str,
                "entry_price": mark_price, "tp_price": tp_price, "sl_price": sl_price}

    if not is_spot:
        try:
            session.set_leverage(category="linear", symbol=symbol,
                                 buyLeverage=str(lev), sellLeverage=str(lev))
        except Exception as err:
            LOGGER.debug("Leverage set skipped for %s: %s", symbol, err)

    order_kwargs: dict = dict(
        category="linear",
        symbol=symbol,
        side=side,
        orderType="Limit",
        price=entry_price_str,
        qty=qty_str,
        timeInForce=TIME_IN_FORCE,
    )
    if not is_spot:
        order_kwargs.update(
            takeProfit=tp_price,
            stopLoss=sl_price,
            tpTriggerBy="MarkPrice",
            slTriggerBy="MarkPrice",
        )

    resp = session.place_order(**order_kwargs)
    order_id = resp["result"]["orderId"]
    LOGGER.info(
        "Placed %s %s qty=%s price=%s  tp=%s sl=%s  atr=%.4f  tags=%s  orderId=%s",
        side, symbol, qty_str, entry_price_str, tp_price, sl_price, atr,
        signal_meta.get("tags", []), order_id,
    )
    return {
        "symbol": symbol, "side": side, "qty": qty_str,
        "entry_price": mark_price, "tp_price": tp_price, "sl_price": sl_price,
        "order_id": order_id,
    }


# ── Bithumb: place GTC TP limit sell on fill ──────────────────────────────────

def _place_bithumb_tp_order(session, symbol: str, pos: dict, state: dict) -> None:
    """Place a GTC limit sell at the TP price right after a Bithumb buy fills.

    If BITHUMB_TICK_EXIT > 0 the TP/SL are recalculated using ±N KRW ticks
    from the actual fill entry; otherwise falls back to the ATR-based stored price.
    The TP order ID is saved in state so section 2c can check for fill or cancel
    it when SL is hit.
    """
    tracked = state["open_positions"].get(symbol, {})
    qty = pos.get("size") or tracked.get("qty")

    if BITHUMB_TICK_EXIT > 0:
        entry_usdt = float(tracked.get("entry_price") or 0)
        if entry_usdt:
            try:
                rate = session._krw_usdt_rate()
                entry_krw_raw = entry_usdt * rate
                krw_tick = session._krw_tick(entry_krw_raw)
                entry_krw = round(entry_krw_raw / krw_tick) * krw_tick
                tp_krw = entry_krw + BITHUMB_TICK_EXIT * krw_tick
                sl_krw = entry_krw - BITHUMB_TICK_EXIT * krw_tick
                tp_tick = session._krw_tick(tp_krw)
                if tp_tick != krw_tick:
                    tp_krw = (tp_krw // tp_tick) * tp_tick
                tp_price = str(tp_krw / rate)
                tracked["tp_price"] = tp_price
                tracked["sl_price"] = str(sl_krw / rate)
                LOGGER.info(
                    "Bithumb tick-exit for %s: entry=₩%d  tick=₩%d  tp=₩%d  sl=₩%d  (±%d ticks)",
                    symbol, entry_krw, krw_tick, tp_krw, sl_krw, BITHUMB_TICK_EXIT,
                )
            except Exception as err:
                LOGGER.warning("tick-exit calc failed for %s: %s", symbol, err)
                tp_price = tracked.get("tp_price")
        else:
            tp_price = tracked.get("tp_price")
    else:
        tp_price = tracked.get("tp_price")

    if not tp_price or not qty:
        LOGGER.warning("Cannot place Bithumb TP sell for %s: tp_price or qty missing", symbol)
        return
    try:
        resp = session.place_order(
            category="linear", symbol=symbol,
            side="Sell", orderType="Limit",
            price=tp_price, qty=qty, timeInForce="GTC",
        )
        tp_order_id = resp["result"]["orderId"]
        state["open_positions"][symbol]["tp_order_id"] = tp_order_id
        LOGGER.info("Bithumb TP limit sell placed for %s: price=%s qty=%s orderId=%s",
                    symbol, tp_price, qty, tp_order_id)
    except Exception as err:
        LOGGER.error("Failed to place Bithumb TP limit sell for %s: %s", symbol, err)


# ── TP/SL repair (Bybit post-restart) ────────────────────────────────────────

def _apply_tp_sl(session, symbol: str, side: str,
                 state: dict, tp: str, sl: str) -> bool:
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
        return True
    except Exception as err:
        if "34040" in str(err):
            return True   # "not modified" — already set
        LOGGER.warning("set_trading_stop failed for %s: %s", symbol, err)
        return False


# ── Telegram alerts ───────────────────────────────────────────────────────────

def _sgn(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v:,.2f}"


def _pnl_emoji(v: float) -> str:
    return "🟢" if v >= 0 else "🔴"


def alert_open(alerter: TelegramAlerter | None, info: dict, dry_run: bool) -> None:
    if alerter is None:
        return
    label = "Long 🔺" if info["side"] == "Buy" else "Short 🔻"
    dry   = "  [DRY RUN]" if dry_run else ""
    alerter.send(
        f"🟢 <b>Position opened{dry}</b>\n"
        f"Strategy: <b>experiment_v2</b>\n"
        f"Symbol: <b>{info['symbol']}</b>  {label}\n"
        f"Entry ≈ ${float(info['entry_price']):,.4f}  Qty: {info['qty']}\n"
        f"TP: ${info['tp_price']}   SL: ${info['sl_price']}"
    )


def alert_close(alerter: TelegramAlerter | None, tracked: dict,
                exit_price: float, reason: str, dry_run: bool) -> None:
    if alerter is None:
        return
    side   = tracked["side"]
    entry  = float(tracked["entry_price"])
    qty_f  = float(tracked["qty"])
    pnl     = (exit_price - entry) * qty_f if side == "Buy" else (entry - exit_price) * qty_f
    pnl_pct = (pnl / (entry * qty_f) * 100) if entry and qty_f else 0.0
    label  = "Long 🔺" if side == "Buy" else "Short 🔻"
    dry    = "  [DRY RUN]" if dry_run else ""
    alerter.send(
        f"{_pnl_emoji(pnl)} <b>Position closed{dry}</b>  [{reason}]\n"
        f"Strategy: <b>experiment_v2</b>\n"
        f"Symbol: <b>{tracked['symbol']}</b>  {label}\n"
        f"Entry: ${entry:,.4f}   Exit: ${exit_price:,.4f}\n"
        f"PnL: {_sgn(pnl)} USDT  ({_sgn(pnl_pct)}%)"
    )


def alert_update(alerter: TelegramAlerter | None, positions: dict[str, dict],
                 session=None, exchange: str = "bybit") -> None:
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

    def _fmt_price(usdt: float) -> str:
        return f"₩{usdt * krw_rate:,.0f}" if is_krw else f"${usdt:,.4f}"

    def _fmt_pnl(usdt: float) -> str:
        return f"{_sgn(usdt * krw_rate)} KRW" if is_krw else f"{_sgn(usdt)} USDT"

    balance_line = "💰 Balance: (unavailable)\n"
    if session is not None:
        for _attempt in range(2):
            try:
                resp  = session.get_wallet_balance(accountType="UNIFIED")
                acct  = resp["result"]["list"][0]
                total = float(acct.get("totalEquity") or 0)
                avail = float(acct.get("totalAvailableBalance") or 0)
                if is_krw:
                    balance_line = (
                        f"💰 Balance: <b>₩{total * krw_rate:,.0f}</b>  "
                        f"Avail: ₩{avail * krw_rate:,.0f}\n"
                    )
                else:
                    upnl = float(acct.get("totalPerpUPL") or 0)
                    balance_line = (
                        f"💰 Balance: <b>${total:,.2f}</b>  "
                        f"Avail: ${avail:,.2f}  uPnL: {_sgn(upnl)} USDT\n"
                    )
                break
            except Exception as err:
                LOGGER.warning("Failed to fetch wallet balance (attempt %d): %s", _attempt + 1, err)
                time.sleep(2)

    if not positions:
        alerter.send(f"📊 <b>experiment_v2 · 30-min update</b>\n🕐 {now_kst}\n{balance_line}No open positions.")
        return
    lines = ["📊 <b>experiment_v2 · 30-min update</b>", f"🕐 {now_kst}", balance_line.strip(), ""]
    for symbol, pos in positions.items():
        side  = pos["side"]
        upnl  = float(pos.get("unrealisedPnl") or 0)
        entry = float(pos.get("avgPrice") or 0)
        mark  = float(pos.get("markPrice") or 0)
        size  = float(pos.get("size") or 0)
        cost  = size * entry
        pct   = (upnl / cost * 100) if cost else 0.0
        label = "Long 🔺" if side == "Buy" else "Short 🔻"
        lines.append(
            f"{_pnl_emoji(upnl)} <b>{symbol}</b>  {label}\n"
            f"  Entry {_fmt_price(entry)}  Mark {_fmt_price(mark)}  Size {size}\n"
            f"  PnL {_fmt_pnl(upnl)}  ({_sgn(pct)}%)"
        )
    alerter.send("\n".join(lines))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    load_params()  # pre-parse defaults; reloaded with exchange after args parsed
    load_frequency_config()  # pre-parse frequency defaults

    parser = argparse.ArgumentParser(description="experiment_v2: 1H wick-reversal strategy")
    parser.add_argument("--once",       action="store_true", help="Run one cycle then exit")
    parser.add_argument("--update",     action="store_true", help="Send position update and exit")
    parser.add_argument("--cancel-all", action="store_true", help="Cancel all open orders and exit")
    parser.add_argument("--close-all",  action="store_true", help="Close all positions at market and exit")
    parser.add_argument("--exchange",   choices=["bybit", "bithumb"], default="bybit",
                        help="Exchange to trade on (default: bybit)")
    parser.add_argument("--debug",      action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for _noisy in ("urllib3", "pybit", "requests", "httpcore", "httpx"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    exchange = args.exchange
    is_spot  = exchange == "bithumb"

    global STATE_FILE
    STATE_FILE = Path(f"data/experiment_v2_{exchange}_state.json")
    load_params(exchange)  # reload with exchange-specific overrides
    load_frequency_config(exchange)  # reload frequency config with exchange overrides
    # params.json per-exchange overrides must win over yaml defaults for shared keys
    load_params(exchange)

    settings = load_settings()
    if exchange == "bithumb":
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
    position_bot_token   = _pos_token_path.read_text(encoding="utf-8").strip() \
                           if _pos_token_path.exists() else None
    position_bot_chat_id = os.getenv("POSITION_BOT_CHAT_ID") or None
    position_alerter = TelegramAlerter.from_env(token=position_bot_token,
                                                chat_id=position_bot_chat_id)

    if args.update:
        alert_update(position_alerter, get_open_positions(session), session, exchange)
        LOGGER.info("Position update sent.")
        return

    if args.cancel_all:
        resp = session.cancel_all_orders(category="linear", settleCoin="USDT")
        cancelled = resp.get("result", {}).get("list", [])
        LOGGER.info("Cancelled %d order(s).", len(cancelled))
        state = load_state()
        state["pending_orders"] = {}
        save_state(state)
        return

    if args.close_all:
        positions = get_open_positions(session)
        if not positions:
            LOGGER.info("No open positions to close.")
        else:
            session.cancel_all_orders(category="linear", settleCoin="USDT")
            for sym, pos in positions.items():
                close_side = "Sell" if pos["side"] == "Buy" else "Buy"
                try:
                    session.place_order(
                        category="linear", symbol=sym,
                        side=close_side, orderType="Market",
                        qty=pos["size"], reduceOnly=True, timeInForce="IOC",
                    )
                    LOGGER.info("Closed %s  side=%s  qty=%s", sym, close_side, pos["size"])
                except Exception as err:
                    LOGGER.error("Failed to close %s: %s", sym, err)
        state = load_state()
        state["open_positions"] = {}
        state["pending_orders"]  = {}
        save_state(state)
        return

    symbols = load_symbols(exchange)
    LOGGER.info("experiment_v2 started | exchange=%s | %d symbols | dry_run=%s",
                exchange, len(symbols), settings.dry_run)

    state = load_state()

    # Clear processed_candles entries older than 2 hours so a restarted bot
    # doesn't silently skip symbols whose candle was cached in a previous run.
    cutoff_ts = int((datetime.now(tz=UTC) - timedelta(hours=2)).timestamp() * 1000)
    stale = [sym for sym, ts in state.get("processed_candles", {}).items()
             if int(ts) < cutoff_ts]
    for sym in stale:
        del state["processed_candles"][sym]
    if stale:
        LOGGER.info("Cleared %d stale processed_candles entries: %s", len(stale), stale)
        save_state(state)
    last_update_at = datetime.now(tz=UTC) - timedelta(seconds=UPDATE_INTERVAL)

    while True:
        now = datetime.now(tz=UTC)
        load_params(exchange)  # hot-reload every tick with exchange overrides
        load_frequency_config(exchange)  # hot-reload frequency config
        load_params(exchange)  # re-apply params so exchange overrides win over yaml

        # ── 1. Fetch live positions ───────────────────────────────────────────
        try:
            live_positions = get_open_positions(session)
        except Exception as err:
            LOGGER.error("Failed to fetch positions: %s", err)
            if args.once:
                break
            time.sleep(CHECK_INTERVAL)
            continue

        # ── 1b. Cancel pending orders unfilled for > 60 s ────────────────────
        state.setdefault("pending_orders", {})
        for sym, pending in list(state["pending_orders"].items()):
            placed_at = datetime.fromisoformat(pending["placed_at"]).replace(tzinfo=UTC)
            if (now - placed_at).total_seconds() < 60:
                continue
            try:
                session.cancel_order(category="linear", symbol=sym,
                                     orderId=pending["order_id"])
                LOGGER.info("Cancelled unfilled order %s for %s", pending["order_id"], sym)
            except Exception as err:
                if "110001" in str(err):
                    LOGGER.info("Order %s for %s already filled (cancel too late)",
                                pending["order_id"], sym)
                    if sym in live_positions:
                        if is_spot:
                            _place_bithumb_tp_order(session, sym, live_positions[sym], state)
                        else:
                            tracked = state["open_positions"].get(sym, {})
                            _apply_tp_sl(session, sym, live_positions[sym]["side"], state,
                                         tracked.get("tp_price", ""), tracked.get("sl_price", ""))
                    del state["pending_orders"][sym]
                    continue
                LOGGER.warning("Cancel failed for %s: %s", sym, err)
            del state["pending_orders"][sym]
            state["open_positions"].pop(sym, None)

        # ── 1c. Detect just-filled pending orders → place TP/SL ──────────────
        for sym in list(state["pending_orders"].keys()):
            if sym not in live_positions:
                continue
            LOGGER.info("Order filled: %s — placing TP/SL", sym)
            if is_spot:
                _place_bithumb_tp_order(session, sym, live_positions[sym], state)
            else:
                tracked = state["open_positions"].get(sym, {})
                _apply_tp_sl(session, sym, live_positions[sym]["side"], state,
                             tracked.get("tp_price", ""), tracked.get("sl_price", ""))
            del state["pending_orders"][sym]

        # ── 2. Detect closes (TP/SL hit by exchange or bot) ───────────────────
        for symbol, tracked in list(state["open_positions"].items()):
            if symbol in live_positions:
                continue
            if symbol in state.get("pending_orders", {}):
                continue
            exit_price = get_mark_price(session, symbol) or float(tracked.get("entry_price", 0))
            LOGGER.info("Detected close: %s %s  exit≈%s", tracked["side"], symbol, exit_price)
            alert_close(alerter, tracked, exit_price, "TP/SL hit", settings.dry_run)
            del state["open_positions"][symbol]

        # ── 2b. Bybit: repair missing TP/SL on existing positions ─────────────
        if not is_spot:
            for symbol, pos in live_positions.items():
                has_tp = bool(pos.get("takeProfit") and float(pos["takeProfit"]) != 0)
                has_sl = bool(pos.get("stopLoss")   and float(pos["stopLoss"])   != 0)
                if has_tp and has_sl:
                    continue
                tracked = state["open_positions"].get(symbol, {})
                tp = tracked.get("tp_price", "")
                sl = tracked.get("sl_price", "")
                if tp and sl:
                    _apply_tp_sl(session, symbol, pos["side"], state, tp, sl)
                    LOGGER.info("Repaired TP/SL for %s: tp=%s sl=%s", symbol, tp, sl)

        # ── 2c. Bithumb: per-tick manual TP/SL enforcement ────────────────────
        if is_spot:
            configured_symbols = {s for s, _ in symbols}
            for symbol, tracked in list(state["open_positions"].items()):
                if symbol not in live_positions or symbol not in configured_symbols:
                    continue
                current_price = get_mark_price(session, symbol)
                if current_price is None:
                    continue

                tp_order_id = tracked.get("tp_order_id")
                # Check if the GTC limit sell already filled (= TP hit)
                if tp_order_id:
                    try:
                        order = session.get_order(orderId=tp_order_id)
                        if order.get("state") == "done":
                            exec_price = float(order.get("price") or tracked.get("tp_price", current_price))
                            LOGGER.info("TP sell filled for %s: price=%s", symbol, exec_price)
                            alert_close(alerter, tracked, exec_price, "TP hit", settings.dry_run)
                            del state["open_positions"][symbol]
                            state["pending_orders"].pop(symbol, None)
                            continue
                    except Exception as err:
                        LOGGER.warning("get_order failed for %s tp_order %s: %s",
                                       symbol, tp_order_id, err)

                sl = float(tracked.get("sl_price") or 0)
                if sl and 0 < current_price <= sl:
                    # SL hit: cancel TP limit order, then market sell
                    if tp_order_id and not settings.dry_run:
                        try:
                            session.cancel_order(category="linear", symbol=symbol,
                                                 orderId=tp_order_id)
                            LOGGER.info("Cancelled TP order %s for %s (SL triggered)",
                                        tp_order_id, symbol)
                        except Exception as err:
                            LOGGER.warning("Could not cancel TP order for %s: %s", symbol, err)
                    qty = live_positions[symbol].get("size") or tracked.get("qty")
                    try:
                        if not settings.dry_run:
                            session.place_order(
                                category="linear", symbol=symbol,
                                side="Sell", orderType="Market",
                                qty=qty, timeInForce="IOC",
                            )
                        LOGGER.info("SL hit for %s: price=%s", symbol, current_price)
                        alert_close(alerter, tracked, current_price, "SL hit", settings.dry_run)
                    except Exception as err:
                        LOGGER.error("SL close failed for %s: %s", symbol, err)
                        continue
                    del state["open_positions"][symbol]
                    state["pending_orders"].pop(symbol, None)

        # ── 3. Signal scan ────────────────────────────────────────────────────
        expected_candle_ts = str((int(now.timestamp() // 3600) - 1) * 3600 * 1000)

        for symbol, lev_override in symbols:
            has_pending  = symbol in state.get("pending_orders", {})
            has_position = symbol in state["open_positions"] or symbol in live_positions
            if has_pending or has_position:
                continue

            if expected_candle_ts == state["processed_candles"].get(symbol):
                continue

            if not _check_frequency(state, symbol, now, live_positions):
                LOGGER.info("Frequency limit reached for %s — skipping", symbol)
                continue

            try:
                side, candle_ts, meta = check_signal(session, symbol)
            except Exception as err:
                LOGGER.warning("Signal check failed %s: %s", symbol, err)
                continue

            if meta.get("reject"):
                LOGGER.info("Signal rejected %s: %s  tags=%s",
                             symbol, meta["reject"], meta.get("tags", []))

            if candle_ts and candle_ts == state["processed_candles"].get(symbol):
                continue
            if candle_ts:
                state["processed_candles"][symbol] = candle_ts

            if side is None:
                continue

            # Bithumb is long-only — skip any Sell signals
            if is_spot and side == "Sell":
                continue

            LOGGER.info("Signal → %s %s  tags=%s", side, symbol, meta.get("tags", []))
            result = open_position(session, symbol, side, meta, settings.dry_run,
                                   lev_override, is_spot=is_spot)
            if result:
                state["open_positions"][symbol] = {
                    "symbol":      symbol,
                    "side":        side,
                    "entry_price": result["entry_price"],
                    "qty":         result["qty"],
                    "tp_price":    result["tp_price"],
                    "sl_price":    result["sl_price"],
                    "open_time":   now.isoformat(),
                }
                order_id = result.get("order_id")
                if order_id:
                    state["pending_orders"][symbol] = {
                        "order_id": order_id,
                        "placed_at": now.isoformat(),
                    }
                _record_signal_time(state, symbol, now)
                alert_open(alerter, result, settings.dry_run)
                live_positions = get_open_positions(session)

        # ── 4. 30-min update alert ────────────────────────────────────────────
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
