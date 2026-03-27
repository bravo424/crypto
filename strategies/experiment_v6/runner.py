"""
experiment_v6 — Signal-enhanced market making on Bybit USDT-perp.

Design
------
  1. WebSocket feed (market_data package) streams live order books + trades
     for every active symbol in ``symbol_list.csv`` next to this strategy
     (path override: ``symbols_csv`` in params.json).
  2. Every `quote_refresh_sec` the runner reads the SignalStore and:
       a. Cancels all existing quotes for a symbol.
       b. If ob_imbalance is too extreme (|imbalance| > threshold) → skip
          (adverse selection: one side of the book has vanished; filling
          there would be against a directional move, not mean-reversion).
       c. Compute inventory-skewed bid/ask prices:
            bid = mid × (1 - half_spread - skew_adj)
            ask = mid × (1 + half_spread + skew_adj)
          where skew_adj = (pos / max_inventory) × skew_factor × half_spread
       d. Place PostOnly limit bid and ask.
  3. Token-bucket rate limiter keeps total order ops ≤ max_orders_per_sec.
  4. Daily drawdown and loss-streak circuit breakers mirror v4/v5.
  5. SIGINT / SIGTERM → cancel all quotes and exit cleanly.

Fee viability (VIP 0)
---------------------
  Maker fee  = 0.02% per side.
  Round-trip = 0.04%.
  Default half_spread = 0.04%, full spread = 0.08% → net = 0.04% per round trip.
  Min viable half_spread = 0.02% (break-even) — params default is 0.04%.

  Optional ``tp_cover_round_trip_fees``: native TP distance is
  max(tp_pct, 2×maker + tp_fee_buffer_pct) so the *gross* take-profit move
  covers fees if entry and TP exit are both maker.  This is not colocated HFT;
  tight TPs still face adverse selection and tick-size floors on illiquid symbols.
  SL (sl_pct) is unchanged — always set alongside TP via set_trading_stop.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
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

HKT = timezone(timedelta(hours=8))   # Hong Kong Time = UTC+8


# ── params ────────────────────────────────────────────────────────────────────

class Params:
    half_spread_pct:              float = 0.0004
    max_inventory_notional:       float = 100.0
    inventory_skew_factor:        float = 0.5
    quote_qty_usd:                float = 15.0
    quote_refresh_sec:            float = 2.0
    max_orders_per_sec:           int   = 8
    max_open_quotes:              int   = 2
    signal_ob_imbalance_threshold: float = 0.35
    signal_pressure_threshold:    float = 0.20
    # ── leverage & margin ─────────────────────────────────────────────────────
    leverage_default:             int   = 3       # leverage applied to all symbols by default
    symbol_leverage:              dict  = None    # type: ignore[assignment]  per-symbol overrides e.g. {"BTCUSDT": 5}
    use_cross_margin:             bool  = True    # True = cross, False = isolated
    # ── position management ───────────────────────────────────────────────────
    tp_pct:                       float = 0.008   # take-profit distance from entry (price %)
    sl_pct:                       float = 0.005   # stop-loss distance from entry (price %)
    tp_cover_round_trip_fees:     bool  = False    # if True, TP % ≥ 2×maker + buffer (see docstring)
    tp_fee_buffer_pct:            float = 0.0001   # extra TP % beyond 2× MAKER_FEE when cover is on
    tp_exit_assume_taker:         bool  = False    # if True, floor uses maker+taker (TP exits as taker)
    max_hold_min:                 int   = 30      # minutes before forced market close
    # ── circuit breakers ─────────────────────────────────────────────────────
    max_daily_drawdown_pct:       float = 0.03
    max_loss_streak:              int   = 3
    loss_streak_pause_min:        int   = 30
    # ── alerting ─────────────────────────────────────────────────────────────
    periodic_alert_min:           int   = 30   # rolling PnL+fee alert interval
    daily_report_hkt_hour:        int   = 22   # 10 PM HKT daily summary
    ws_startup_wait_sec:          float = 5.0
    dry_run:                      bool  = False
    symbols_csv:                  str   = "symbol_list.csv"   # relative to this package dir

    def __post_init__(self) -> None:
        if self.symbol_leverage is None:
            self.symbol_leverage = {}


_p = Params()
_p.symbol_leverage = {}


def load_params() -> None:
    path = HERE / "params.json"
    with path.open(encoding="utf-8") as fh:
        d = json.load(fh)
    _p.half_spread_pct               = float(d.get("half_spread_pct",               _p.half_spread_pct))
    _p.max_inventory_notional        = float(d.get("max_inventory_notional",         _p.max_inventory_notional))
    _p.inventory_skew_factor         = float(d.get("inventory_skew_factor",          _p.inventory_skew_factor))
    _p.quote_qty_usd                 = float(d.get("quote_qty_usd",                  _p.quote_qty_usd))
    _p.quote_refresh_sec             = float(d.get("quote_refresh_sec",              _p.quote_refresh_sec))
    _p.max_orders_per_sec            = int(d.get("max_orders_per_sec",               _p.max_orders_per_sec))
    _p.max_open_quotes               = int(d.get("max_open_quotes",                  _p.max_open_quotes))
    _p.signal_ob_imbalance_threshold = float(d.get("signal_ob_imbalance_threshold",  _p.signal_ob_imbalance_threshold))
    _p.signal_pressure_threshold     = float(d.get("signal_pressure_threshold",      _p.signal_pressure_threshold))
    _p.leverage_default              = int(d.get("leverage_default",                 _p.leverage_default))
    _p.symbol_leverage               = {str(k): int(v) for k, v in d.get("symbol_leverage", _p.symbol_leverage).items()}
    _p.use_cross_margin              = bool(d.get("use_cross_margin",                _p.use_cross_margin))
    _p.tp_pct                        = float(d.get("tp_pct",                         _p.tp_pct))
    _p.sl_pct                        = float(d.get("sl_pct",                         _p.sl_pct))
    _p.tp_cover_round_trip_fees      = bool(d.get("tp_cover_round_trip_fees",       _p.tp_cover_round_trip_fees))
    _p.tp_fee_buffer_pct             = float(d.get("tp_fee_buffer_pct",              _p.tp_fee_buffer_pct))
    _p.tp_exit_assume_taker          = bool(d.get("tp_exit_assume_taker",           _p.tp_exit_assume_taker))
    _p.max_hold_min                  = int(d.get("max_hold_min",                     _p.max_hold_min))
    _p.max_daily_drawdown_pct        = float(d.get("max_daily_drawdown_pct",         _p.max_daily_drawdown_pct))
    _p.max_loss_streak               = int(d.get("max_loss_streak",                  _p.max_loss_streak))
    _p.loss_streak_pause_min         = int(d.get("loss_streak_pause_min",            _p.loss_streak_pause_min))
    _p.periodic_alert_min            = int(d.get("periodic_alert_min",               _p.periodic_alert_min))
    _p.daily_report_hkt_hour         = int(d.get("daily_report_hkt_hour",            _p.daily_report_hkt_hour))
    _p.ws_startup_wait_sec           = float(d.get("ws_startup_wait_sec",            _p.ws_startup_wait_sec))
    _p.dry_run                       = bool(d.get("dry_run",                         _p.dry_run))
    _p.symbols_csv                   = str(d.get("symbols_csv",                    _p.symbols_csv))

    # Enforce minimum viable spread (break-even = 2 × maker fee)
    min_half = MAKER_FEE
    if _p.half_spread_pct < min_half:
        LOGGER.warning("half_spread_pct %.4f%% < min %.4f%% — clamping",
                       _p.half_spread_pct * 100, min_half * 100)
        _p.half_spread_pct = min_half


def _effective_tp_pct() -> float:
    """TP distance from entry: optional floor so gross move covers VIP0 fees."""
    base = _p.tp_pct
    if not _p.tp_cover_round_trip_fees:
        return base
    if _p.tp_exit_assume_taker:
        floor = MAKER_FEE + TAKER_FEE + _p.tp_fee_buffer_pct
    else:
        floor = 2.0 * MAKER_FEE + _p.tp_fee_buffer_pct
    eff = max(base, floor)
    if eff > base:
        LOGGER.info(
            "tp_fee_recovery: tp_pct raised %.5f%% → %.5f%% (fee floor %.5f%%)",
            base * 100, eff * 100, floor * 100,
        )
    return eff


# ── token bucket rate limiter ─────────────────────────────────────────────────

class TokenBucket:
    """Thread-safe token bucket for rate-limiting API calls."""

    def __init__(self, rate: float) -> None:
        self._rate      = rate      # tokens per second
        self._tokens    = rate
        self._last      = time.monotonic()
        self._lock      = threading.Lock()

    def consume(self, n: float = 1.0) -> None:
        """Block until `n` tokens are available."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last
                self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
            time.sleep(0.05)


