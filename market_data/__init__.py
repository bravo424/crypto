"""
market_data — live Bybit WebSocket market data engine.

Usage
-----
  from pathlib import Path
  from market_data import get_signal_store, start_market_data, stop_market_data

  # Start WebSocket feed (pass each strategy's symbol_list.csv)
  start_market_data(csv_path=Path("strategies/experiment_v7/symbol_list.csv"))

  # Read latest signals for a symbol anywhere in the process
  sig = get_signal_store().get("BTCUSDT")
  if sig:
      print(sig.ob_imbalance, sig.trend_bias)

  # Graceful shutdown
  stop_market_data()
"""
from __future__ import annotations

from pathlib import Path

from market_data.signal_store import SignalStore, _global_store
from market_data.websocket_client import BybitWSClient

_client: BybitWSClient | None = None


def get_signal_store() -> SignalStore:
    """Return the process-global signal store (always safe to call)."""
    return _global_store


def start_market_data(
    symbols: list[str] | None = None,
    csv_path: str | Path | None = None,
) -> None:
    """Start the WebSocket feed in a daemon background thread.

    If `symbols` is provided, subscribes to those only.
    Else if `csv_path` is set, reads active rows from that CSV.
    Else falls back to ``market_data/symbol_list.csv``.

    Safe to call multiple times — only starts once (first call wins).
    """
    global _client
    if _client is not None and _client.is_running():
        return
    p = Path(csv_path) if csv_path else None
    _client = BybitWSClient(symbols=symbols, csv_path=p)
    _client.start()


def stop_market_data() -> None:
    """Gracefully stop the WebSocket feed."""
    global _client
    if _client is not None:
        _client.stop()
        _client = None
