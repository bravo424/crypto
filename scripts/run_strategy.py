"""
run_strategy.py — launch any registered strategy by name.

Usage:  run-strat <strategy_name> [strategy args]
E.g.:   run-strat experiment_v1
        run-strat experiment_v1 --once
"""
from __future__ import annotations

import importlib
import os
import signal
import sys
import traceback
from pathlib import Path

import requests
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
_POWER_TOKEN_PATH = _ROOT / "config" / "sra_strat_power_bot_token"


def _notify_power(name: str, *, started: bool, exchange: str = "",
                  exc: BaseException | None = None) -> None:
    """Fire-and-forget Telegram notification to sra_strat_power_bot."""
    if not _POWER_TOKEN_PATH.exists():
        return
    token = _POWER_TOKEN_PATH.read_text(encoding="utf-8").strip()
    chat_id = os.getenv("SRA_STRAT_POWER_BOT_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    label = f"{name}; {exchange}" if exchange else name
    if started:
        icon, action = "🟢", "started"
        text = f"{icon} <b>{label}</b> {action}"
    elif exc is not None and not isinstance(exc, (KeyboardInterrupt, SystemExit)):
        icon = "💥"
        tb = traceback.format_exception_only(type(exc), exc)[-1].strip()
        text = f"{icon} <b>{label}</b> crashed\n<code>{tb[:300]}</code>"
    elif isinstance(exc, KeyboardInterrupt):
        icon = "⏹"
        text = f"{icon} <b>{label}</b> stopped (Ctrl+C)"
    else:
        icon = "🔴"
        text = f"{icon} <b>{label}</b> stopped"
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def _ensure_root_on_path() -> None:
    """Add the project root to sys.path so 'strategies.*' is importable."""
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)


def main() -> None:
    _ensure_root_on_path()
    load_dotenv()

    if len(sys.argv) < 2:
        print("Usage: run-strat <strategy_name> [args...]")
        print()
        print("Available strategies:")
        _list_strategies()
        sys.exit(1)

    name = sys.argv[1].replace("-", "_")
    # Extract --exchange value before handing argv to the strategy
    remaining = sys.argv[2:]
    exchange = ""
    for i, arg in enumerate(remaining):
        if arg == "--exchange" and i + 1 < len(remaining):
            exchange = remaining[i + 1]
            break
        if arg.startswith("--exchange="):
            exchange = arg.split("=", 1)[1]
            break
    # Pass remaining argv to the strategy's own argparse
    sys.argv = [f"run-strat/{name}"] + remaining

    runner_path = Path(__file__).resolve().parent.parent / "strategies" / name / "runner.py"
    if not runner_path.exists():
        print(f"Strategy '{name}' not found under strategies/{name}/runner.py")
        sys.exit(1)

    try:
        module = importlib.import_module(f"strategies.{name}.runner")
    except Exception as exc:
        print(f"Error loading strategy '{name}': {exc}")
        raise

    _notify_power(name, started=True, exchange=exchange)

    # Register signal handlers so stop alert fires on SIGTERM / Windows CTRL_BREAK
    def _on_signal(signum, frame):
        _notify_power(name, started=False, exchange=exchange,
                      exc=KeyboardInterrupt(f"signal {signum}"))
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    if hasattr(signal, "SIGBREAK"):          # Windows only
        signal.signal(signal.SIGBREAK, _on_signal)

    _exc: BaseException | None = None
    try:
        module.main()
    except BaseException as _e:
        _exc = _e
        raise
    finally:
        _notify_power(name, started=False, exchange=exchange, exc=_exc)


def _list_strategies() -> None:
    root = Path(__file__).resolve().parent.parent / "strategies"
    for path in sorted(root.iterdir()):
        if path.is_dir() and (path / "runner.py").exists():
            print(f"  {path.name}")


if __name__ == "__main__":
    main()
