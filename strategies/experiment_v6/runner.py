"""
experiment_v6 — Signal-enhanced market making on Bybit USDT-perp.

Design
------
  1. WebSocket feed (market_data package) streams live order books + trades
     for every symbol in market_data/symbol_list.csv.
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
from datetime import UTC, datetime, timedelta
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
    max_daily_drawdown_pct:       float = 0.03
    max_loss_streak:              int   = 3
    loss_streak_pause_min:        int   = 30
    ws_startup_wait_sec:          float = 5.0
    dry_run:                      bool  = False


_p = Params()


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
    _p.max_daily_drawdown_pct        = float(d.get("max_daily_drawdown_pct",         _p.max_daily_drawdown_pct))
    _p.max_loss_streak               = int(d.get("max_loss_streak",                  _p.max_loss_streak))
    _p.loss_streak_pause_min         = int(d.get("loss_streak_pause_min",            _p.loss_streak_pause_min))
    _p.ws_startup_wait_sec           = float(d.get("ws_startup_wait_sec",            _p.ws_startup_wait_sec))
    _p.dry_run                       = bool(d.get("dry_run",                         _p.dry_run))

    # Enforce minimum viable spread (break-even = 2 × maker fee)
    min_half = MAKER_FEE
    if _p.half_spread_pct < min_half:
        LOGGER.warning("half_spread_pct %.4f%% < min %.4f%% — clamping",
                       _p.half_spread_pct * 100, min_half * 100)
        _p.half_spread_pct = min_half


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
        self.bid_id:      str | None = None
        self.ask_id:      str | None = None
        self.net_pos_qty: float      = 0.0   # refreshed from live positions
        self.entry_price: float      = 0.0


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
    md.start_market_data()
    LOGGER.info("Waiting %.0fs for WS to populate …", _p.ws_startup_wait_sec)
    time.sleep(_p.ws_startup_wait_sec)

    store       = md.get_signal_store()
    symbols     = store.symbols()
    if not symbols:
        # Fall back to CSV if WS not yet populated
        from market_data.websocket_client import _load_symbols
        symbols = _load_symbols()
    LOGGER.info("experiment_v6 started | %d symbols | dry_run=%s",
                len(symbols), _p.dry_run)

    states:     dict[str, SymbolState] = {s: SymbolState() for s in symbols}
    bucket      = TokenBucket(rate=float(_p.max_orders_per_sec))

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

            # Update per-symbol position state
            for sym in symbols:
                pos = live_positions.get(sym)
                st  = states[sym]
                if pos:
                    st.net_pos_qty = float(pos.get("size") or 0)
                    if pos.get("side") == "Sell":
                        st.net_pos_qty = -st.net_pos_qty
                    st.entry_price = float(pos.get("avgPrice") or 0)
                else:
                    st.net_pos_qty = 0.0
                    st.entry_price = 0.0

            prev_positions = live_positions

            # ── quote refresh ─────────────────────────────────────────────────
            for sym in symbols:
                sig = store.get(sym)
                if sig is None or sig.mid <= 0:
                    LOGGER.debug("%s: no market data yet — skip", sym)
                    continue
                try:
                    _quote_symbol(session, sym, sig, states[sym], bucket)
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
