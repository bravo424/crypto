"""
experiment_v7 — Signal-Driven Directional Scalp on Bybit USDT-perp.

Why not market making (v6)?
---------------------------
  At Bybit VIP 0, maker fee = 0.02% per side — you PAY, never receive a rebate.
  A round-trip MM trade (both sides as maker) costs 0.04% in fees, before
  accounting for adverse selection.  The high Sharpe from the MM backtest was
  an artefact of the fill model, not real edge.

This strategy instead:
  1. Waits for a strong live signal from the market_data WebSocket feed:
       - ob_imbalance AND trade_pressure_5m both agree on direction.
  2. Enters with a market order (taker, 0.055%) — guarantees fill, no queue.
  3. Immediately places native exchange TP (limit, 0.02%) + SL (market) via
     set_trading_stop.
  4. Polls each cycle for max-hold timeout and emergency backup SL.

Fee math per trade
------------------
  TP scenario (+0.8%):  0.8% − 0.055% entry − 0.02% TP exit = +0.725% net
  SL scenario (−0.4%): −0.4% − 0.055% entry − 0.055% SL exit = −0.51% net
  Break-even win rate ≈ 0.51 / (0.725 + 0.51) ≈ 41%

Key params (strategies/experiment_v7/params.json)
--------------------------------------------------
  ob_imbalance_thresh   0.25   min |ob_imbalance| to fire entry
  pressure_thresh       0.15   min |trade_pressure_5m| to fire entry
  tp_pct                0.008  take-profit distance from entry (0.8%)
  sl_pct                0.004  stop-loss distance from entry   (0.4%)
  max_hold_min          20     force-close if position held > 20 min
  max_risk_pct          0.01   position notional = equity × max_risk_pct / sl_pct
  max_notional_usd      200    hard cap on position size in USDT
  cooldown_sec          60     min seconds between entries for same symbol
  max_open_positions    3      max simultaneous open positions
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path

from dotenv import load_dotenv
from pybit.unified_trading import HTTP

from upbit_bybit_bot.config import load_settings
from upbit_bybit_bot.telegram_alerter import TelegramAlerter

import market_data as md
from market_data.signal_store import MarketSignal

LOGGER = logging.getLogger(__name__)
HERE   = Path(__file__).resolve().parent

MAKER_FEE = 0.0002
TAKER_FEE = 0.00055
HKT = timezone(timedelta(hours=8))


# ── params ────────────────────────────────────────────────────────────────────

class Params:
    # ── entry signal ──────────────────────────────────────────────────────────
    ob_imbalance_thresh:  float = 0.25   # min |ob_imbalance| to enter
    pressure_thresh:      float = 0.15   # min |trade_pressure_5m| to enter
    require_trend_bias:   bool  = False  # if True, also require trend_bias to agree
    # ── position management ───────────────────────────────────────────────────
    tp_pct:               float = 0.008  # take-profit at +0.8% from entry
    sl_pct:               float = 0.004  # stop-loss   at -0.4% from entry
    max_hold_min:         int   = 20     # force-close after 20 min
    # ── sizing ────────────────────────────────────────────────────────────────
    max_risk_pct:         float = 0.01   # risk 1% of equity per trade
    max_notional_usd:     float = 200.0  # hard cap on notional per position
    # ── frequency ─────────────────────────────────────────────────────────────
    cooldown_sec:         int   = 60     # seconds between entries per symbol
    max_open_positions:   int   = 3      # simultaneous open positions cap
    scan_interval_sec:    float = 5.0    # main loop sleep
    # ── leverage & margin ─────────────────────────────────────────────────────
    leverage_default:     int   = 3
    symbol_leverage:      dict  = None   # type: ignore[assignment]
    use_cross_margin:     bool  = True
    # ── circuit breakers ──────────────────────────────────────────────────────
    max_daily_drawdown_pct: float = 0.03
    max_loss_streak:        int   = 3
    loss_streak_pause_min:  int   = 30
    # ── alerting ──────────────────────────────────────────────────────────────
    periodic_alert_min:     int   = 30
    daily_report_hkt_hour:  int   = 22
    # ── misc ──────────────────────────────────────────────────────────────────
    ws_startup_wait_sec:    float = 8.0
    dry_run:                bool  = False


_p = Params()
_p.symbol_leverage = {}


def load_params() -> None:
    path = HERE / "params.json"
    with path.open(encoding="utf-8") as fh:
        d = json.load(fh)

    _p.ob_imbalance_thresh    = float(d.get("ob_imbalance_thresh",    _p.ob_imbalance_thresh))
    _p.pressure_thresh        = float(d.get("pressure_thresh",        _p.pressure_thresh))
    _p.require_trend_bias     = bool(d.get("require_trend_bias",      _p.require_trend_bias))
    _p.tp_pct                 = float(d.get("tp_pct",                 _p.tp_pct))
    _p.sl_pct                 = float(d.get("sl_pct",                 _p.sl_pct))
    _p.max_hold_min           = int(d.get("max_hold_min",             _p.max_hold_min))
    _p.max_risk_pct           = float(d.get("max_risk_pct",           _p.max_risk_pct))
    _p.max_notional_usd       = float(d.get("max_notional_usd",       _p.max_notional_usd))
    _p.cooldown_sec           = int(d.get("cooldown_sec",             _p.cooldown_sec))
    _p.max_open_positions     = int(d.get("max_open_positions",       _p.max_open_positions))
    _p.scan_interval_sec      = float(d.get("scan_interval_sec",      _p.scan_interval_sec))
    _p.leverage_default       = int(d.get("leverage_default",         _p.leverage_default))
    _p.symbol_leverage        = {str(k): int(v) for k, v in d.get("symbol_leverage", _p.symbol_leverage).items()}
    _p.use_cross_margin       = bool(d.get("use_cross_margin",        _p.use_cross_margin))
    _p.max_daily_drawdown_pct = float(d.get("max_daily_drawdown_pct", _p.max_daily_drawdown_pct))
    _p.max_loss_streak        = int(d.get("max_loss_streak",          _p.max_loss_streak))
    _p.loss_streak_pause_min  = int(d.get("loss_streak_pause_min",    _p.loss_streak_pause_min))
    _p.periodic_alert_min     = int(d.get("periodic_alert_min",       _p.periodic_alert_min))
    _p.daily_report_hkt_hour  = int(d.get("daily_report_hkt_hour",    _p.daily_report_hkt_hour))
    _p.ws_startup_wait_sec    = float(d.get("ws_startup_wait_sec",    _p.ws_startup_wait_sec))
    _p.dry_run                = bool(d.get("dry_run",                 _p.dry_run))


# ── per-symbol state ──────────────────────────────────────────────────────────

class SymbolState:
    def __init__(self) -> None:
        self.net_pos_qty:     float           = 0.0
        self.entry_price:     float           = 0.0
        self.position_open_ts: datetime | None = None
        self.last_entry_ts:   datetime | None  = None  # for cooldown


# ── instrument helpers ────────────────────────────────────────────────────────

_instr_cache: dict[str, dict] = {}


def _get_instrument(session: HTTP, symbol: str) -> dict:
    if symbol not in _instr_cache:
        try:
            resp  = session.get_instruments_info(category="linear", symbol=symbol)
            items = resp.get("result", {}).get("list", [])
            if items:
                _instr_cache[symbol] = items[0]
        except Exception:
            _instr_cache[symbol] = {}
    return _instr_cache.get(symbol, {})


def _tick_size(session: HTTP, symbol: str) -> str:
    instr = _get_instrument(session, symbol)
    return instr.get("priceFilter", {}).get("tickSize", "0.01")


def _qty_step(session: HTTP, symbol: str) -> str:
    instr = _get_instrument(session, symbol)
    return instr.get("lotSizeFilter", {}).get("qtyStep", "0.001")


def _round_price(price: float, tick: str, up: bool = False) -> str:
    t = Decimal(tick)
    d = Decimal(str(price))
    mode = ROUND_UP if up else ROUND_DOWN
    return format((d / t).to_integral_value(rounding=mode) * t.normalize(), "f")


def _round_qty(qty: float, step: str) -> str:
    s = Decimal(step)
    d = Decimal(str(qty))
    return format((d / s).to_integral_value(rounding=ROUND_DOWN) * s.normalize(), "f")


def _get_equity(session: HTTP) -> float:
    resp = session.get_wallet_balance(accountType="UNIFIED")
    return float(resp["result"]["list"][0].get("totalEquity") or 0)


def _fetch_fees(session: HTTP, since: datetime, until: datetime) -> float:
    total: float = 0.0
    cursor: str  = ""
    start_ms = int(since.timestamp() * 1000)
    end_ms   = int(until.timestamp() * 1000)
    try:
        while True:
            kwargs: dict = dict(
                category="linear", startTime=start_ms, endTime=end_ms, limit=100)
            if cursor:
                kwargs["cursor"] = cursor
            resp   = session.get_executions(**kwargs)
            result = resp.get("result", {})
            for e in result.get("list", []):
                total += abs(float(e.get("execFee") or 0))
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
    except Exception as exc:
        LOGGER.warning("_fetch_fees: %s", exc)
    return total


def _get_positions(session: HTTP) -> dict[str, dict]:
    resp = session.get_positions(category="linear", settleCoin="USDT")
    return {
        item["symbol"]: item
        for item in resp["result"]["list"]
        if float(item.get("size") or 0) > 0
    }


# ── leverage & margin setup ───────────────────────────────────────────────────

def _get_leverage(symbol: str) -> int:
    return _p.symbol_leverage.get(symbol, _p.leverage_default)


def _setup_symbol(session: HTTP, symbol: str) -> None:
    lev     = _get_leverage(symbol)
    lev_str = str(lev)
    mode    = 0 if _p.use_cross_margin else 1
    label   = "cross" if _p.use_cross_margin else "isolated"

    if _p.dry_run:
        LOGGER.info("[DRY RUN] %s: would set %s margin leverage=%dx", symbol, label, lev)
        return

    # switch_margin_mode is not supported on Unified Trading Accounts (UTA).
    # Bybit error 100028 = "unified account is forbidden" — safe to skip silently.
    # Non-UTA accounts: tradeMode=0 switches TO cross, tradeMode=1 to isolated.
    try:
        session.switch_margin_mode(
            category="linear", symbol=symbol,
            tradeMode=mode, buyLeverage=lev_str, sellLeverage=lev_str,
        )
        LOGGER.info("%s: margin mode → %s", symbol, label)
    except Exception as exc:
        err_str = str(exc)
        if "100028" in err_str:
            # UTA account — margin mode is managed account-wide, not per symbol
            LOGGER.debug("%s: UTA account — skipping per-symbol margin mode switch", symbol)
        else:
            LOGGER.debug("%s: switch_margin_mode: %s", symbol, exc)

    try:
        session.set_leverage(
            category="linear", symbol=symbol,
            buyLeverage=lev_str, sellLeverage=lev_str,
        )
        LOGGER.info("%s: leverage → %dx", symbol, lev)
    except Exception as exc:
        err_str = str(exc)
        if "110043" in err_str:
            # "leverage not modified" — already set to the requested value, nothing to do
            LOGGER.debug("%s: leverage already at %dx — no change needed", symbol, lev)
        else:
            LOGGER.warning("%s: set_leverage failed: %s", symbol, exc)


# ── signal evaluation ─────────────────────────────────────────────────────────

def _check_entry(sig: MarketSignal) -> str | None:
    """Return 'Buy', 'Sell', or None based on live order-book and flow signals.

    Both ob_imbalance and trade_pressure_5m must agree and exceed their
    respective thresholds.  Optionally also requires trend_bias to agree.
    """
    ob  = sig.ob_imbalance
    p5m = sig.trade_pressure_5m

    bullish = ob >= _p.ob_imbalance_thresh and p5m >= _p.pressure_thresh
    bearish = ob <= -_p.ob_imbalance_thresh and p5m <= -_p.pressure_thresh

    if bullish:
        if _p.require_trend_bias and sig.trend_bias != "Buy":
            return None
        return "Buy"

    if bearish:
        if _p.require_trend_bias and sig.trend_bias != "Sell":
            return None
        return "Sell"

    return None


# ── position management ───────────────────────────────────────────────────────

def _calc_qty(session: HTTP, symbol: str, mid: float, equity: float) -> str:
    """Position size: risk-based notional capped at max_notional_usd."""
    notional = min(
        equity * _p.max_risk_pct / max(_p.sl_pct, 0.0001),
        _p.max_notional_usd,
    )
    qty_raw = notional / mid if mid > 0 else 0.0
    step    = _qty_step(session, symbol)
    qty_str = _round_qty(qty_raw, step)
    return qty_str


def _set_native_tp_sl(session: HTTP, symbol: str, st: SymbolState) -> None:
    if st.entry_price <= 0 or st.net_pos_qty == 0.0:
        return

    is_long = st.net_pos_qty > 0
    tick    = _tick_size(session, symbol)

    if is_long:
        tp = _round_price(st.entry_price * (1.0 + _p.tp_pct), tick, up=True)
        sl = _round_price(st.entry_price * (1.0 - _p.sl_pct), tick, up=False)
    else:
        tp = _round_price(st.entry_price * (1.0 - _p.tp_pct), tick, up=False)
        sl = _round_price(st.entry_price * (1.0 + _p.sl_pct), tick, up=True)

    LOGGER.info("%s: native TP=%s SL=%s  entry=%.4f %s",
                symbol, tp, sl, st.entry_price,
                "Long" if is_long else "Short")

    if _p.dry_run:
        LOGGER.info("[DRY RUN] %s: would set_trading_stop TP=%s SL=%s", symbol, tp, sl)
        return

    try:
        session.set_trading_stop(
            category="linear", symbol=symbol,
            takeProfit=tp, stopLoss=sl,
            tpTriggerBy="MarkPrice", slTriggerBy="MarkPrice",
            positionIdx=0,
        )
        LOGGER.info("%s: TP/SL confirmed on exchange", symbol)
    except Exception as exc:
        LOGGER.warning("%s: set_trading_stop failed: %s", symbol, exc)


def _market_close(session: HTTP, symbol: str, st: SymbolState, reason: str) -> bool:
    qty  = abs(st.net_pos_qty)
    side = "Sell" if st.net_pos_qty > 0 else "Buy"
    LOGGER.warning("%s: %s — market close %s qty=%.6f entry=%.4f",
                   symbol, reason, side, qty, st.entry_price)
    if _p.dry_run:
        LOGGER.info("[DRY RUN] %s: would market-close %s qty=%.6f", symbol, side, qty)
        return True
    try:
        session.place_order(
            category="linear", symbol=symbol,
            side=side, orderType="Market",
            qty=str(qty), timeInForce="IOC",
            reduceOnly=True,
        )
        st.net_pos_qty     = 0.0
        st.entry_price     = 0.0
        st.position_open_ts = None
        return True
    except Exception as exc:
        LOGGER.error("%s: market close failed: %s", symbol, exc)
        return False


def _manage_position(session: HTTP, symbol: str, st: SymbolState,
                     sig: MarketSignal, now: datetime) -> bool:
    """Backup polling: max-hold timeout + emergency SL at 2× threshold.

    Primary TP/SL live on the exchange via set_trading_stop.
    Returns True if a close was issued this cycle.
    """
    if st.net_pos_qty == 0.0 or st.entry_price <= 0:
        return False

    mid     = sig.mid
    is_long = st.net_pos_qty > 0
    pnl_pct = ((mid - st.entry_price) / st.entry_price) if is_long \
              else ((st.entry_price - mid) / st.entry_price)

    if st.position_open_ts is not None:
        age_min = (now - st.position_open_ts).total_seconds() / 60.0
        if age_min >= _p.max_hold_min:
            _market_close(session, symbol, st,
                          f"max-hold {age_min:.0f}m >= {_p.max_hold_min}m")
            return True

    if pnl_pct <= -(_p.sl_pct * 2.0):
        _market_close(session, symbol, st,
                      f"emergency SL pnl={pnl_pct*100:.2f}% (native stop missed)")
        return True

    LOGGER.debug("%s: pos=%.6f entry=%.4f mid=%.4f pnl=%+.2f%%  age=%.0fm",
                 symbol, st.net_pos_qty, st.entry_price, mid, pnl_pct * 100,
                 (now - st.position_open_ts).total_seconds() / 60.0
                 if st.position_open_ts else 0)
    return False


# ── cancel all open orders on exit ────────────────────────────────────────────

def _cancel_all(session: HTTP) -> None:
    LOGGER.info("Cancelling all open orders …")
    try:
        session.cancel_all_orders(category="linear", settleCoin="USDT")
        LOGGER.info("cancel_all_orders sent.")
    except Exception as exc:
        LOGGER.warning("cancel_all_orders failed: %s", exc)


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    load_params()

    parser = argparse.ArgumentParser(description="experiment_v7: directional scalp")
    parser.add_argument("--debug",   action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once",    action="store_true",
                        help="Run one scan cycle and exit")
    args = parser.parse_args()

    from utils.logging_setup import setup_logging
    setup_logging("experiment_v7", debug=args.debug)

    if args.dry_run:
        _p.dry_run = True

    settings = load_settings()
    session  = HTTP(
        testnet=settings.bybit_testnet,
        api_key=settings.bybit_credentials.api_key,
        api_secret=settings.bybit_credentials.api_secret,
        recv_window=20000,
    )
    alerter = TelegramAlerter.from_env(
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )

    # ── start market data ─────────────────────────────────────────────────────
    md.start_market_data()
    LOGGER.info("Waiting %.0fs for WebSocket to populate …", _p.ws_startup_wait_sec)
    time.sleep(_p.ws_startup_wait_sec)

    store   = md.get_signal_store()
    symbols = store.symbols()
    if not symbols:
        from market_data.websocket_client import _load_symbols
        symbols = _load_symbols()

    LOGGER.info(
        "experiment_v7 started | %d symbols | dry_run=%s | tp=%.1f%% sl=%.1f%%",
        len(symbols), _p.dry_run, _p.tp_pct * 100, _p.sl_pct * 100,
    )

    # ── setup leverage & margin per symbol ────────────────────────────────────
    LOGGER.info("Configuring leverage / margin mode for %d symbols …", len(symbols))
    for sym in symbols:
        try:
            _setup_symbol(session, sym)
        except Exception as exc:
            LOGGER.warning("%s: setup failed: %s", sym, exc)

    states: dict[str, SymbolState] = {s: SymbolState() for s in symbols}

    # ── shutdown handler ──────────────────────────────────────────────────────
    _shutdown    = threading.Event()
    _clean_exit  = False   # set True on graceful SIGTERM/Ctrl-C

    def _on_shutdown(signum, frame):  # noqa: ANN001
        nonlocal _clean_exit
        LOGGER.info("Shutdown signal %s received", signum)
        _clean_exit = True
        _cancel_all(session)
        md.stop_market_data()
        _shutdown.set()

    signal.signal(signal.SIGTERM, _on_shutdown)
    signal.signal(signal.SIGINT,  _on_shutdown)

    # ── alert / circuit-breaker state ─────────────────────────────────────────
    daily_start_equity:     float | None = None
    daily_date:             str          = ""
    loss_streak:            int          = 0
    loss_streak_pause_until: datetime | None = None
    prev_positions:         dict[str, dict] = {}

    _now0               = datetime.now(tz=UTC)
    periodic_alert_ts:  datetime = _now0
    periodic_alert_eq:  float    = 0.0
    daily_report_sent_slot: str  = ""

    # ── startup alert ─────────────────────────────────────────────────────────
    if alerter:
        try:
            alerter.send(
                f"🟢 <b>experiment_v7</b> started\n"
                f"{len(symbols)} symbols | tp={_p.tp_pct*100:.1f}% sl={_p.sl_pct*100:.1f}% "
                f"dry_run={_p.dry_run}"
            )
        except Exception:
            pass

    try:
        while not _shutdown.is_set():
            now = datetime.now(tz=UTC)
            load_params()

            # ── daily equity snapshot ─────────────────────────────────────────
            today = now.strftime("%Y-%m-%d")
            if daily_date != today:
                try:
                    daily_start_equity = _get_equity(session)
                    daily_date = today
                    LOGGER.info("New day — equity snapshot: %.4f USDT", daily_start_equity)
                except Exception:
                    pass

            # ── daily drawdown guard ──────────────────────────────────────────
            if daily_start_equity and daily_start_equity > 0:
                try:
                    curr_eq = _get_equity(session)
                    if periodic_alert_eq == 0.0:
                        periodic_alert_eq = curr_eq
                    dd = (daily_start_equity - curr_eq) / daily_start_equity
                    if dd >= _p.max_daily_drawdown_pct:
                        LOGGER.warning(
                            "Daily drawdown %.1f%% ≥ limit %.0f%% — halting",
                            dd * 100, _p.max_daily_drawdown_pct * 100)
                        _cancel_all(session)
                        time.sleep(_p.scan_interval_sec)
                        continue
                except Exception:
                    pass

            # ── 30-min rolling alert ──────────────────────────────────────────
            elapsed_min = (now - periodic_alert_ts).total_seconds() / 60.0
            if alerter and elapsed_min >= _p.periodic_alert_min and periodic_alert_eq > 0:
                try:
                    curr_eq = _get_equity(session)
                    pnl_30  = curr_eq - periodic_alert_eq
                    pct_30  = (pnl_30 / periodic_alert_eq * 100) if periodic_alert_eq > 0 else 0.0
                    fees_30 = _fetch_fees(session, periodic_alert_ts, now)
                    open_cnt = sum(1 for s in states.values() if s.net_pos_qty != 0.0)
                    icon = "📈" if pnl_30 >= 0 else "📉"
                    alerter.send(
                        f"{icon} <b>experiment_v7</b> — {_p.periodic_alert_min}min Update\n"
                        f"PnL ({_p.periodic_alert_min}m): <b>{pnl_30:+.4f} USDT</b>"
                        f" ({pct_30:+.2f}%)\n"
                        f"Fees ({_p.periodic_alert_min}m): {fees_30:.4f} USDT\n"
                        f"Balance: {curr_eq:.4f} USDT\n"
                        f"Open positions: {open_cnt}"
                    )
                    LOGGER.info("Periodic alert sent | pnl=%+.4f fees=%.4f", pnl_30, fees_30)
                    periodic_alert_ts = now
                    periodic_alert_eq = curr_eq
                except Exception as exc:
                    LOGGER.warning("Periodic alert failed: %s", exc)

            # ── daily 10 PM HKT report ────────────────────────────────────────
            now_hkt  = now.astimezone(HKT)
            hkt_slot = now_hkt.strftime("%Y-%m-%d %H")
            if (alerter and daily_start_equity
                    and now_hkt.hour == _p.daily_report_hkt_hour
                    and now_hkt.minute < 5
                    and hkt_slot != daily_report_sent_slot):
                try:
                    curr_eq  = _get_equity(session)
                    pnl_day  = curr_eq - daily_start_equity
                    pct_day  = (pnl_day / daily_start_equity * 100) if daily_start_equity > 0 else 0.0
                    day_start_dt = datetime.strptime(daily_date, "%Y-%m-%d").replace(tzinfo=UTC)
                    fees_day = _fetch_fees(session, day_start_dt, now)
                    icon = "📈" if pnl_day >= 0 else "📉"
                    alerter.send(
                        f"{icon} <b>experiment_v7</b> — Daily Summary\n"
                        f"({now_hkt.strftime('%Y-%m-%d')} {_p.daily_report_hkt_hour}:00 HKT)\n"
                        f"PnL (today): <b>{pnl_day:+.4f} USDT</b> ({pct_day:+.2f}%)\n"
                        f"Fees (today): {fees_day:.4f} USDT\n"
                        f"Balance: {curr_eq:.4f} USDT\n"
                        f"Day start: {daily_start_equity:.4f} USDT"
                    )
                    LOGGER.info("Daily report sent | pnl=%+.4f fees=%.4f", pnl_day, fees_day)
                    daily_report_sent_slot = hkt_slot
                except Exception as exc:
                    LOGGER.warning("Daily report failed: %s", exc)

            # ── loss-streak pause ─────────────────────────────────────────────
            if loss_streak_pause_until and now < loss_streak_pause_until:
                rem = int((loss_streak_pause_until - now).total_seconds() / 60) + 1
                LOGGER.info("Loss-streak pause — %d min remaining", rem)
                time.sleep(_p.scan_interval_sec)
                continue
            elif loss_streak_pause_until and now >= loss_streak_pause_until:
                loss_streak_pause_until = None
                loss_streak = 0
                LOGGER.info("Loss-streak pause expired — resuming.")

            # ── refresh live positions ────────────────────────────────────────
            try:
                live_positions = _get_positions(session)
            except Exception as exc:
                LOGGER.warning("Position fetch failed: %s", exc)
                time.sleep(_p.scan_interval_sec)
                continue

            # Detect closed positions → update loss streak
            for sym, old in prev_positions.items():
                if sym not in live_positions:
                    old_entry  = float(old.get("avgPrice") or 0)
                    mark_price = 0.0
                    try:
                        resp = session.get_tickers(category="linear", symbol=sym)
                        mark_price = float(resp["result"]["list"][0].get("markPrice") or 0)
                    except Exception:
                        pass
                    old_side = old.get("side", "Buy")
                    won = (mark_price > old_entry) if old_side == "Buy" \
                          else (mark_price < old_entry)
                    if won:
                        loss_streak = 0
                        LOGGER.info("%s: position closed at PROFIT (mark=%.4f entry=%.4f)",
                                    sym, mark_price, old_entry)
                    else:
                        loss_streak += 1
                        LOGGER.info("%s: position closed at LOSS  (mark=%.4f entry=%.4f) "
                                    "streak=%d", sym, mark_price, old_entry, loss_streak)
                        if loss_streak >= _p.max_loss_streak and not _p.dry_run:
                            loss_streak_pause_until = now + timedelta(
                                minutes=_p.loss_streak_pause_min)
                            LOGGER.warning("Loss streak %d — pausing %d min",
                                           loss_streak, _p.loss_streak_pause_min)
                            if alerter:
                                try:
                                    alerter.send(
                                        f"🚫 <b>experiment_v7</b>\n"
                                        f"<b>{loss_streak} consecutive losses</b>\n"
                                        f"Pausing {_p.loss_streak_pause_min} min "
                                        f"(until {loss_streak_pause_until.strftime('%H:%M UTC')})"
                                    )
                                except Exception:
                                    pass

            # Update per-symbol state; set native TP/SL on new position
            for sym in symbols:
                pos = live_positions.get(sym)
                st  = states[sym]
                if pos:
                    new_qty = float(pos.get("size") or 0)
                    if pos.get("side") == "Sell":
                        new_qty = -new_qty
                    is_new = st.net_pos_qty == 0.0 and new_qty != 0.0
                    st.net_pos_qty = new_qty
                    st.entry_price = float(pos.get("avgPrice") or 0)
                    if is_new:
                        st.position_open_ts = now
                        LOGGER.info(
                            "%s: position opened — size=%.6f entry=%.4f lev=%dx",
                            sym, new_qty, st.entry_price, _get_leverage(sym),
                        )
                        if alerter:
                            try:
                                alerter.send(
                                    f"{'🟢' if new_qty > 0 else '🔴'} <b>experiment_v7</b> "
                                    f"{'Long' if new_qty > 0 else 'Short'}\n"
                                    f"<b>{sym}</b>  entry={st.entry_price:.4f}\n"
                                    f"TP={st.entry_price*(1+_p.tp_pct):.4f}  "
                                    f"SL={st.entry_price*(1-_p.sl_pct):.4f}"
                                )
                            except Exception:
                                pass
                        try:
                            _set_native_tp_sl(session, sym, st)
                        except Exception as exc:
                            LOGGER.warning("%s: _set_native_tp_sl error: %s", sym, exc)
                else:
                    if st.net_pos_qty != 0.0:
                        LOGGER.info("%s: position cleared", sym)
                    st.net_pos_qty      = 0.0
                    st.entry_price      = 0.0
                    st.position_open_ts = None

            prev_positions = live_positions

            # ── position management (timeout + emergency backup SL) ────────────
            for sym in symbols:
                sig = store.get(sym)
                st  = states[sym]
                if sig is None or sig.mid <= 0 or st.net_pos_qty == 0.0:
                    continue
                try:
                    _manage_position(session, sym, st, sig, now)
                except Exception as exc:
                    LOGGER.warning("%s: position management error: %s", sym, exc)

            # ── entry scan ────────────────────────────────────────────────────
            open_count = sum(1 for s in states.values() if s.net_pos_qty != 0.0)
            if open_count >= _p.max_open_positions:
                LOGGER.debug("Max open positions (%d) reached — skipping entry scan",
                             _p.max_open_positions)
            else:
                try:
                    equity = _get_equity(session)
                except Exception:
                    equity = 0.0

                for sym in symbols:
                    st  = states[sym]
                    sig = store.get(sym)

                    if sig is None or sig.mid <= 0:
                        LOGGER.debug("%s: no market data — skip", sym)
                        continue

                    if st.net_pos_qty != 0.0:
                        continue  # already in a position for this symbol

                    # Cooldown check
                    if st.last_entry_ts is not None:
                        elapsed = (now - st.last_entry_ts).total_seconds()
                        if elapsed < _p.cooldown_sec:
                            LOGGER.debug("%s: cooldown %.0fs remaining",
                                         sym, _p.cooldown_sec - elapsed)
                            continue

                    side = _check_entry(sig)
                    if side is None:
                        LOGGER.debug("%s: no signal (imb=%.3f p5m=%.3f bias=%s)",
                                     sym, sig.ob_imbalance,
                                     sig.trade_pressure_5m, sig.trend_bias)
                        continue

                    # Size the order
                    qty_str = _calc_qty(session, sym, sig.mid, equity)
                    if float(qty_str) <= 0:
                        LOGGER.warning("%s: computed qty=0 — skip (equity=%.2f)", sym, equity)
                        continue

                    LOGGER.info(
                        "%s: ENTRY %s  mid=%.4f  qty=%s  "
                        "imb=%.3f  p5m=%.3f  bias=%s",
                        sym, side, sig.mid, qty_str,
                        sig.ob_imbalance, sig.trade_pressure_5m, sig.trend_bias,
                    )

                    if _p.dry_run:
                        LOGGER.info("[DRY RUN] %s: would place Market %s qty=%s",
                                    sym, side, qty_str)
                        st.last_entry_ts = now
                        open_count += 1
                        if open_count >= _p.max_open_positions:
                            break
                        continue

                    try:
                        session.place_order(
                            category="linear", symbol=sym,
                            side=side, orderType="Market",
                            qty=qty_str, timeInForce="IOC",
                        )
                        st.last_entry_ts = now
                        open_count += 1
                        LOGGER.info("%s: Market %s order sent qty=%s", sym, side, qty_str)
                    except Exception as exc:
                        LOGGER.error("%s: entry order failed: %s", sym, exc)

                    if open_count >= _p.max_open_positions:
                        break

            if args.once:
                break

            time.sleep(_p.scan_interval_sec)

    except KeyboardInterrupt:
        _clean_exit = True
        LOGGER.info("Keyboard interrupt — shutting down.")
    except Exception as exc:
        LOGGER.exception("Unhandled exception in main loop: %s", exc)
        if alerter:
            try:
                alerter.send(
                    f"🔴 <b>experiment_v7 CRASHED</b>\n"
                    f"<code>{type(exc).__name__}: {exc}</code>\n"
                    f"Check logs immediately. Open positions may still be live."
                )
            except Exception:
                pass
    finally:
        _cancel_all(session)
        md.stop_market_data()
        LOGGER.info("experiment_v7 stopped.")
        if alerter:
            try:
                stop_reason = "clean shutdown" if _clean_exit else "unexpected stop"
                alerter.send(
                    f"⛔ <b>experiment_v7 stopped</b> ({stop_reason})\n"
                    f"All orders cancelled. Check open positions on Bybit."
                )
            except Exception:
                pass


if __name__ == "__main__":
    main()
