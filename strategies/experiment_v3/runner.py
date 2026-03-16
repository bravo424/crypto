"""
experiment_v3 — 1-minute ATR trailing-queue scalper on Bybit USDT-perp.

Design philosophy
-----------------
experiment_v1's main flaw: it posts a limit order at exactly the mark price
right after a momentum spike.  The price has already moved; PostOnly either
gets rejected or fills at the worst tick of the move.

experiment_v3 solves this by NEVER chasing the spike.  Instead it queues a
limit order a fixed distance BELOW the mark (for Buys) and waits for the
micro-pullback that follows almost every momentum candle.  Every tick the bot
checks if the order is "stale" (price ran away without filling) and transparently
cancel-and-refreshes it at the new level — so we're always hunting a good entry
without ever paying taker fees.

Signal (all must hold on the last closed 1-min candle):
  1. 369 EMA alignment: close > EMA3 > EMA6 > EMA9 → Long
                        close < EMA3 < EMA6 < EMA9 → Short
  2. Volume spike: candle volume ≥ VMULT × rolling average over VLOOKBACK bars.

Entry (guaranteed PostOnly / maker):
  Buy:  limit = mark − TRAIL_OFFSET_ATR × ATR   (queue below market)
  Sell: limit = mark + TRAIL_OFFSET_ATR × ATR   (queue above market)

  Every CHECK_INTERVAL the order is refreshed if:
    |mark − limit_price| > STALE_MULT × trail_offset
  After MAX_ORDER_AGE_MIN minutes without fill the signal is abandoned.

Exit (native Bybit TP/SL, ATR-based):
  TP = entry ± TP_ATR_MULT × ATR      SL = entry ∓ SL_ATR_MULT × ATR
  Default 2:1 R:R (1.6 / 0.8).  ATR adapts automatically to volatility.

Frequency guard:
  NORDERSPERHOUR per symbol (rolling 1-hr window) + MAX_OPEN_POSITIONS global.
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

from dotenv import load_dotenv
from pybit.exceptions import InvalidRequestError
from pybit.unified_trading import HTTP

from upbit_bybit_bot.config import load_settings
from upbit_bybit_bot.telegram_alerter import TelegramAlerter

LOGGER = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
if str(HERE.parent / "experiment_v1") not in sys.path:
    sys.path.insert(0, str(HERE.parent / "experiment_v1"))

STATE_FILE      = Path("data/experiment_v3_bybit_state.json")
CHECK_INTERVAL  = 15       # seconds — fast tick so stale refresh works well on 1m candles
UPDATE_INTERVAL = 1800     # 30-min Telegram position summary

# ── mutable globals (hot-reloaded from params.json every tick) ────────────────
VMULT:             float = 1.5
VLOOKBACK:         int   = 10
ATR_PERIOD:        int   = 14
TRAIL_OFFSET_ATR:  float = 0.2    # limit = mark ± TRAIL_OFFSET_ATR × ATR from mark
TP_ATR_MULT:       float = 1.6    # TP distance = TP_ATR_MULT × ATR
SL_ATR_MULT:       float = 0.8    # SL distance = SL_ATR_MULT × ATR  → 2:1 R:R
STALE_MULT:        float = 3.0    # refresh if |mark − limit| > STALE_MULT × trail_offset
MAX_ORDER_AGE_MIN: int   = 5      # abandon signal after N minutes without fill
MAXRISKPCT:        float = 0.01   # 1% equity risk per trade
NOTIONAL_CAP_USDT: float = 300.0  # max total open margin
NORDERSPERHOUR:    int   = 3      # max new entries per symbol per rolling hour
MAX_OPEN_POSITIONS: int  = 3      # max concurrent open positions across all symbols
LEVERAGE:          int   = 10
TIME_IN_FORCE:     str   = "PostOnly"
HTF_INTERVAL:      str   = "15"     # higher-timeframe candles for trend filter
HTF_EMA_PERIOD:    int   = 20       # EMA period on HTF
MIN_ATR_PCT:       float = 0.002    # skip symbol if ATR / mark < this (0.2%)


def load_params() -> None:
    global VMULT, VLOOKBACK, ATR_PERIOD, TRAIL_OFFSET_ATR, TP_ATR_MULT
    global SL_ATR_MULT, STALE_MULT, MAX_ORDER_AGE_MIN, MAXRISKPCT
    global NOTIONAL_CAP_USDT, NORDERSPERHOUR, MAX_OPEN_POSITIONS, LEVERAGE, TIME_IN_FORCE
    global HTF_EMA_PERIOD, MIN_ATR_PCT
    path = HERE / "params.json"
    with path.open(encoding="utf-8") as fh:
        p = json.load(fh)
    VMULT              = float(p.get("vmult",              VMULT))
    VLOOKBACK          = int(p.get("vlookback",            VLOOKBACK))
    ATR_PERIOD         = int(p.get("atr_period",           ATR_PERIOD))
    TRAIL_OFFSET_ATR   = float(p.get("trail_offset_atr",   TRAIL_OFFSET_ATR))
    TP_ATR_MULT        = float(p.get("tp_atr_mult",        TP_ATR_MULT))
    SL_ATR_MULT        = float(p.get("sl_atr_mult",        SL_ATR_MULT))
    STALE_MULT         = float(p.get("stale_mult",         STALE_MULT))
    MAX_ORDER_AGE_MIN  = int(p.get("max_order_age_min",    MAX_ORDER_AGE_MIN))
    MAXRISKPCT         = float(p.get("maxriskpct",         MAXRISKPCT))
    NOTIONAL_CAP_USDT  = float(p.get("notional_cap_usdt",  NOTIONAL_CAP_USDT))
    NORDERSPERHOUR     = int(p.get("nordersperhour",       NORDERSPERHOUR))
    MAX_OPEN_POSITIONS = int(p.get("max_open_positions",   MAX_OPEN_POSITIONS))
    LEVERAGE           = int(p.get("leverage",             LEVERAGE))
    TIME_IN_FORCE      = str(p.get("time_in_force",        TIME_IN_FORCE))
    HTF_EMA_PERIOD     = int(p.get("htf_ema_period",       HTF_EMA_PERIOD))
    MIN_ATR_PCT        = float(p.get("min_atr_pct",        MIN_ATR_PCT))


# ── symbols ───────────────────────────────────────────────────────────────────

def load_symbols() -> list[tuple[str, int | None]]:
    path = HERE / "symbols_bybit.json"
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
        "open_positions":   {},
        "pending_orders":   {},   # symbol → {order_id, placed_at, signal_at, limit_price, side, atr}
        "signal_times":     {},   # symbol → [ISO timestamps] for nordersperhour guard
        "processed_candles": {},
        "last_update_alert": None,
    }


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ── indicator maths ───────────────────────────────────────────────────────────

def _ema(values: list[float], period: int) -> float:
    if len(values) < period:
        return float("nan")
    k   = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _atr(highs: list[float], lows: list[float], closes: list[float],
         period: int) -> float:
    """Wilder-smoothed ATR.  Needs ≥ period+1 bars."""
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


def check_369(closes: list[float]) -> str | None:
    """Bull stack: close > EMA3 > EMA6 > EMA9.  Bear stack: reverse."""
    if len(closes) < 9:
        return None
    ema3   = _ema(closes, 3)
    ema6   = _ema(closes, 6)
    ema9   = _ema(closes, 9)
    price  = closes[-1]
    if price > ema3 > ema6 > ema9:
        return "Buy"
    if price < ema3 < ema6 < ema9:
        return "Sell"
    return None


def check_htf_trend(session: HTTP, symbol: str) -> str | None:
    """15-min EMA trend gate: returns 'Buy', 'Sell', or None (neutral)."""
    resp = session.get_kline(category="linear", symbol=symbol,
                             interval=HTF_INTERVAL, limit=HTF_EMA_PERIOD + 5)
    candles = resp["result"]["list"]
    if len(candles) < HTF_EMA_PERIOD + 1:
        return None
    closes = [float(c[4]) for c in reversed(candles[1:])]
    ema    = _ema(closes, HTF_EMA_PERIOD)
    if closes[-1] > ema:
        return "Buy"
    if closes[-1] < ema:
        return "Sell"
    return None


# ── signal ────────────────────────────────────────────────────────────────────

def check_signal(session: HTTP, symbol: str) -> tuple[str | None, str, float]:
    """Return (side_or_None, candle_ts_ms_str, atr) for last closed 1-min candle."""
    limit = max(30, ATR_PERIOD + VLOOKBACK + 5)
    resp  = session.get_kline(category="linear", symbol=symbol,
                              interval="1", limit=limit)
    candles = resp["result"]["list"]
    if len(candles) < ATR_PERIOD + VLOOKBACK + 2:
        return None, "", float("nan")

    # Bybit: newest first; [0] still forming → skip
    closed = list(reversed(candles[1:]))
    highs   = [float(c[2]) for c in closed]
    lows    = [float(c[3]) for c in closed]
    closes  = [float(c[4]) for c in closed]
    volumes = [float(c[5]) for c in closed]
    candle_ts = str(candles[1][0])

    # ── volume spike ──────────────────────────────────────────────────────────
    avg_vol = sum(volumes[-(VLOOKBACK + 1):-1]) / VLOOKBACK if VLOOKBACK > 0 else 1.0
    if avg_vol <= 0 or volumes[-1] < VMULT * avg_vol:
        return None, candle_ts, float("nan")

    # ── 369 EMA alignment ─────────────────────────────────────────────────────
    side = check_369(closes)
    if side is None:
        return None, candle_ts, float("nan")

    # ── ATR ───────────────────────────────────────────────────────────────────
    atr = _atr(highs, lows, closes, ATR_PERIOD)
    if atr != atr or atr <= 0:   # nan / zero guard
        return None, candle_ts, float("nan")

    return side, candle_ts, atr


# ── instrument / price helpers ────────────────────────────────────────────────

def get_instrument(session: HTTP, symbol: str) -> dict | None:
    try:
        resp = session.get_instruments_info(category="linear", symbol=symbol)
    except InvalidRequestError:
        return None
    items = resp.get("result", {}).get("list", [])
    return items[0] if items else None


def get_mark_price(session: HTTP, symbol: str) -> float | None:
    resp  = session.get_tickers(category="linear", symbol=symbol)
    items = resp.get("result", {}).get("list", [])
    if not items:
        return None
    return float(items[0].get("markPrice") or 0) or None


def get_wallet_equity(session: HTTP) -> float:
    resp = session.get_wallet_balance(accountType="UNIFIED")
    return float(resp["result"]["list"][0].get("totalEquity") or 0)


def get_open_positions(session: HTTP) -> dict[str, dict]:
    resp = session.get_positions(category="linear", settleCoin="USDT")
    return {
        item["symbol"]: item
        for item in resp["result"]["list"]
        if float(item.get("size") or 0) > 0
    }


# ── price rounding ────────────────────────────────────────────────────────────

def _round_down(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_DOWN) * tick


def _round_qty(qty: Decimal, step: Decimal) -> Decimal:
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def _trail_price(side: str, mark: float, atr: float, tick_size: str) -> Decimal:
    """Limit price offset from mark by TRAIL_OFFSET_ATR × ATR."""
    p      = Decimal(str(mark))
    tick   = Decimal(tick_size)
    offset = Decimal(str(TRAIL_OFFSET_ATR)) * Decimal(str(atr))
    if side == "Buy":
        return _round_down(p - offset, tick)
    # Sell: post above market
    off_up = (p + offset) / tick
    from decimal import ROUND_UP
    return off_up.to_integral_value(rounding=ROUND_UP) * tick


def _trail_offset_amount(atr: float) -> float:
    """Raw offset in price units for staleness checks."""
    return TRAIL_OFFSET_ATR * atr


def _is_stale(pending: dict, mark: float) -> bool:
    """True if mark has drifted more than STALE_MULT × trail_offset from the limit."""
    limit_price  = float(pending.get("limit_price", mark))
    atr          = float(pending.get("atr", 0))
    if atr <= 0:
        return False
    offset       = _trail_offset_amount(atr)
    return abs(mark - limit_price) > STALE_MULT * offset


# ── entry size ────────────────────────────────────────────────────────────────

def calc_qty(equity: float, sl_distance: float, qty_step: str,
             min_qty: float, entry: float) -> str:
    """Size = (MAXRISKPCT × equity) / sl_distance, capped at NOTIONAL_CAP_USDT."""
    if sl_distance <= 0:
        return "0"
    raw_notional = min(MAXRISKPCT * equity / sl_distance * entry, NOTIONAL_CAP_USDT)
    step    = Decimal(qty_step)
    qty     = _round_qty(Decimal(str(raw_notional)) / Decimal(str(entry)), step)
    min_d   = Decimal(str(min_qty))
    if qty < min_d:
        qty = min_d
    return format(qty.normalize(), "f")


# ── tp/sl levels ─────────────────────────────────────────────────────────────

def calc_tp_sl(side: str, entry: float, atr: float,
               tick_size: str) -> tuple[str, str]:
    tick  = Decimal(tick_size)
    e     = Decimal(str(entry))
    dist  = Decimal(str(atr))
    tp_d  = dist * Decimal(str(TP_ATR_MULT))
    sl_d  = dist * Decimal(str(SL_ATR_MULT))
    if side == "Buy":
        tp = _round_down(e + tp_d, tick)
        sl = _round_down(e - sl_d, tick)
    else:
        tp = _round_down(e - tp_d, tick)
        sl = _round_down(e + sl_d, tick)
    return format(tp.normalize(), "f"), format(sl.normalize(), "f")


# ── frequency guard ───────────────────────────────────────────────────────────

def _freq_ok(state: dict, symbol: str, now: datetime,
             live_positions: dict) -> bool:
    window_start = now - timedelta(hours=1)
    times        = state["signal_times"].get(symbol, [])
    recent       = [t for t in times
                    if datetime.fromisoformat(t).replace(tzinfo=UTC) >= window_start]
    state["signal_times"][symbol] = recent
    if len(recent) >= NORDERSPERHOUR:
        return False
    if MAX_OPEN_POSITIONS > 0:
        total = len(live_positions) + len(state.get("pending_orders", {}))
        if total >= MAX_OPEN_POSITIONS:
            return False
    return True


def _record_signal(state: dict, symbol: str, now: datetime) -> None:
    state["signal_times"].setdefault(symbol, []).append(now.isoformat())


# ── clock sync ────────────────────────────────────────────────────────────────

def _sync_pybit_clock(testnet: bool = False) -> None:
    import pybit._helpers as _h
    tmp       = HTTP(testnet=testnet)
    resp      = tmp.get_server_time()
    server_ms = int(resp["result"]["timeNano"]) // 1_000_000
    local_ms  = _h.generate_timestamp()
    offset_ms = server_ms - local_ms
    LOGGER.info("Clock sync: offset=%+dms", offset_ms)
    _orig = _h.generate_timestamp
    _h.generate_timestamp = lambda: _orig() + offset_ms


# ── place / refresh entry order ───────────────────────────────────────────────

def _place_entry(session: HTTP, symbol: str, side: str, atr: float,
                 dry_run: bool, lev_override: int | None, now: datetime,
                 signal_at_iso: str | None = None) -> dict | None:
    """Place (or replace) a PostOnly trailing limit entry.

    Returns a pending_orders dict entry, or None on failure.
    """
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

    sl_dist = SL_ATR_MULT * atr
    if sl_dist <= 0:
        return None
    if MIN_ATR_PCT > 0 and atr / mark < MIN_ATR_PCT:
        LOGGER.warning("Skipping %s: ATR %.4f%% < minimum %.4f%%",
                       symbol, atr / mark * 100, MIN_ATR_PCT * 100)
        return None

    try:
        equity = get_wallet_equity(session)
    except Exception as err:
        LOGGER.warning("Could not fetch equity for %s: %s", symbol, err)
        return None

    limit_px = _trail_price(side, mark, atr, tick_size)
    # TP/SL must be relative to mark price — Bybit validates them against mark at
    # submission time, not against the (future) fill price of the limit order.
    tp_price, sl_price = calc_tp_sl(side, mark, atr, tick_size)
    qty_str  = calc_qty(equity, sl_dist, qty_step, min_qty, float(limit_px))

    if float(qty_str) * float(limit_px) < min_not:
        LOGGER.warning("Skipping %s: notional below exchange minimum", symbol)
        return None

    signal_at = signal_at_iso or now.isoformat()

    if dry_run:
        LOGGER.info("[DRY RUN] QUEUE %s %s  limit=%s  tp=%s  sl=%s  atr=%.4f",
                    side, symbol, limit_px, tp_price, sl_price, atr)
        return {
            "order_id":   "dry",
            "placed_at":  now.isoformat(),
            "signal_at":  signal_at,
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

    resp     = session.place_order(
        category="linear", symbol=symbol,
        side=side, orderType="Limit",
        price=str(limit_px), qty=qty_str,
        timeInForce=TIME_IN_FORCE,
        takeProfit=tp_price, stopLoss=sl_price,
        tpTriggerBy="MarkPrice", slTriggerBy="MarkPrice",
    )
    order_id = resp["result"]["orderId"]
    LOGGER.info("Queued %s %s  limit=%s  tp=%s  sl=%s  atr=%.4f  orderId=%s",
                side, symbol, limit_px, tp_price, sl_price, atr, order_id)
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


# ── Telegram ──────────────────────────────────────────────────────────────────

def _sgn(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v:,.2f}"


def _pnl_emoji(v: float) -> str:
    return "🟢" if v >= 0 else "🔴"


def alert_open(alerter, info: dict, dry_run: bool) -> None:
    if alerter is None:
        return
    label = "Long 🔺" if info["side"] == "Buy" else "Short 🔻"
    dry   = "  [DRY RUN]" if dry_run else ""
    alerter.send(
        f"🟢 <b>Position opened{dry}</b>\n"
        f"Strategy: <b>experiment_v3</b>\n"
        f"Symbol: <b>{info['symbol']}</b>  {label}\n"
        f"Entry ≈ ${float(info['entry_price']):,.4f}  Qty: {info['qty']}\n"
        f"TP: ${info['tp_price']}   SL: ${info['sl_price']}"
    )


def alert_close(alerter, tracked: dict, exit_price: float,
                reason: str, dry_run: bool) -> None:
    if alerter is None:
        return
    side    = tracked["side"]
    entry   = float(tracked["entry_price"])
    qty_f   = float(tracked["qty"])
    pnl     = (exit_price - entry) * qty_f if side == "Buy" else (entry - exit_price) * qty_f
    pnl_pct = pnl / (entry * qty_f) * 100 if entry and qty_f else 0.0
    label   = "Long 🔺" if side == "Buy" else "Short 🔻"
    dry     = "  [DRY RUN]" if dry_run else ""
    alerter.send(
        f"{_pnl_emoji(pnl)} <b>Position closed{dry}</b>  [{reason}]\n"
        f"Strategy: <b>experiment_v3</b>\n"
        f"Symbol: <b>{tracked['symbol']}</b>  {label}\n"
        f"Entry: ${entry:,.4f}   Exit: ${exit_price:,.4f}\n"
        f"PnL: {_sgn(pnl)} USDT  ({_sgn(pnl_pct)}%)"
    )


def alert_update(alerter, positions: dict, session=None) -> None:
    if alerter is None:
        return
    now_kst = datetime.now().strftime("%Y-%m-%d %H:%M KST")
    balance_line = ""
    if session:
        for _ in range(2):
            try:
                acct  = session.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]
                total = float(acct.get("totalEquity") or 0)
                avail = float(acct.get("totalAvailableBalance") or 0)
                upnl  = float(acct.get("totalPerpUPL") or 0)
                balance_line = (f"💰 Balance: <b>${total:,.2f}</b>  "
                                f"Avail: ${avail:,.2f}  uPnL: {_sgn(upnl)} USDT\n")
                break
            except Exception:
                time.sleep(2)
    if not positions:
        alerter.send(f"📊 <b>experiment_v3 · 30-min update</b>\n🕐 {now_kst}\n{balance_line}No open positions.")
        return
    lines = ["📊 <b>experiment_v3 · 30-min update</b>", f"🕐 {now_kst}",
             balance_line.strip(), ""]
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
            f"  Entry ${entry:,.4f}  Mark ${mark:,.4f}  Qty {size}\n"
            f"  PnL {_sgn(upnl)} USDT  ({_sgn(pct)}%)"
        )
    alerter.send("\n".join(lines))


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    load_params()

    parser = argparse.ArgumentParser(description="experiment_v3: 1-min ATR trailing-queue scalper")
    parser.add_argument("--once",       action="store_true")
    parser.add_argument("--update",     action="store_true")
    parser.add_argument("--cancel-all", action="store_true")
    parser.add_argument("--close-all",  action="store_true")
    parser.add_argument("--debug",      action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for _noisy in ("urllib3", "pybit", "requests", "httpcore", "httpx"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

    settings = load_settings()
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
    position_alerter = TelegramAlerter.from_env(
        token=position_bot_token,
        chat_id=os.getenv("POSITION_BOT_CHAT_ID") or None,
    )

    if args.update:
        alert_update(position_alerter, get_open_positions(session), session)
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

    symbols = load_symbols()
    LOGGER.info("experiment_v3 started | %d symbols | dry_run=%s", len(symbols), settings.dry_run)

    state          = load_state()
    last_update_at = datetime.now(tz=UTC) - timedelta(seconds=UPDATE_INTERVAL)

    while True:
        now = datetime.now(tz=UTC)
        load_params()

        # ── 1. Fetch live positions ───────────────────────────────────────────
        try:
            live_positions = get_open_positions(session)
        except Exception as err:
            LOGGER.error("Failed to fetch positions: %s", err)
            if args.once:
                break
            time.sleep(CHECK_INTERVAL)
            continue

        # ── 2. Process pending orders ────────────────────────────────────────
        state.setdefault("pending_orders", {})
        for sym in list(state["pending_orders"].keys()):
            pending   = state["pending_orders"][sym]
            placed_at = datetime.fromisoformat(pending["placed_at"]).replace(tzinfo=UTC)
            signal_at = datetime.fromisoformat(pending["signal_at"]).replace(tzinfo=UTC)
            age_min   = (now - signal_at).total_seconds() / 60

            # a. Signal expired — abandon
            if age_min >= MAX_ORDER_AGE_MIN:
                if pending.get("order_id") and pending["order_id"] != "dry":
                    try:
                        session.cancel_order(category="linear", symbol=sym,
                                             orderId=pending["order_id"])
                    except Exception:
                        pass
                LOGGER.info("Signal expired for %s after %.1f min — abandoned", sym, age_min)
                del state["pending_orders"][sym]
                continue

            # b. Order filled → mark as open, set tracking
            if sym in live_positions:
                LOGGER.info("Order filled: %s — tracking position", sym)
                pos    = live_positions[sym]
                entry  = float(pos.get("avgPrice") or pending["limit_price"])
                atr    = float(pending.get("atr", 0))
                tp, sl = calc_tp_sl(pending["side"], entry, atr,
                                    "0.01")   # tick re-fetched in repair loop if needed
                state["open_positions"][sym] = {
                    "symbol":      sym,
                    "side":        pending["side"],
                    "entry_price": entry,
                    "qty":         pending.get("qty", pos.get("size", "0")),
                    "tp_price":    pending.get("tp_price", tp),
                    "sl_price":    pending.get("sl_price", sl),
                    "open_time":   now.isoformat(),
                }
                _record_signal(state, sym, now)
                alert_open(alerter, {
                    "symbol":      sym,
                    "side":        pending["side"],
                    "entry_price": entry,
                    "qty":         state["open_positions"][sym]["qty"],
                    "tp_price":    state["open_positions"][sym]["tp_price"],
                    "sl_price":    state["open_positions"][sym]["sl_price"],
                }, settings.dry_run)
                del state["pending_orders"][sym]
                continue

            # c. Stale — cancel and re-queue in step 4 (leave signal_at intact)
            if age_min < MAX_ORDER_AGE_MIN and pending.get("order_id") and \
               pending["order_id"] != "dry":
                mark = get_mark_price(session, sym)
                if mark and _is_stale(pending, mark):
                    try:
                        session.cancel_order(category="linear", symbol=sym,
                                             orderId=pending["order_id"])
                        LOGGER.info("Stale order cancelled for %s (limit=%s mark=%.4f) — will refresh",
                                    sym, pending["limit_price"], mark)
                    except Exception as err:
                        if "110001" in str(err):
                            # already filled between fetch and cancel
                            LOGGER.info("Order already filled for %s (late cancel)", sym)
                        else:
                            LOGGER.warning("Cancel error for %s: %s", sym, err)
                    # Keep signal_at so re-queue in step 4 inherits the original timer
                    state["pending_orders"][sym]["order_id"] = None

        # ── 3. Detect position closes (TP/SL hit on exchange) ──────────────
        for sym, tracked in list(state["open_positions"].items()):
            if sym in live_positions or sym in state.get("pending_orders", {}):
                continue
            exit_price = get_mark_price(session, sym) or float(tracked.get("entry_price", 0))
            LOGGER.info("Position closed: %s  exit≈%.4f", sym, exit_price)
            alert_close(alerter, tracked, exit_price, "TP/SL hit", settings.dry_run)
            del state["open_positions"][sym]

        # ── 4. Signal scan + stale refresh ────────────────────────────────────
        open_margin = sum(
            float(p.get("positionIM") or 0) or
            float(p.get("size", 0)) * float(p.get("avgPrice", 0)) /
            float(p.get("leverage") or LEVERAGE or 1)
            for p in live_positions.values()
        )
        if open_margin >= NOTIONAL_CAP_USDT:
            LOGGER.info("Cap reached ($%.2f) — skipping signal scan.", open_margin)
        else:
            expected_candle_ts = str((int(now.timestamp() // 60) - 1) * 60 * 1000)

            for symbol, lev_override in symbols:
                has_open    = symbol in state["open_positions"] or symbol in live_positions
                has_pending = symbol in state.get("pending_orders", {})

                if has_open:
                    continue

                # Re-queue a stale pending order (order_id was nulled in step 2c)
                if has_pending:
                    pending = state["pending_orders"][symbol]
                    if pending.get("order_id") is not None:
                        continue   # still valid, nothing to do
                    # order_id is None → need a re-place at current price
                    atr         = float(pending.get("atr", 0))
                    signal_side = pending["side"]
                    signal_at_iso = pending["signal_at"]
                    if atr > 0:
                        new_entry = _place_entry(
                            session, symbol, signal_side, atr,
                            settings.dry_run, lev_override, now,
                            signal_at_iso=signal_at_iso,
                        )
                        if new_entry:
                            state["pending_orders"][symbol] = new_entry
                        else:
                            del state["pending_orders"][symbol]
                    else:
                        del state["pending_orders"][symbol]
                    continue

                # Fresh signal scan
                if expected_candle_ts == state["processed_candles"].get(symbol):
                    continue

                if not _freq_ok(state, symbol, now, live_positions):
                    LOGGER.debug("Frequency guard blocked %s", symbol)
                    continue

                try:
                    side, candle_ts, atr = check_signal(session, symbol)
                except Exception as err:
                    LOGGER.warning("Signal check failed %s: %s", symbol, err)
                    continue

                if candle_ts and candle_ts == state["processed_candles"].get(symbol):
                    continue
                if candle_ts:
                    state["processed_candles"][symbol] = candle_ts

                if side is None:
                    continue

                # 15-min trend alignment — only trade with the HTF direction
                try:
                    htf_side = check_htf_trend(session, symbol)
                except Exception as err:
                    LOGGER.warning("HTF trend check failed %s: %s", symbol, err)
                    htf_side = None
                if htf_side is not None and htf_side != side:
                    LOGGER.debug("HTF trend (%s) conflicts 1m signal (%s) for %s — skipped",
                                 htf_side, side, symbol)
                    continue

                LOGGER.info("Signal → %s %s  atr=%.4f  htf=%s", side, symbol, atr, htf_side)
                entry_info = _place_entry(
                    session, symbol, side, atr,
                    settings.dry_run, lev_override, now,
                )
                if entry_info:
                    state["pending_orders"][symbol] = entry_info

        # ── 5. 30-min update ────────────────────────────────────────────────
        if (now - last_update_at).total_seconds() >= UPDATE_INTERVAL:
            alert_update(position_alerter, live_positions, session)
            last_update_at = now
            state["last_update_alert"] = now.isoformat()

        save_state(state)

        if args.once:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