# ── instrument helpers ────────────────────────────────────────────────────────

_tick_cache: dict[str, str] = {}


def _tick_size(session: HTTP, symbol: str) -> str:
    if symbol not in _tick_cache:
        try:
            resp = session.get_instruments_info(category="linear", symbol=symbol)
            items = resp.get("result", {}).get("list", [])
            if items:
                _tick_cache[symbol] = items[0]["priceFilter"]["tickSize"]
        except Exception:
            _tick_cache[symbol] = "0.01"
    return _tick_cache.get(symbol, "0.01")


def _qty_step(session: HTTP, symbol: str) -> str:
    key = f"qs_{symbol}"
    if key not in _tick_cache:
        try:
            resp = session.get_instruments_info(category="linear", symbol=symbol)
            items = resp.get("result", {}).get("list", [])
            if items:
                _tick_cache[key] = items[0]["lotSizeFilter"]["qtyStep"]
        except Exception:
            _tick_cache[key] = "0.001"
    return _tick_cache.get(key, "0.001")


def _round_price(price: float, tick: str, up: bool = False) -> str:
    t = Decimal(tick)
    d = Decimal(str(price))
    if up:
        result = (d / t).to_integral_value(rounding=ROUND_UP) * t
    else:
        result = (d / t).to_integral_value(rounding=ROUND_DOWN) * t
    return format(result.normalize(), "f")


