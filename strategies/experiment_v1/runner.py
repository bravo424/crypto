"""
experiment_v1 — 5-minute volume-spike momentum strategy on Bybit USDT-perp.

Signal:  last completed 5-min candle volume > previous candle × 1.1
         AND candle is bullish  → Long (Buy)
         AND candle is bearish  → Short (Sell)

Risk:    leverage 10×, $10 USDT notional per position
         TP +5%, SL -3% (set natively on Bybit so they survive restarts)

Alerts:  Telegram on every open / close, plus a 30-min position summary
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
# make this strategy's own modules importable when run via installed package
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from upbit_exchange import UpbitSession      # noqa: E402
from bithumb_exchange import BithumbSession  # noqa: E402

STATE_FILE = Path("data/experiment_v1_state.json")

MAX_NOTIONAL_MULTIPLIER = 3.0   # skip if min order costs > 3× NOTIONAL_USDT ($30)
CHECK_INTERVAL  = 60            # seconds between signal checks
UPDATE_INTERVAL = 1800          # 30 minutes between position-update alerts

# Mutable params — overwritten by load_params() at startup and on each loop tick
LEVERAGE: int             = 20
NOTIONAL_USDT: float      = 20.0
NOTIONAL_CAP_USDT: float  = 500.0
TAKE_PROFIT_PCT: Decimal  = Decimal("0.05")
STOP_LOSS_PCT: Decimal    = Decimal("0.03")
VOLUME_SPIKE: float       = 1.20
TIME_IN_FORCE: str        = "PostOnly"
USE_369: bool             = True
BITHUMB_TICK_EXIT: int    = 0   # 0 = disabled; >0 = exit at ±N KRW ticks from entry


# ── params (hot-reloadable) ───────────────────────────────────────────────────

def load_params(exchange: str = "bybit") -> None:
    """Read params.json and update module-level strategy constants in place.
    Called once at startup and at the top of every loop iteration so changes
    to params.json take effect without restarting the process.

    Exchange-specific overrides can be placed under an "exchanges" key:
      {"exchanges": {"bithumb": {"notional_cap_usdt": 69.0}}}
    These overlay the top-level defaults for the given exchange only.
    """
    global LEVERAGE, NOTIONAL_USDT, NOTIONAL_CAP_USDT, TAKE_PROFIT_PCT, STOP_LOSS_PCT, VOLUME_SPIKE, TIME_IN_FORCE, USE_369, BITHUMB_TICK_EXIT
    path = HERE / "params.json"
    with path.open(encoding="utf-8") as fh:
        p = json.load(fh)
    # apply per-exchange overrides on top of the base dict
    overrides = p.get("exchanges", {}).get(exchange, {})
    p = {**p, **overrides}
    LEVERAGE          = int(p.get("leverage",          LEVERAGE))
    NOTIONAL_USDT     = float(p.get("notional_usdt",   NOTIONAL_USDT))
    NOTIONAL_CAP_USDT = float(p.get("notional_cap_usdt", NOTIONAL_CAP_USDT))
    TAKE_PROFIT_PCT   = Decimal(str(p.get("take_profit_pct", TAKE_PROFIT_PCT)))
    STOP_LOSS_PCT     = Decimal(str(p.get("stop_loss_pct",   STOP_LOSS_PCT)))
    VOLUME_SPIKE      = float(p.get("volume_spike",    VOLUME_SPIKE))
    TIME_IN_FORCE     = str(p.get("time_in_force",     TIME_IN_FORCE))
    USE_369           = bool(p.get("use_369",           USE_369))
    BITHUMB_TICK_EXIT = int(p.get("bithumb_tick_exit",  BITHUMB_TICK_EXIT))


# ── symbols ───────────────────────────────────────────────────────────────────

def load_symbols(exchange: str = "bybit") -> list[tuple[str, int | None]]:
    """Return list of (symbol, leverage_override_or_None) for the given exchange.

    Looks for symbols_{exchange}.json (e.g. symbols_bybit.json).
    Bybit entries can be a plain string or {"symbol": ..., "leverage": ...}.
    Spot exchanges (upbit, bithumb) use plain strings only — leverage is always 1.
    """
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
    return {"open_positions": {}, "pending_orders": {}, "processed_candles": {}, "last_update_alert": None}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ── signal detection ──────────────────────────────────────────────────────────

# ── 369 strategy ───────────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> float:
    """Calculate EMA for given period using the last N closing prices.
    Uses standard smoothing factor k = 2 / (period + 1).
    """
    if len(values) < period:
        return float("nan")
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period  # seed with SMA of first `period` values
    for price in values[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def check_369(closes: list[float]) -> str | None:
    """Return 'Buy' if EMAs are bullishly stacked, 'Sell' if bearishly stacked,
    None if no clear alignment.

    Requires close > EMA3 > EMA6 > EMA9 for Long.
    Requires close < EMA3 < EMA6 < EMA9 for Short.
    """
    if len(closes) < 9:
        return None
    ema3 = _ema(closes, 3)
    ema6 = _ema(closes, 6)
    ema9 = _ema(closes, 9)
    price = closes[-1]
    if price > ema3 > ema6 > ema9:
        return "Buy"
    if price < ema3 < ema6 < ema9:
        return "Sell"
    return None


# ── signal detection ────────────────────────────────────────────────────────────

def check_signal(session: HTTP, symbol: str) -> tuple[str | None, str]:
    """Return (side or None, candle_start_time_ms_str) for last completed 5-min candle."""
    # Fetch enough candles for EMA-9 warmup (30 gives reliable EMA values)
    limit = 30 if USE_369 else 3
    resp = session.get_kline(category="linear", symbol=symbol, interval="5", limit=limit)
    candles = resp["result"]["list"]
    # Bybit returns newest first; [0] may still be forming
    # [1] = last fully closed candle, [2] = the one before it
    if len(candles) < 3:
        return None, ""

    current  = candles[1]   # [startTime, open, high, low, close, volume, turnover]
    previous = candles[2]
    start_time = str(current[0])

    vol_now  = float(current[5])
    vol_prev = float(previous[5])
    if vol_prev == 0:
        return None, start_time

    if vol_now / vol_prev < VOLUME_SPIKE:
        return None, start_time

    # Candle direction from the last completed candle
    price_move = float(current[4]) - float(current[1])
    if price_move > 0:
        candle_side: str | None = "Buy"
    elif price_move < 0:
        candle_side = "Sell"
    else:
        return None, start_time

    if not USE_369:
        return candle_side, start_time

    # 369 confirmation: EMAs computed on closed candles (exclude candle[0] still forming)
    # candles are newest-first; reverse to get chronological order, skip candle[0]
    closed = candles[1:]          # exclude the still-forming candle
    closes = [float(c[4]) for c in reversed(closed)]
    side_369 = check_369(closes)

    if side_369 is None:
        LOGGER.debug("369 filter: no EMA alignment for %s — skipping", symbol)
        return None, start_time
    if side_369 != candle_side:
        LOGGER.debug("369 filter: EMA says %s but candle says %s for %s — skipping",
                     side_369, candle_side, symbol)
        return None, start_time

    return candle_side, start_time


# ── instrument helpers ────────────────────────────────────────────────────────

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
    value = float(items[0].get("markPrice") or 0)
    return value or None


# ── order helpers ─────────────────────────────────────────────────────────────

def _round_qty(qty: Decimal, step: Decimal) -> Decimal:
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def _round_price(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def calc_qty(notional: float, mark_price: float, qty_step: str, min_qty: float) -> str:
    """Return qty string: target notional rounded to step, clamped up to min_qty."""
    step = Decimal(qty_step)
    raw  = Decimal(str(notional)) / Decimal(str(mark_price))
    qty  = _round_qty(raw, step)
    # if rounding down gave 0 or below min_qty, snap up to min_qty (one step)
    min_qty_d = Decimal(str(min_qty))
    if qty < min_qty_d:
        qty = min_qty_d
    return format(qty.normalize(), "f")


def calc_tp_sl(side: str, entry: float, tick_size: str,
               leverage: int | None = None) -> tuple[str, str]:  # noqa: ARG001
    """Compute absolute TP/SL price levels from params.json percentages.

    Both take_profit_pct and stop_loss_pct are direct price-move fractions:
      0.03 = TP fires when price moves +3% from entry.
      0.02 = SL fires when price moves -2% from entry.
    Leverage does NOT affect these levels — set the percentages in params.json
    at values that make sense relative to your leverage (e.g. at 20× leverage a
    2% price SL = -40% margin loss, safely above the ~5% liquidation boundary).
    """
    p       = Decimal(str(entry))
    tick    = Decimal(tick_size)
    tp_move = TAKE_PROFIT_PCT
    sl_move = STOP_LOSS_PCT
    if side == "Buy":
        tp = _round_price(p * (1 + tp_move), tick)
        sl = _round_price(p * (1 - sl_move), tick)
    else:
        tp = _round_price(p * (1 - tp_move), tick)
        sl = _round_price(p * (1 + sl_move), tick)
    return format(tp.normalize(), "f"), format(sl.normalize(), "f")


def _apply_tp_sl(session, symbol: str, side: str, state: dict,
                 leverage_override: int | None = None) -> tuple[str, str] | None:
    """Compute TP/SL from the current mark price + params.json percentages and
    call set_trading_stop.  Always uses live mark price so prices are guaranteed
    to be valid relative to where the market is right now.

    Pass leverage_override=1 for Upbit spot (no leverage); omit for Bybit
    (falls back to the global LEVERAGE setting).

    Updates state["open_positions"][symbol] on success.
    Returns (tp, sl) strings on success, None on failure.
    """
    try:
        mark = get_mark_price(session, symbol)
        if not mark:
            LOGGER.warning("Cannot set TP/SL for %s: mark price unavailable", symbol)
            return None
        inst = get_instrument(session, symbol)
        tick = inst.get("priceFilter", {}).get("tickSize", "0.01") if inst else "0.01"
        tp, sl = calc_tp_sl(side, mark, tick, leverage_override)
        session.set_trading_stop(
            category="linear", symbol=symbol,
            takeProfit=tp, stopLoss=sl,
            tpTriggerBy="MarkPrice", slTriggerBy="MarkPrice",
            positionIdx=0,
        )
        if symbol in state.get("open_positions", {}):
            state["open_positions"][symbol]["tp_price"] = tp
            state["open_positions"][symbol]["sl_price"] = sl
        return tp, sl
    except Exception as err:
        err_str = str(err)
        if "34040" in err_str:
            # "not modified" — already set to these exact values; treat as success
            mark2 = get_mark_price(session, symbol) or 0
            inst2 = get_instrument(session, symbol)
            tick2 = inst2.get("priceFilter", {}).get("tickSize", "0.01") if inst2 else "0.01"
            tp2, sl2 = calc_tp_sl(side, mark2, tick2, leverage_override) if mark2 else ("", "")
            return (tp2, sl2) if tp2 else None
        LOGGER.warning("set_trading_stop failed for %s: %s", symbol, err)
        return None


# ── trade execution ───────────────────────────────────────────────────────────

def _place_bithumb_tp_order(session, symbol: str, pos: dict, state: dict) -> None:
    """Bithumb spot: place a GTC limit sell at the TP price immediately after a buy fills.

    If BITHUMB_TICK_EXIT > 0 (set via params.json exchanges.bithumb.bithumb_tick_exit),
    TP is placed at entry + N×KRW-tick and SL threshold is stored at entry - N×KRW-tick
    (section 2c will market-sell when price drops to that level).
    Falls back to the percentage-based tp_price stored at open time when disabled.

    The TP order ID is stored in state["open_positions"][symbol]["tp_order_id"] so
    section 2c can verify fill (= TP hit) or cancel it before an SL market sell.
    """
    tracked = state["open_positions"].get(symbol, {})
    qty = pos.get("size") or tracked.get("qty")

    # ── tick-based TP/SL override ──────────────────────────────────────────────
    if BITHUMB_TICK_EXIT > 0:
        entry_usdt = float(tracked.get("entry_price") or 0)
        if entry_usdt:
            try:
                rate = session._krw_usdt_rate()
                # Work in integer KRW to avoid precision loss in the USDT↔KRW round-trip.
                entry_krw_raw = entry_usdt * rate
                krw_tick = session._krw_tick(entry_krw_raw)
                # Snap entry to the nearest tick (round, not floor) so TP/SL are symmetric.
                entry_krw = round(entry_krw_raw / krw_tick) * krw_tick
                tp_krw = entry_krw + BITHUMB_TICK_EXIT * krw_tick
                sl_krw = entry_krw - BITHUMB_TICK_EXIT * krw_tick
                # If going up N ticks crosses a price tier, re-snap TP to the new tier's tick.
                tp_tick = session._krw_tick(tp_krw)
                if tp_tick != krw_tick:
                    tp_krw = (tp_krw // tp_tick) * tp_tick
                tp_price = str(tp_krw / rate)
                tracked["tp_price"] = tp_price
                tracked["sl_price"] = str(sl_krw / rate)
                LOGGER.info(
                    "Bithumb tick-exit for %s: entry=₩%d  tick=₩%d  "
                    "tp=₩%d  sl=₩%d  (±%d KRW ticks)",
                    symbol, entry_krw, krw_tick, tp_krw, sl_krw, BITHUMB_TICK_EXIT,
                )
            except Exception as err:
                LOGGER.warning("tick-exit calc failed for %s, using stored tp_price: %s", symbol, err)
                tp_price = tracked.get("tp_price")
        else:
            tp_price = tracked.get("tp_price")
    else:
        tp_price = tracked.get("tp_price")
    # ──────────────────────────────────────────────────────────────────────────

    if not tp_price or not qty:
        LOGGER.warning("Cannot place Bithumb TP sell for %s: tp_price or qty missing", symbol)
        return
    try:
        resp = session.place_order(
            category="linear", symbol=symbol,
            side="Sell", orderType="Limit",
            price=tp_price, qty=qty,
            timeInForce="GTC",
        )
        tp_order_id = resp["result"]["orderId"]
        state["open_positions"][symbol]["tp_order_id"] = tp_order_id
        LOGGER.info("Bithumb TP limit sell placed for %s: price=%s qty=%s orderId=%s",
                    symbol, tp_price, qty, tp_order_id)
    except Exception as err:
        LOGGER.error("Failed to place Bithumb TP limit sell for %s: %s", symbol, err)


def open_position(session: HTTP, symbol: str, side: str, dry_run: bool,
                  leverage_override: int | None = None) -> dict | None:
    lev = leverage_override if leverage_override is not None else LEVERAGE
    inst = get_instrument(session, symbol)
    if not inst:
        LOGGER.warning("Instrument not found: %s", symbol)
        return None

    mark_price = get_mark_price(session, symbol)
    if mark_price is None:
        LOGGER.warning("No mark price for %s", symbol)
        return None

    lot   = inst.get("lotSizeFilter", {})
    price_filter = inst.get("priceFilter", {})
    qty_step  = lot.get("qtyStep", "1")
    min_qty   = float(lot.get("minOrderQty", "1"))
    min_notional = float(lot.get("minNotionalValue") or 5)
    tick_size = price_filter.get("tickSize", "0.01")

    qty_str = calc_qty(NOTIONAL_USDT * lev, mark_price, qty_step, min_qty)
    qty_f   = float(qty_str)
    actual_notional = qty_f * mark_price

    # Skip if the minimum order is too expensive relative to our intended margin
    target_notional = NOTIONAL_USDT * lev
    if actual_notional > target_notional * MAX_NOTIONAL_MULTIPLIER:
        LOGGER.warning(
            "Skipping %s: minimum order costs $%.2f (min_qty=%s × $%.2f) "
            "which exceeds %.0f× the $%.0f target notional",
            symbol, actual_notional, min_qty, mark_price,
            MAX_NOTIONAL_MULTIPLIER, target_notional,
        )
        return None
    if actual_notional < min_notional:
        LOGGER.warning("Skipping %s: notional $%.2f is below exchange minimum $%.2f",
                       symbol, actual_notional, min_notional)
        return None

    tp_price, sl_price = calc_tp_sl(side, mark_price, tick_size, lev)

    if dry_run:
        LOGGER.info("[DRY RUN] OPEN %s %s  qty=%s  entry≈%s  tp=%s  sl=%s",
                    side, symbol, qty_str, mark_price, tp_price, sl_price)
        return {"symbol": symbol, "side": side, "qty": qty_str,
                "entry_price": mark_price, "tp_price": tp_price, "sl_price": sl_price}

    # set leverage (idempotent)
    try:
        session.set_leverage(category="linear", symbol=symbol,
                             buyLeverage=str(lev), sellLeverage=str(lev))
    except Exception as err:
        LOGGER.debug("Leverage set skipped for %s: %s", symbol, err)

    resp = session.place_order(
        category="linear",
        symbol=symbol,
        side=side,
        orderType="Limit",
        price=str(_round_price(Decimal(str(mark_price)), Decimal(tick_size))),
        qty=qty_str,
        timeInForce=TIME_IN_FORCE,
        takeProfit=tp_price,
        stopLoss=sl_price,
        tpTriggerBy="MarkPrice",
        slTriggerBy="MarkPrice",
    )
    order_id = resp["result"]["orderId"]
    LOGGER.info("Placed %s %s qty=%s price=%s  tp=%s sl=%s (pending fill)  orderId=%s",
                side, symbol, qty_str,
                str(_round_price(Decimal(str(mark_price)), Decimal(tick_size))),
                tp_price, sl_price, order_id)
    return {"symbol": symbol, "side": side, "qty": qty_str,
            "entry_price": mark_price, "tp_price": tp_price, "sl_price": sl_price,
            "order_id": order_id}


# ── live position query ───────────────────────────────────────────────────────

def get_open_positions(session) -> dict[str, dict]:
    """Return {symbol: position_dict} for all positions with size > 0."""
    resp = session.get_positions(category="linear", settleCoin="USDT")
    return {
        item["symbol"]: item
        for item in resp["result"]["list"]
        if float(item.get("size") or 0) > 0
    }


def calc_open_notional(bybit_positions: dict[str, dict]) -> float:
    """Sum of initial margin (positionIM) across all open positions.
    positionIM is the actual USDT locked as margin per position, so this
    reflects real capital usage regardless of leverage.
    Falls back to positionValue / leverage if positionIM is absent.
    """
    total = 0.0
    for pos in bybit_positions.values():
        im = float(pos.get("positionIM") or 0)
        if im:
            total += im
        else:
            # fallback: contract notional ÷ leverage
            size  = float(pos.get("size") or 0)
            entry = float(pos.get("avgPrice") or 0)
            lev   = float(pos.get("leverage") or LEVERAGE or 1)
            total += (size * entry) / lev
    return total


# ── Telegram messages ─────────────────────────────────────────────────────────

def _sgn(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v:,.2f}"


def _pnl_emoji(v: float) -> str:
    return "🟢" if v >= 0 else "🔴"


def alert_open(alerter: TelegramAlerter | None, info: dict, dry_run: bool) -> None:
    if alerter is None:
        return
    side  = info["side"]
    label = "Long 🔺" if side == "Buy" else "Short 🔻"
    dry   = "  [DRY RUN]" if dry_run else ""
    alerter.send(
        f"🟢 <b>Position opened{dry}</b>\n"
        f"Strategy: <b>experiment_v1</b>\n"
        f"Symbol: <b>{info['symbol']}</b>  {label}\n"
        f"Entry ≈ ${info['entry_price']:,.4f}  Qty: {info['qty']}\n"
        f"TP: ${info['tp_price']}   SL: ${info['sl_price']}"
    )


def alert_close(alerter: TelegramAlerter | None, tracked: dict,
                exit_price: float, reason: str, dry_run: bool) -> None:
    if alerter is None:
        return
    side   = tracked["side"]
    entry  = float(tracked["entry_price"])
    qty_f  = float(tracked["qty"])
    if side == "Buy":
        pnl     = (exit_price - entry) * qty_f
        pnl_pct = (exit_price - entry) / entry * 100 if entry else 0.0
    else:
        pnl     = (entry - exit_price) * qty_f
        pnl_pct = (entry - exit_price) / entry * 100 if entry else 0.0
    label  = "Long 🔺" if side == "Buy" else "Short 🔻"
    dry    = "  [DRY RUN]" if dry_run else ""
    alerter.send(
        f"{_pnl_emoji(pnl)} <b>Position closed{dry}</b>  [{reason}]\n"
        f"Strategy: <b>experiment_v1</b>\n"
        f"Symbol: <b>{tracked['symbol']}</b>  {label}\n"
        f"Entry: ${entry:,.4f}   Exit: ${exit_price:,.4f}\n"
        f"PnL: {_sgn(pnl)} USDT  ({_sgn(pnl_pct)}%)"
    )


def alert_update(alerter: TelegramAlerter | None, bybit_positions: dict[str, dict],
                 session: HTTP | None = None, exchange: str = "bybit") -> None:
    if alerter is None:
        return
    now_kst = datetime.now().strftime("%Y-%m-%d %H:%M KST")
    is_krw = exchange in ("upbit", "bithumb")

    # For KRW exchanges fetch the rate so we can display native KRW values.
    krw_rate = 1.0
    if is_krw and session is not None:
        try:
            krw_rate = session._krw_usdt_rate()  # type: ignore[union-attr]
        except Exception:
            krw_rate = 1450.0

    def _fmt_price(usdt: float) -> str:
        if is_krw:
            return f"₩{usdt * krw_rate:,.0f}"
        return f"${usdt:,.4f}"

    def _fmt_pnl(usdt: float) -> str:
        if is_krw:
            return f"{_sgn(usdt * krw_rate)} KRW"
        return f"{_sgn(usdt)} USDT"

    currency_label = "KRW" if is_krw else "USDT"

    # fetch wallet balance
    balance_line = ""
    if session is not None:
        try:
            resp = session.get_wallet_balance(accountType="UNIFIED")
            acct = resp["result"]["list"][0]
            total_eq  = float(acct.get("totalEquity") or 0)
            avail_eq  = float(acct.get("totalAvailableBalance") or 0)
            upnl_total = float(acct.get("totalPerpUPL") or 0)
            if is_krw:
                balance_line = (
                    f"💰 Balance: <b>₩{total_eq * krw_rate:,.0f}</b>  "
                    f"Avail: ₩{avail_eq * krw_rate:,.0f}\n"
                )
            else:
                balance_line = (
                    f"💰 Balance: <b>${total_eq:,.2f}</b>  "
                    f"Avail: ${avail_eq:,.2f}  "
                    f"uPnL: {_sgn(upnl_total)} USDT\n"
                )
        except Exception as err:
            LOGGER.warning("Failed to fetch wallet balance for update: %s", err)

    if not bybit_positions:
        alerter.send(
            f"📊 <b>experiment_v1 · 30-min update</b>\n🕐 {now_kst}\n{balance_line}No open positions."
        )
        return
    lines = [f"📊 <b>experiment_v1 · 30-min update</b>", f"🕐 {now_kst}", balance_line.strip(), ""]
    for symbol, pos in bybit_positions.items():
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


# ── main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    load_params()  # pre-parse default; re-called with exchange after args are parsed
    parser = argparse.ArgumentParser(description="experiment_v1: volume-spike momentum")
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    parser.add_argument("--update", action="store_true",
                        help="Send a position update to Telegram immediately and exit")
    parser.add_argument("--cancel-all", action="store_true",
                        help="Cancel all open USDT-perp orders on Bybit and exit")
    parser.add_argument("--close-all", action="store_true",
                        help="Close all open USDT-perp positions at market and exit")
    parser.add_argument("--exchange", choices=["bybit", "upbit", "bithumb"], default="bybit",
                        help="Exchange to trade on: bybit (default), upbit, or bithumb (spot)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable DEBUG logging (shows 369 filter vetoes, skipped candles, etc.)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Always suppress noisy third-party loggers — urllib3 debug logs expose
    # API tokens in URLs and pybit/_http_manager is excessively verbose.
    for _noisy in ("urllib3", "pybit", "requests", "httpcore", "httpx"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    exchange = args.exchange
    global STATE_FILE
    STATE_FILE = Path(f"data/experiment_v1_{exchange}_state.json")
    load_params(exchange)  # reload with exchange-specific overrides now that exchange is known

    settings = load_settings()
    if exchange == "upbit":
        if not settings.upbit_credentials:
            raise SystemExit("Upbit credentials not found. Check UPBIT_CREDENTIALS_FILE in .env.")
        session: HTTP | UpbitSession | BithumbSession = UpbitSession(
            access_key=settings.upbit_credentials.api_key,
            secret_key=settings.upbit_credentials.api_secret,
        )
    elif exchange == "bithumb":
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

    # trade alerts (open/close) → @nasang_bot
    alerter  = TelegramAlerter.from_env(token=settings.telegram_bot_token,
                                        chat_id=settings.telegram_chat_id)

    # position update alerts (30-min summary) → @position_update_srabot
    _pos_token_path = Path(os.getenv("POSITION_BOT_CREDENTIALS_FILE",
                                     "design/_cred_position_bot_token"))
    position_bot_token = _pos_token_path.read_text(encoding="utf-8").strip() \
        if _pos_token_path.exists() else None
    position_bot_chat_id = os.getenv("POSITION_BOT_CHAT_ID") or None
    position_alerter = TelegramAlerter.from_env(token=position_bot_token,
                                                chat_id=position_bot_chat_id)

    if args.update:
        bybit_positions = get_open_positions(session)
        alert_update(position_alerter, bybit_positions, session, exchange)
        LOGGER.info("Position update sent.")
        return

    if args.cancel_all:
        resp = session.cancel_all_orders(category="linear", settleCoin="USDT")
        cancelled = resp.get("result", {}).get("list", [])
        if cancelled:
            for o in cancelled:
                LOGGER.info("Cancelled order %s  symbol=%s  side=%s  qty=%s",
                            o.get("orderId"), o.get("symbol"),
                            o.get("side"), o.get("qty"))
            LOGGER.info("Total cancelled: %d order(s)", len(cancelled))
        else:
            LOGGER.info("No open orders to cancel.")
        # also clear pending_orders from local state
        state = load_state()
        state["pending_orders"] = {}
        save_state(state)
        return

    if args.close_all:
        positions = get_open_positions(session)
        if not positions:
            LOGGER.info("No open positions to close.")
        else:
            # cancel open orders first so they don't re-open
            session.cancel_all_orders(category="linear", settleCoin="USDT")
            for sym, pos in positions.items():
                close_side = "Sell" if pos["side"] == "Buy" else "Buy"
                qty = pos["size"]
                try:
                    session.place_order(
                        category="linear",
                        symbol=sym,
                        side=close_side,
                        orderType="Market",
                        qty=qty,
                        reduceOnly=True,
                        timeInForce="IOC",
                    )
                    LOGGER.info("Closed %s  side=%s  qty=%s", sym, close_side, qty)
                except Exception as err:
                    LOGGER.error("Failed to close %s: %s", sym, err)
            LOGGER.info("Close-all done: %d position(s) submitted.", len(positions))
        # wipe local state
        state = load_state()
        state["open_positions"] = {}
        state["pending_orders"] = {}
        save_state(state)
        return

    symbols = load_symbols(exchange)
    LOGGER.info("experiment_v1 started | exchange=%s | %d symbols | dry_run=%s",
                exchange, len(symbols), settings.dry_run)

    state = load_state()
    # Always schedule the first update to fire on the next tick, so a restart
    # never silently resets the 30-min clock.
    last_update_at = datetime.now(tz=UTC) - timedelta(seconds=UPDATE_INTERVAL)

    while True:
        now = datetime.now(tz=UTC)
        load_params(exchange)  # hot-reload params.json on every tick (exchange-aware)

        # ── 1. Fetch all open Bybit positions ─────────────────────────────────
        try:
            bybit_positions = get_open_positions(session)
        except Exception as err:
            LOGGER.error("Failed to fetch positions: %s", err)
            if args.once:
                break
            time.sleep(CHECK_INTERVAL)
            continue
        # ── 1b. Cancel pending orders unfilled for > 60 s ───────────────────────
        state.setdefault("pending_orders", {})
        for sym, pending in list(state["pending_orders"].items()):
            placed_at = datetime.fromisoformat(pending["placed_at"]).replace(tzinfo=UTC)
            if (now - placed_at).total_seconds() < 60:
                continue
            # still unfilled after 1 min — cancel it
            try:
                session.cancel_order(category="linear", symbol=sym,
                                     orderId=pending["order_id"])
                LOGGER.info("Cancelled unfilled order %s for %s (placed %s)",
                            pending["order_id"], sym, pending["placed_at"])
            except Exception as err:
                err_str = str(err)
                if "110001" in err_str:
                    # order already filled before cancel — set TP/SL now and keep position
                    LOGGER.info("Order %s for %s already filled (cancel too late)",
                                pending["order_id"], sym)
                    if sym in bybit_positions:
                        if exchange == "bithumb":
                            _place_bithumb_tp_order(session, sym, bybit_positions[sym], state)
                        else:
                            lev = 1 if exchange == "upbit" else None
                            result = _apply_tp_sl(session, sym, bybit_positions[sym]["side"], state, lev)
                            if result:
                                LOGGER.info("TP/SL set for %s after late-fill: tp=%s sl=%s",
                                            sym, *result)
                    del state["pending_orders"][sym]
                    continue
                LOGGER.warning("Cancel failed for %s order %s: %s",
                               sym, pending["order_id"], err)
            del state["pending_orders"][sym]
            state["open_positions"].pop(sym, None)  # unblock the symbol

        # clear pending orders that have already been filled and set their TP/SL
        for sym in list(state["pending_orders"].keys()):
            if sym not in bybit_positions:
                continue
            pending = state["pending_orders"][sym]  # noqa: F841 (kept for future use)
            LOGGER.info("Order filled: %s — placing TP/SL", sym)
            if exchange == "bithumb":
                _place_bithumb_tp_order(session, sym, bybit_positions[sym], state)
            else:
                lev = 1 if exchange == "upbit" else None
                result = _apply_tp_sl(session, sym, bybit_positions[sym]["side"], state, lev)
                if result:
                    LOGGER.info("TP/SL set for %s: tp=%s sl=%s", sym, *result)
            del state["pending_orders"][sym]

        # ── 2. Detect closes: positions we tracked but Bybit closed (TP/SL) ──
        for symbol, tracked in list(state["open_positions"].items()):
            if symbol in bybit_positions:
                continue
            if symbol in state.get("pending_orders", {}):
                continue  # order still pending — not filled yet, not a close
            exit_price = get_mark_price(session, tracked["symbol"]) or float(tracked.get("entry_price", 0))
            LOGGER.info("Detected close: %s %s  exit_price=%s",
                        tracked["side"], symbol, exit_price)
            alert_close(alerter, tracked, exit_price, "TP/SL hit", settings.dry_run)
            del state["open_positions"][symbol]

        # ── 2b. Ensure every live position has TP/SL set ───────────────────────────
        # Repairs positions that were opened while the bot was down, or where
        # set_trading_stop was skipped (e.g. 110001 race or restart).
        # Each symbol is wrapped individually so a failure for one symbol does NOT
        # abort the repair of the remaining symbols.
        configured_symbols = {s for s, _ in symbols}
        for symbol, pos in bybit_positions.items():
            if exchange in ("upbit", "bithumb") and symbol not in configured_symbols:
                continue  # ignore stale holdings that aren't in our watchlist
            try:
                tracked = state["open_positions"].get(symbol, {})
                tp_price = tracked.get("tp_price")
                sl_price = tracked.get("sl_price")
                if exchange in ("upbit", "bithumb"):
                    if tp_price and sl_price:
                        continue  # spot: stored prices are the thresholds, nothing to repair
                else:
                    has_tp = bool(pos.get("takeProfit") and float(pos["takeProfit"]) != 0)
                    has_sl = bool(pos.get("stopLoss")   and float(pos["stopLoss"])   != 0)
                    if has_tp and has_sl:
                        continue  # already armed on Bybit
                if not tp_price or not sl_price:
                    # No stored prices — recalculate from current entry price
                    entry = float(pos.get("avgPrice") or 0)
                    if entry:
                        inst = get_instrument(session, symbol)
                        tick = inst.get("priceFilter", {}).get("tickSize", "0.01") if inst else "0.01"
                        side = pos["side"]
                        pos_lev = 1 if exchange in ("upbit", "bithumb") else int(float(pos.get("leverage") or LEVERAGE))
                        tp_price, sl_price = calc_tp_sl(side, entry, tick, pos_lev)
                        LOGGER.info("Recalculated TP/SL for %s: tp=%s sl=%s", symbol, tp_price, sl_price)
                        if symbol not in state["open_positions"]:
                            if exchange in ("upbit", "bithumb"):
                                # Spot: this is a pre-existing holding the bot didn't open.
                                # Don't auto-track it — it would block signals and inflate the cap.
                                continue
                            # Bybit: position exists after restart; track for close-detection.
                            state["open_positions"][symbol] = {
                                "symbol": symbol,
                                "side": pos["side"],
                                "entry_price": entry,
                                "qty": pos.get("size", "0"),
                                "tp_price": tp_price,
                                "sl_price": sl_price,
                                "open_time": now.isoformat(),
                            }
                            LOGGER.info("Tracking previously unrecorded position: %s", symbol)
                        else:
                            state["open_positions"][symbol]["tp_price"] = tp_price
                            state["open_positions"][symbol]["sl_price"] = sl_price
                if exchange not in ("upbit", "bithumb"):
                    pos_lev = int(float(pos.get("leverage") or LEVERAGE))
                    result = _apply_tp_sl(session, symbol, pos["side"], state, pos_lev)
                    if result:
                        LOGGER.info("TP/SL applied to %s: tp=%s sl=%s", symbol, *result)
            except Exception as repair_err:
                LOGGER.warning("Repair loop error for %s (skipping): %s", symbol, repair_err)

        # ── 2c. Spot (Upbit / Bithumb): manual TP/SL enforcement ────────────────────
        if exchange in ("upbit", "bithumb"):
            for symbol, tracked in list(state["open_positions"].items()):
                if symbol not in bybit_positions:
                    continue
                current_price = get_mark_price(session, symbol)
                if current_price is None:
                    continue
                tp = float(tracked.get("tp_price") or 0)
                sl = float(tracked.get("sl_price") or 0)
                close_reason = None

                # Bithumb: a GTC limit sell at TP price was placed immediately after buy fill.
                # Check if it has already executed (= TP hit) before doing anything else.
                tp_order_id = tracked.get("tp_order_id") if exchange == "bithumb" else None
                if tp_order_id:
                    try:
                        order = session.get_order(orderId=tp_order_id)
                        if order.get("state") == "done":
                            exec_price = float(order.get("price") or tp)
                            LOGGER.info("TP sell order filled for %s: price=%s", symbol, exec_price)
                            alert_close(alerter, tracked, exec_price, "TP hit", settings.dry_run)
                            del state["open_positions"][symbol]
                            state["pending_orders"].pop(symbol, None)
                            continue
                    except Exception as err:
                        LOGGER.warning("get_order failed for %s tp_order %s: %s",
                                       symbol, tp_order_id, err)

                # SL check: cancel the outstanding TP limit order then market-sell.
                if sl and 0 < current_price <= sl:
                    close_reason = "SL hit"
                    if tp_order_id and not settings.dry_run:
                        try:
                            session.cancel_order(category="linear", symbol=symbol,
                                                 orderId=tp_order_id)
                            LOGGER.info("Cancelled TP order %s for %s (SL triggered)",
                                        tp_order_id, symbol)
                        except Exception as err:
                            LOGGER.warning("Could not cancel TP order for %s: %s", symbol, err)
                elif not tp_order_id and tp and current_price >= tp:
                    # Upbit (no TP limit order): price-based TP check
                    close_reason = "TP hit"

                if close_reason:
                    qty = bybit_positions[symbol].get("size") or tracked.get("qty")
                    try:
                        if not settings.dry_run:
                            session.place_order(
                                category="linear", symbol=symbol,
                                side="Sell", orderType="Market",
                                qty=qty, timeInForce="IOC",
                            )
                        LOGGER.info("Spot %s triggered: %s price=%s",
                                    close_reason, symbol, current_price)
                        alert_close(alerter, tracked, current_price, close_reason, settings.dry_run)
                    except Exception as err:
                        LOGGER.error("Spot close failed for %s (%s): %s",
                                     symbol, close_reason, err)
                        continue
                    del state["open_positions"][symbol]
                    state["pending_orders"].pop(symbol, None)

        # ── 3. Signal scan — only for symbols with no open position ───────────
        # For spot: only count bot-opened positions towards the cap. Pre-existing
        # account holdings must not inflate the cap and block all new signals.
        if exchange in ("upbit", "bithumb"):
            cap_positions = {s: p for s, p in bybit_positions.items() if s in state["open_positions"]}
        else:
            cap_positions = bybit_positions
        total_notional = calc_open_notional(cap_positions)
        if total_notional >= NOTIONAL_CAP_USDT:
            LOGGER.info(
                "Global cap reached: $%.2f open >= $%.0f threshold. Skipping signal scan.",
                total_notional, NOTIONAL_CAP_USDT,
            )
        else:
            LOGGER.debug("Total open notional $%.2f / $%.0f cap", total_notional, NOTIONAL_CAP_USDT)
            # Pre-compute the last-completed 5-min candle timestamp so we can
            # skip the kline API call for symbols whose candle hasn't changed.
            expected_candle_ts = str((int(now.timestamp() // 300) - 1) * 300 * 1000)

            for symbol, _lev_override in symbols:
                lev_override = 1 if exchange in ("upbit", "bithumb") else _lev_override
                is_spot = exchange in ("upbit", "bithumb")
                has_pending = symbol in state.get("pending_orders", {})
                has_position = symbol in state["open_positions"] or symbol in bybit_positions

                # Never act while an order is still in-flight
                if has_pending:
                    continue
                # Bybit: position already managed by native TP/SL on the exchange
                if has_position and not is_spot:
                    continue

                # Skip kline fetch entirely if this candle was already processed
                if expected_candle_ts == state["processed_candles"].get(symbol):
                    continue
                try:
                    side, candle_ts = check_signal(session, symbol)
                except Exception as err:
                    LOGGER.warning("Signal check failed %s: %s", symbol, err)
                    continue

                # deduplicate: same candle already processed (safety net)
                if candle_ts and candle_ts == state["processed_candles"].get(symbol):
                    continue
                if candle_ts:
                    state["processed_candles"][symbol] = candle_ts

                if side is None:
                    continue

                # ── Spot: bearish signal while holding → exit by signal ────────
                if is_spot and has_position and side == "Sell":
                    if symbol in bybit_positions:
                        tracked = state["open_positions"].get(symbol, {})
                        qty = bybit_positions[symbol].get("size") or tracked.get("qty")
                        current_price = get_mark_price(session, symbol) or float(tracked.get("entry_price", 0))
                        try:
                            if not settings.dry_run:
                                session.place_order(
                                    category="linear", symbol=symbol,
                                    side="Sell", orderType="Market",
                                    qty=qty, timeInForce="IOC",
                                )
                            LOGGER.info("Signal exit triggered: %s  price=%s", symbol, current_price)
                            alert_close(alerter, tracked, current_price, "Signal exit", settings.dry_run)
                        except Exception as err:
                            LOGGER.error("Signal exit failed for %s: %s", symbol, err)
                            continue
                        state["open_positions"].pop(symbol, None)
                        state["pending_orders"].pop(symbol, None)
                    continue

                # ── Spot: no open position but Sell signal → skip (no shorts) ──
                if is_spot and side == "Sell":
                    continue

                # ── Already holding on spot and Buy signal → skip ──────────────
                if has_position:
                    continue

                # Re-check cap live before each open (previous iteration may have opened)
                live_positions = get_open_positions(session)
                if calc_open_notional(live_positions) >= NOTIONAL_CAP_USDT:
                    LOGGER.info("Global cap reached mid-scan. Stopping signal scan.")
                    bybit_positions = live_positions
                    break

                LOGGER.info("Signal → %s %s (candle %s)", side, symbol, candle_ts)
                result = open_position(session, symbol, side, settings.dry_run, lev_override)
                if result:
                    state["open_positions"][symbol] = {
                        "symbol": symbol,
                        "side": side,
                        "entry_price": result["entry_price"],
                        "qty": result["qty"],
                        "tp_price": result["tp_price"],
                        "sl_price": result["sl_price"],
                        "open_time": now.isoformat(),
                    }
                    # track for fill-or-cancel (order_id absent in dry_run)
                    order_id = result.get("order_id")
                    if order_id:
                        state["pending_orders"][symbol] = {
                            "order_id": order_id,
                            "placed_at": now.isoformat(),
                        }
                    alert_open(alerter, result, settings.dry_run)
                    bybit_positions = get_open_positions(session)

        # ── 4. 30-min update alert ─────────────────────────────────────────────
        if (now - last_update_at).total_seconds() >= UPDATE_INTERVAL:
            alert_update(position_alerter, bybit_positions, session, exchange)
            last_update_at = now
            state["last_update_alert"] = now.isoformat()

        save_state(state)

        if args.once:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
