"""
market_data — live Bybit WebSocket market data engine.

Usage
-----
  from market_data import get_signal_store, start_market_data, stop_market_data

  # Start WebSocket feed in background thread
  start_market_data()

  # Read latest signals for a symbol anywhere in the process
  sig = get_signal_store().get("BTCUSDT")
  if sig:
      print(sig.ob_imbalance, sig.trend_bias)

  # Graceful shutdown
  stop_market_data()
"""
from __future__ import annotations

from market_data.signal_store import SignalStore, _global_store
from market_data.websocket_client import BybitWSClient

_client: BybitWSClient | None = None


def get_signal_store() -> SignalStore:
    """Return the process-global signal store (always safe to call)."""
    return _global_store


def start_market_data(symbols: list[str] | None = None) -> None:
    """Start the WebSocket feed in a daemon background thread.

    If `symbols` is None, reads from market_data/symbol_list.csv.
    Safe to call multiple times — only starts once.
    """
    global _client
    if _client is not None and _client.is_running():
        return
    _client = BybitWSClient(symbols=symbols)
    _client.start()


def stop_market_data() -> None:
    """Gracefully stop the WebSocket feed."""
    global _client
    if _client is not None:
        _client.stop()
        _client = None