def _round_qty(qty: float, step: str) -> str:
    s = Decimal(step)
    d = Decimal(str(qty))
    result = (d / s).to_integral_value(rounding=ROUND_DOWN) * s
    return format(result.normalize(), "f")


def _get_equity(session: HTTP) -> float:
    resp = session.get_wallet_balance(accountType="UNIFIED")
    return float(resp["result"]["list"][0].get("totalEquity") or 0)


def _fetch_fees(session: HTTP, since: datetime, until: datetime) -> float:
    """Sum |execFee| for all linear perpetual executions in [since, until].

    Handles Bybit cursor-based pagination automatically.
    Returns 0.0 on any API error so alert still sends with a note.
    """
    total: float = 0.0
    cursor: str  = ""
    start_ms = int(since.timestamp() * 1000)
    end_ms   = int(until.timestamp() * 1000)
    try:
        while True:
            kwargs: dict = dict(
                category="linear",
                startTime=start_ms,
                endTime=end_ms,
                limit=100,
            )
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
        LOGGER.warning("_fetch_fees [%s→%s]: %s", since.isoformat(), until.isoformat(), exc)
    return total


def _get_positions(session: HTTP) -> dict[str, dict]:
    resp = session.get_positions(category="linear", settleCoin="USDT")
    return {
        item["symbol"]: item
        for item in resp["result"]["list"]
        if float(item.get("size") or 0) > 0
    }


# ── quoting logic ─────────────────────────────────────────────────────────────

class SymbolState:
    """Tracks live quote order IDs and inventory for one symbol."""

    def __init__(self) -> None:
        self.bid_id:          str | None      = None
        self.ask_id:          str | None      = None
        self.net_pos_qty:     float           = 0.0   # refreshed from live positions
        self.entry_price:     float           = 0.0
        # position management
        self.position_open_ts: datetime | None = None  # when this position was first detected
        self.tp_order_id:      str | None      = None  # pending TP limit order


