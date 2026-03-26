"""Bybit WebSocket v5 public linear feed manager.

Subscribes to `orderbook.50.<symbol>` and `publicTrade.<symbol>` for each
active symbol in symbol_list.csv.

VIP 0 limits
  - Max 10 WebSocket connections per IP.
  - Max 10 topics per connection (public feed).
  - We use up to 2 connections: each handles up to 10 topics.
    8 symbols × 2 topics = 16 topics → 2 connections of 8 topics each.

Each connection runs in its own daemon thread.
"""
from __future__ import annotations

import csv
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable

import websocket  # websocket-client library

from market_data.book_manager import BookManager
from market_data.signal_engine import SignalEngine
from market_data.signal_store import _global_store
from market_data.trade_flow import TradeFlow

LOGGER = logging.getLogger(__name__)

_WS_URL   = "wss://stream.bybit.com/v5/public/linear"
_PING_INTERVAL = 20          # seconds — Bybit requires ping within 30s
_MAX_TOPICS    = 10          # per connection, VIP 0 public feed
_RECONNECT_BASE = 2.0        # seconds, doubled each retry
_RECONNECT_MAX  = 60.0

HERE = Path(__file__).resolve().parent


def _load_symbols(csv_path: Path | None = None) -> list[str]:
    path = csv_path or HERE / "symbol_list.csv"
    syms: list[str] = []
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("active", "true").strip().lower() in ("true", "1", "yes"):
                syms.append(row["symbol"].strip())
    return syms


class _SingleConnection(threading.Thread):
    """One WebSocket connection managing up to _MAX_TOPICS topics."""

    def __init__(self, topics: list[str], book: BookManager,
                 flow: TradeFlow, engine: SignalEngine,
                 name: str = "bybit-ws") -> None:
        super().__init__(name=name, daemon=True)
        self._topics  = topics
        self._book    = book
        self._flow    = flow
        self._engine  = engine
        self._ws: websocket.WebSocketApp | None = None
        self._stop_event  = threading.Event()
        self._connected   = threading.Event()
        self._msg_counter = 0          # counts market data messages received

    def run(self) -> None:
        delay = _RECONNECT_BASE
        while not self._stop_event.is_set():
            try:
                self._ws = websocket.WebSocketApp(
                    _WS_URL,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self._ws.run_forever(ping_interval=_PING_INTERVAL,
                                     ping_timeout=10)
            except Exception as exc:
                LOGGER.warning("%s: connection error: %s", self.name, exc)
            if not self._stop_event.is_set():
                LOGGER.info("%s: reconnecting in %.0fs …", self.name, delay)
                time.sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX)
            else:
                break

    def stop(self) -> None:
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def is_connected(self) -> bool:
        return self._connected.is_set()

    # ── WebSocket callbacks ───────────────────────────────────────────────────

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        self._connected.set()
        LOGGER.info("%s: connected — subscribing %d topics", self.name, len(self._topics))
        ws.send(json.dumps({"op": "subscribe", "args": self._topics}))

    def _on_message(self, ws: websocket.WebSocketApp, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return

        # Heartbeat / subscription confirmation
        if "op" in msg:
            if msg.get("op") == "subscribe":
                ok = msg.get("success", False)
                if ok:
                    LOGGER.info("%s: subscription confirmed — %s",
                                self.name, msg.get("ret_msg", "ok"))
                else:
                    LOGGER.warning("%s: subscription FAILED — %s",
                                   self.name, msg.get("ret_msg", "unknown"))
            return

        topic: str = msg.get("topic", "")
        data        = msg.get("data", {})
        msg_type    = msg.get("type", "")

        self._msg_counter += 1
        if self._msg_counter % 1000 == 0:
            LOGGER.info("%s: ✓ %d market-data messages received", self.name, self._msg_counter)

        if topic.startswith("orderbook."):
            symbol = topic.split(".")[-1]
            bids   = data.get("b", [])
            asks   = data.get("a", [])
            if msg_type == "snapshot":
                self._book.on_snapshot(symbol, bids, asks)
            else:
                self._book.on_delta(symbol, bids, asks)
            self._engine.refresh(symbol)

        elif topic.startswith("publicTrade."):
            symbol = topic.split(".")[-1]
            for trade in (data if isinstance(data, list) else [data]):
                side  = trade.get("S") or trade.get("side", "Buy")
                qty   = float(trade.get("v") or trade.get("size", 0))
                price = float(trade.get("p") or trade.get("price", 0))
                ts_ms = int(trade.get("T") or trade.get("ts", 0))
                ts    = ts_ms / 1000.0 if ts_ms else time.time()
                self._flow.on_trade(symbol, side, qty, price, ts)
            self._engine.refresh(symbol)

    def _on_error(self, ws: websocket.WebSocketApp, err: Exception) -> None:
        LOGGER.warning("%s: WS error: %s", self.name, err)
        self._connected.clear()

    def _on_close(self, ws: websocket.WebSocketApp, code, msg) -> None:
        LOGGER.info("%s: WS closed (%s %s)", self.name, code, msg)
        self._connected.clear()


class BybitWSClient:
    """Manages multiple _SingleConnection threads — one per batch of topics."""

    def __init__(self, symbols: list[str] | None = None,
                 csv_path: Path | None = None) -> None:
        self._symbols = symbols or _load_symbols(csv_path)
        self._book   = BookManager(depth=5)
        self._flow   = TradeFlow()
        self._engine = SignalEngine(self._book, self._flow, _global_store)
        self._conns: list[_SingleConnection] = []
        self._running = False

    def start(self) -> None:
        if self._running:
            return

        # Build topic list: orderbook.50 + publicTrade per symbol
        all_topics: list[str] = []
        for sym in self._symbols:
            all_topics.append(f"orderbook.50.{sym}")
            all_topics.append(f"publicTrade.{sym}")

        # Split into batches of _MAX_TOPICS per connection
        batches: list[list[str]] = []
        for i in range(0, len(all_topics), _MAX_TOPICS):
            batches.append(all_topics[i: i + _MAX_TOPICS])

        LOGGER.info("BybitWSClient: starting %d connection(s) for %d symbols",
                    len(batches), len(self._symbols))

        for idx, batch in enumerate(batches):
            conn = _SingleConnection(
                topics=batch,
                book=self._book, flow=self._flow, engine=self._engine,
                name=f"bybit-ws-{idx}",
            )
            conn.start()
            self._conns.append(conn)

        self._running = True

    def stop(self) -> None:
        for conn in self._conns:
            conn.stop()
        self._conns.clear()
        self._running = False
        LOGGER.info("BybitWSClient: stopped.")

    def is_running(self) -> bool:
        return self._running and any(c.is_alive() for c in self._conns)