def _quote_symbol(session: HTTP, symbol: str, sig: MarketSignal,
                  st: SymbolState, bucket: TokenBucket) -> None:
    """Cancel old quotes and post fresh bid + ask for one symbol."""
    tick = _tick_size(session, symbol)
    step = _qty_step(session, symbol)
    mid  = sig.mid
    if mid <= 0:
        return

    # ── adverse selection filter ──────────────────────────────────────────────
    # Skip if order book is heavily one-sided (about to move directionally).
    if abs(sig.ob_imbalance) > _p.signal_ob_imbalance_threshold:
        LOGGER.debug("%s: skipping — ob_imbalance %.3f > threshold %.3f",
                     symbol, sig.ob_imbalance, _p.signal_ob_imbalance_threshold)
        _cancel_quotes(session, symbol, st, bucket)
        return

    # ── cancel stale quotes ───────────────────────────────────────────────────
    _cancel_quotes(session, symbol, st, bucket)

    # ── inventory skew ────────────────────────────────────────────────────────
    pos_usd  = st.net_pos_qty * mid
    skew_raw = pos_usd / max(_p.max_inventory_notional, 1.0)
    skew_raw = max(-1.0, min(1.0, skew_raw))
    skew_adj = skew_raw * _p.inventory_skew_factor * _p.half_spread_pct

    # Hard inventory cap — skip new quotes if position too large
    if abs(pos_usd) >= _p.max_inventory_notional:
        LOGGER.info("%s: inventory cap hit (%.2f USD) — no new quotes", symbol, pos_usd)
        return

    bid_price = mid * (1.0 - _p.half_spread_pct - skew_adj)
    ask_price = mid * (1.0 + _p.half_spread_pct - skew_adj)
    qty_raw   = _p.quote_qty_usd / mid
    qty_str   = _round_qty(qty_raw, step)

    if float(qty_str) <= 0:
        return

    bid_str = _round_price(bid_price, tick, up=False)
    ask_str = _round_price(ask_price, tick, up=True)

    if _p.dry_run:
        LOGGER.info("[DRY RUN] %s  bid=%s  ask=%s  qty=%s  imb=%.3f",
                    symbol, bid_str, ask_str, qty_str, sig.ob_imbalance)
        return

    # ── place bid ─────────────────────────────────────────────────────────────
    try:
        bucket.consume(1)
        r = session.place_order(
            category="linear", symbol=symbol,
            side="Buy", orderType="Limit",
            price=bid_str, qty=qty_str,
            timeInForce="PostOnly",
        )
        st.bid_id = r["result"]["orderId"]
    except Exception as exc:
        LOGGER.warning("%s: bid failed: %s", symbol, exc)

    # ── place ask ─────────────────────────────────────────────────────────────
    try:
        bucket.consume(1)
        r = session.place_order(
            category="linear", symbol=symbol,
            side="Sell", orderType="Limit",
            price=ask_str, qty=qty_str,
            timeInForce="PostOnly",
        )
        st.ask_id = r["result"]["orderId"]
    except Exception as exc:
        LOGGER.warning("%s: ask failed: %s", symbol, exc)

    LOGGER.debug("%s  bid=%s  ask=%s  qty=%s  skew=%.4f  imb=%.3f",
                 symbol, bid_str, ask_str, qty_str, skew_adj, sig.ob_imbalance)


def _cancel_quotes(session: HTTP, symbol: str,
                   st: SymbolState, bucket: TokenBucket) -> None:
    for oid_attr in ("bid_id", "ask_id"):
        oid = getattr(st, oid_attr)
        if oid:
            try:
                bucket.consume(1)
                session.cancel_order(category="linear", symbol=symbol, orderId=oid)
            except Exception:
                pass
            setattr(st, oid_attr, None)


def _get_leverage(symbol: str) -> int:
    """Return configured leverage for a symbol, falling back to default."""
    return _p.symbol_leverage.get(symbol, _p.leverage_default)


def _setup_symbol(session: HTTP, symbol: str, bucket: TokenBucket) -> None:
    """Set cross/isolated margin mode and leverage for a symbol on startup.

    Bybit requires margin mode and leverage to be set together when switching
    to cross margin.  Errors are logged but never fatal — the bot continues
    using whatever the account currently has set.
    """
    lev = _get_leverage(symbol)
    lev_str = str(lev)
    mode_str = "cross" if _p.use_cross_margin else "isolated"
    trade_mode = 0 if _p.use_cross_margin else 1

    if _p.dry_run:
        LOGGER.info("[DRY RUN] %s: would set %s margin leverage=%dx", symbol, mode_str, lev)
        return

    # Switch margin mode (cross/isolated) — must provide leverage values.
    # Skipped silently for Unified Trading Accounts (UTA, error 100028).
    try:
        bucket.consume(1)
        session.switch_margin_mode(
            category="linear", symbol=symbol,
            tradeMode=trade_mode,
            buyLeverage=lev_str, sellLeverage=lev_str,
        )
        LOGGER.info("%s: margin mode → %s", symbol, mode_str)
    except Exception as exc:
        err_str = str(exc)
        if "100028" in err_str:
            LOGGER.debug("%s: UTA account — skipping per-symbol margin mode switch", symbol)
        else:
            LOGGER.debug("%s: switch_margin_mode: %s", symbol, exc)

    # Set leverage explicitly (needed even when mode doesn't change).
    try:
        bucket.consume(1)
        session.set_leverage(
            category="linear", symbol=symbol,
            buyLeverage=lev_str, sellLeverage=lev_str,
        )
        LOGGER.info("%s: leverage → %dx", symbol, lev)
    except Exception as exc:
        err_str = str(exc)
        if "110043" in err_str:
            LOGGER.debug("%s: leverage already at %dx — no change needed", symbol, lev)
        else:
            LOGGER.warning("%s: set_leverage failed: %s", symbol, exc)


def _set_native_tp_sl(session: HTTP, symbol: str, st: SymbolState,
                      bucket: TokenBucket) -> None:
    """Place exchange-level TP and SL via set_trading_stop immediately when a
    position opens.

    Using native stops means they execute at the exchange even if the bot
    disconnects.  The polling _manage_position still handles max-hold timeout
    and acts as an emergency backup.
    """
    if st.entry_price <= 0 or st.net_pos_qty == 0.0:
        return

    is_long = st.net_pos_qty > 0
    tick    = _tick_cache.get(symbol, "0.01")
    tp_pct  = _effective_tp_pct()

    if is_long:
        tp_price = _round_price(st.entry_price * (1.0 + tp_pct), tick, up=True)
        sl_price = _round_price(st.entry_price * (1.0 - _p.sl_pct), tick, up=False)
    else:
        tp_price = _round_price(st.entry_price * (1.0 - tp_pct), tick, up=False)
        sl_price = _round_price(st.entry_price * (1.0 + _p.sl_pct), tick, up=True)

    direction = "Long" if is_long else "Short"
    LOGGER.info(
        "%s: setting native TP=%s SL=%s | entry=%.4f %s",
        symbol, tp_price, sl_price, st.entry_price, direction,
    )

    if _p.dry_run:
        LOGGER.info("[DRY RUN] %s: would call set_trading_stop TP=%s SL=%s",
                    symbol, tp_price, sl_price)
        return

    try:
        bucket.consume(1)
        session.set_trading_stop(
            category="linear", symbol=symbol,
            takeProfit=tp_price,
            stopLoss=sl_price,
            tpTriggerBy="MarkPrice",
            slTriggerBy="MarkPrice",
            positionIdx=0,
        )
        LOGGER.info("%s: native TP/SL confirmed on exchange", symbol)
    except Exception as exc:
        LOGGER.warning(
            "%s: set_trading_stop failed: %s — polling fallback active",
            symbol, exc,
        )


def _cancel_all_symbols(session: HTTP, states: dict[str, SymbolState],
                        bucket: TokenBucket) -> None:
    LOGGER.info("Cancelling all quotes …")
    for sym, st in states.items():
        _cancel_quotes(session, sym, st, bucket)
    try:
        bucket.consume(1)
        session.cancel_all_orders(category="linear", settleCoin="USDT")
        LOGGER.info("cancel_all_orders sent.")
    except Exception as exc:
        LOGGER.warning("cancel_all_orders failed: %s", exc)


def _market_close(session: HTTP, symbol: str, st: SymbolState,
                  bucket: TokenBucket, reason: str) -> bool:
    """Place an IOC market order to flatten the position.  Returns True on success."""
    qty = abs(st.net_pos_qty)
    if qty <= 0:
        return False
    close_side = "Sell" if st.net_pos_qty > 0 else "Buy"
    LOGGER.warning("%s: %s — market close %s qty=%.6f entry=%.4f",
                   symbol, reason, close_side, qty, st.entry_price)
    if _p.dry_run:
        LOGGER.info("[DRY RUN] %s: would market-close %s qty=%.6f", symbol, close_side, qty)
        return True
    try:
        bucket.consume(1)
        session.place_order(
            category="linear", symbol=symbol,
            side=close_side, orderType="Market",
            qty=str(qty), timeInForce="IOC",
            reduceOnly=True,
        )
        st.net_pos_qty  = 0.0
        st.entry_price  = 0.0
        st.position_open_ts = None
        st.tp_order_id  = None
        return True
    except Exception as exc:
        LOGGER.error("%s: market close failed: %s", symbol, exc)
        return False


def _manage_position(session: HTTP, symbol: str, st: SymbolState,
                     sig: MarketSignal, bucket: TokenBucket,
                     now: datetime) -> bool:
    """Backup position management — runs every cycle as a safety net.

    Primary TP/SL are handled natively on the exchange via set_trading_stop
    (called immediately when a position opens).  This function handles:
      1. max_hold_min timeout  — exchange has no time-based stops.
      2. Emergency backup SL   — fires at 2× sl_pct in case the native stop
                                 somehow did not execute (e.g. extreme gap).

    Returns True if a market-close was issued this cycle.
    """
    if st.net_pos_qty == 0.0 or st.entry_price <= 0:
        return False

    mid     = sig.mid
    is_long = st.net_pos_qty > 0
    pnl_pct = ((mid - st.entry_price) / st.entry_price) if is_long \
              else ((st.entry_price - mid) / st.entry_price)

    # ── Max-hold timeout (exchange cannot do time-based stops) ────────────────
    if st.position_open_ts is not None:
        age_min = (now - st.position_open_ts).total_seconds() / 60.0
        if age_min >= _p.max_hold_min:
            _cancel_quotes(session, symbol, st, bucket)
            _market_close(session, symbol, st, bucket,
                          f"max-hold {age_min:.0f}m >= {_p.max_hold_min}m")
            return True

    # ── Emergency backup SL (2× threshold — native stop should fire first) ───
    emergency_sl = _p.sl_pct * 2.0
    if pnl_pct <= -emergency_sl:
        _cancel_quotes(session, symbol, st, bucket)
        _market_close(session, symbol, st, bucket,
                      f"emergency SL pnl={pnl_pct*100:.2f}% (native stop missed)")
        return True

    LOGGER.debug("%s: pos=%.6f entry=%.4f mid=%.4f pnl=%+.2f%%  age=%.0fm",
                 symbol, st.net_pos_qty, st.entry_price, mid, pnl_pct * 100,
                 (now - st.position_open_ts).total_seconds() / 60.0
                 if st.position_open_ts else 0)
    return False


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    load_dotenv()
    load_params()

    parser = argparse.ArgumentParser(description="experiment_v6: MM strategy")
    parser.add_argument("--debug",    action="store_true")
    parser.add_argument("--dry-run",  action="store_true")
    parser.add_argument("--once",     action="store_true",
                        help="Run one quoting cycle and exit")
    args = parser.parse_args()

    from utils.logging_setup import setup_logging
    setup_logging("experiment_v6", debug=args.debug)

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

    # ── start market data feed ────────────────────────────────────────────────
    _sym_csv = Path(_p.symbols_csv)
    if not _sym_csv.is_absolute():
        _sym_csv = HERE / _sym_csv
    md.start_market_data(csv_path=_sym_csv)
    LOGGER.info("Waiting %.0fs for WS to populate …", _p.ws_startup_wait_sec)
    time.sleep(_p.ws_startup_wait_sec)

    store       = md.get_signal_store()
    symbols     = store.symbols()
    if not symbols:
        from market_data.websocket_client import _load_symbols
        symbols = _load_symbols(_sym_csv)
    LOGGER.info("experiment_v6 started | %d symbols | dry_run=%s",
                len(symbols), _p.dry_run)

    states:     dict[str, SymbolState] = {s: SymbolState() for s in symbols}
    bucket      = TokenBucket(rate=float(_p.max_orders_per_sec))

    # ── configure leverage and margin mode per symbol ─────────────────────────
    LOGGER.info("Configuring leverage / margin mode for %d symbols …", len(symbols))
    for sym in symbols:
        try:
            _setup_symbol(session, sym, bucket)
        except Exception as exc:
            LOGGER.warning("%s: setup failed: %s", sym, exc)

    # ── shutdown handler ──────────────────────────────────────────────────────
    _shutdown = threading.Event()

    def _on_shutdown(signum, frame):  # noqa: ANN001
        LOGGER.info("Shutdown signal %s — cancelling all quotes …", signum)
        _cancel_all_symbols(session, states, bucket)
        md.stop_market_data()
        _shutdown.set()

    signal.signal(signal.SIGTERM, _on_shutdown)
    signal.signal(signal.SIGINT,  _on_shutdown)

    # ── state ─────────────────────────────────────────────────────────────────
    daily_start_equity: float | None = None
    daily_date:         str          = ""
    loss_streak:        int          = 0
    loss_streak_pause_until: datetime | None = None
    prev_positions:     dict[str, dict] = {}

    # ── alert state ───────────────────────────────────────────────────────────
    # 30-min rolling alert — window starts when bot starts
    _now0               = datetime.now(tz=UTC)
    periodic_alert_ts:  datetime = _now0   # start of current window
    periodic_alert_eq:  float    = 0.0     # equity at window start (set on first equity fetch)
    # Daily report — "YYYY-MM-DD HH" in HKT; prevents duplicate sends within same hour
    daily_report_sent_slot: str  = ""

    try:
        while not _shutdown.is_set():
            now = datetime.now(tz=UTC)
            load_params()   # hot reload

            # ── daily drawdown snapshot ───────────────────────────────────────
            today = now.strftime("%Y-%m-%d")
            if daily_date != today:
                try:
                    daily_start_equity = _get_equity(session)
                    daily_date = today
                    LOGGER.info("New day — equity snapshot: %.4f USDT", daily_start_equity)
                except Exception:
                    pass

            if daily_start_equity and daily_start_equity > 0:
                try:
                    curr_eq = _get_equity(session)
                    # Seed periodic alert equity on first successful fetch
                    if periodic_alert_eq == 0.0:
                        periodic_alert_eq = curr_eq
                    dd = (daily_start_equity - curr_eq) / daily_start_equity
                    if dd >= _p.max_daily_drawdown_pct:
                        LOGGER.warning(
                            "Daily drawdown %.1f%% ≥ limit %.0f%% — halting",
                            dd * 100, _p.max_daily_drawdown_pct * 100)
                        _cancel_all_symbols(session, states, bucket)
                        time.sleep(_p.quote_refresh_sec)
                        continue
                except Exception:
                    pass

            # ── 30-min rolling PnL + fee alert ───────────────────────────────
            elapsed_min = (now - periodic_alert_ts).total_seconds() / 60.0
            if alerter and elapsed_min >= _p.periodic_alert_min and periodic_alert_eq > 0:
                try:
                    curr_eq   = _get_equity(session)
                    pnl_30    = curr_eq - periodic_alert_eq
                    pnl_pct   = (pnl_30 / periodic_alert_eq * 100) if periodic_alert_eq > 0 else 0.0
                    fees_30   = _fetch_fees(session, periodic_alert_ts, now)
                    icon      = "📈" if pnl_30 >= 0 else "📉"
                    open_cnt  = sum(1 for s in states.values() if s.net_pos_qty != 0.0)
                    alerter.send(
                        f"{icon} <b>experiment_v6</b> — {_p.periodic_alert_min}min Update\n"
                        f"PnL ({_p.periodic_alert_min}m): <b>{pnl_30:+.4f} USDT</b>"
                        f" ({pnl_pct:+.2f}%)\n"
                        f"Fees ({_p.periodic_alert_min}m): {fees_30:.4f} USDT\n"
                        f"Balance: {curr_eq:.4f} USDT\n"
                        f"Open positions: {open_cnt}"
                    )
                    LOGGER.info(
                        "Periodic alert sent | pnl=%+.4f fees=%.4f balance=%.4f",
                        pnl_30, fees_30, curr_eq,
                    )
                    periodic_alert_ts = now
                    periodic_alert_eq = curr_eq
                except Exception as exc:
                    LOGGER.warning("Periodic alert failed: %s", exc)

            # ── daily summary at daily_report_hkt_hour HKT ────────────────────
            now_hkt   = now.astimezone(HKT)
            hkt_slot  = now_hkt.strftime("%Y-%m-%d %H")   # e.g. "2026-03-26 22"
            if (alerter
                    and daily_start_equity
                    and now_hkt.hour == _p.daily_report_hkt_hour
                    and now_hkt.minute < 5          # 5-min window to catch the hour
                    and hkt_slot != daily_report_sent_slot):
                try:
                    curr_eq  = _get_equity(session)
                    pnl_day  = curr_eq - daily_start_equity
                    pnl_pct  = (pnl_day / daily_start_equity * 100) if daily_start_equity > 0 else 0.0
                    day_start_dt = datetime.strptime(daily_date, "%Y-%m-%d").replace(tzinfo=UTC)
                    fees_day = _fetch_fees(session, day_start_dt, now)
                    icon     = "📈" if pnl_day >= 0 else "📉"
                    alerter.send(
                        f"{icon} <b>experiment_v6</b> — Daily Summary\n"
                        f"({now_hkt.strftime('%Y-%m-%d')} {_p.daily_report_hkt_hour}:00 HKT)\n"
                        f"PnL (today): <b>{pnl_day:+.4f} USDT</b> ({pnl_pct:+.2f}%)\n"
                        f"Fees (today): {fees_day:.4f} USDT\n"
                        f"Balance: {curr_eq:.4f} USDT\n"
                        f"Day start: {daily_start_equity:.4f} USDT"
                    )
                    LOGGER.info(
                        "Daily report sent | pnl=%+.4f fees=%.4f balance=%.4f",
                        pnl_day, fees_day, curr_eq,
                    )
                    daily_report_sent_slot = hkt_slot
                except Exception as exc:
                    LOGGER.warning("Daily report failed: %s", exc)

            # ── loss-streak pause ─────────────────────────────────────────────
            if loss_streak_pause_until and now < loss_streak_pause_until:
                rem = int((loss_streak_pause_until - now).total_seconds() / 60) + 1
                LOGGER.info("Loss-streak pause — %d min remaining", rem)
                time.sleep(_p.quote_refresh_sec)
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
                time.sleep(_p.quote_refresh_sec)
                continue

            # Detect newly closed positions (for loss-streak tracking)
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
                    else:
                        loss_streak += 1
                        if loss_streak >= _p.max_loss_streak and not _p.dry_run:
                            loss_streak_pause_until = now + timedelta(
                                minutes=_p.loss_streak_pause_min)
                            LOGGER.warning(
                                "Loss streak %d — pausing %d min",
                                loss_streak, _p.loss_streak_pause_min)
                            try:
                                alerter.send(
                                    f"🚫 <b>experiment_v6</b>\n"
                                    f"<b>{loss_streak} consecutive losses</b>\n"
                                    f"Pausing {_p.loss_streak_pause_min} min "
                                    f"(until {loss_streak_pause_until.strftime('%H:%M UTC')})"
                                )
                            except Exception:
                                pass

            # Update per-symbol position state and stamp open timestamp
            for sym in symbols:
                pos = live_positions.get(sym)
                st  = states[sym]
                if pos:
                    new_qty = float(pos.get("size") or 0)
                    if pos.get("side") == "Sell":
                        new_qty = -new_qty
                    # Stamp open time and set native TP/SL when a new position appears
                    is_new_position = st.net_pos_qty == 0.0 and new_qty != 0.0
                    st.net_pos_qty = new_qty
                    st.entry_price = float(pos.get("avgPrice") or 0)
                    if is_new_position:
                        st.position_open_ts = now
                        LOGGER.info("%s: position opened — size=%.6f entry=%.4f  lev=%dx",
                                    sym, new_qty, st.entry_price, _get_leverage(sym))
                        try:
                            _set_native_tp_sl(session, sym, st, bucket)
                        except Exception as exc:
                            LOGGER.warning("%s: _set_native_tp_sl error: %s", sym, exc)
                else:
                    if st.net_pos_qty != 0.0:
                        LOGGER.info("%s: position closed (external fill / TP resolved)", sym)
                    st.net_pos_qty      = 0.0
                    st.entry_price      = 0.0
                    st.position_open_ts = None
                    st.tp_order_id      = None

            prev_positions = live_positions

            # ── position management (TP / SL / timeout) ──────────────────────
            for sym in symbols:
                sig = store.get(sym)
                if sig is None or sig.mid <= 0:
                    continue
                st = states[sym]
                if st.net_pos_qty != 0.0:
                    try:
                        _manage_position(session, sym, st, sig, bucket, now)
                    except Exception as exc:
                        LOGGER.warning("%s: position management error: %s", sym, exc)

            # ── quote refresh ─────────────────────────────────────────────────
            for sym in symbols:
                sig = store.get(sym)
                if sig is None or sig.mid <= 0:
                    LOGGER.debug("%s: no market data yet — skip", sym)
                    continue
                st = states[sym]
                try:
                    _quote_symbol(session, sym, sig, st, bucket)
                except Exception as exc:
                    LOGGER.warning("%s: quoting error: %s", sym, exc)

            if args.once:
                break

            time.sleep(_p.quote_refresh_sec)

    except KeyboardInterrupt:
        pass
    finally:
        _cancel_all_symbols(session, states, bucket)
        md.stop_market_data()
        LOGGER.info("experiment_v6 stopped.")


if __name__ == "__main__":
    main()
