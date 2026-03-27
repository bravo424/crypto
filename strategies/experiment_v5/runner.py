"""
experiment_v5
-------------
Safer v5 profile built on top of experiment_v4 core engine.

Design goals:
- Prioritize capital protection over trade frequency.
- Keep expected TP distance in roughly 1-2% zone.
- Keep SL tighter than TP and enforce strict portfolio brakes.
- Run independently from v4 using v5 params/symbol files/state files.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from strategies.experiment_v4 import runner as core

# ── optional market_data integration ─────────────────────────────────────────
# If the market_data package is installed and the WebSocket feed is running,
# v5 uses live order-book imbalance + trade pressure as an extra confirmation
# gate for entries.  If unavailable the strategy runs unchanged.
try:
    import market_data as _md
    _MD_AVAILABLE = True
except ImportError:
    _md = None          # type: ignore[assignment]
    _MD_AVAILABLE = False

HERE = Path(__file__).resolve().parent

# Make v4 core read v5-local files.
core.HERE = HERE

_orig_load_params = core.load_params
_orig_check_macro_trend = core.check_macro_trend
_last_tuning_snapshot: tuple[float, float, float, float] | None = None
_runtime_overrides: dict[str, object] = {}
_runtime_overrides_logged = False


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _parse_value(raw: str) -> object:
    s = raw.strip()
    low = s.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    try:
        if "." in s or "e" in low:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _clear_pause(exchange: str) -> None:
    """Clear loss-streak pause and reset streak counter in the state file."""
    state_file = Path(f"data/experiment_v5_{exchange}_state.json")
    if not state_file.exists():
        print(f"State file not found: {state_file}")
        return
    state = json.loads(state_file.read_text(encoding="utf-8"))
    was_paused = bool(state.get("loss_streak_pause_until"))
    state["loss_streak_pause_until"] = None
    state["loss_streak"] = 0
    state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                          encoding="utf-8")
    if was_paused:
        print(f"Loss-streak pause cleared for {exchange}. Bot will resume on next cycle.")
    else:
        print(f"No active pause found for {exchange} (loss_streak reset to 0).")


def _parse_cli_overrides() -> None:
    """Consume v5-only CLI flags and leave core flags in sys.argv."""
    global _runtime_overrides
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--set", action="append", default=[])
    parser.add_argument("--clear-pause", action="store_true")
    args, remaining = parser.parse_known_args(sys.argv[1:])

    # Resolve exchange from remaining args so --clear-pause knows which state file.
    _exchange = "bybit"
    for i, arg in enumerate(remaining):
        if arg == "--exchange" and i + 1 < len(remaining):
            _exchange = remaining[i + 1]
            break
        if arg.startswith("--exchange="):
            _exchange = arg.split("=", 1)[1]
            break

    if args.clear_pause:
        _clear_pause(_exchange)
        raise SystemExit(0)

    overrides: dict[str, object] = {}
    for item in args.set:
        if "=" not in item:
            raise SystemExit(f"Invalid --set format: '{item}'. Use --set key=value")
        key, raw = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"Invalid --set key: '{item}'")
        overrides[key] = _parse_value(raw)

    _runtime_overrides = overrides
    sys.argv = [sys.argv[0]] + remaining


def _load_local_params(exchange: str) -> dict:
    """Load v5 params merged with exchange-specific overrides."""
    global _runtime_overrides_logged
    with (HERE / "params.json").open(encoding="utf-8") as fh:
        p = json.load(fh)
    ex = p.get("exchanges", {}).get(exchange, {})
    merged = {**p, **ex}

    # Runtime CLI override format:
    #   --set key=value                         -> applies to all exchanges
    #   --set exchanges.bybit.key=value         -> only when exchange=bybit
    #   --set exchanges.bithumb.key=value       -> only when exchange=bithumb
    if _runtime_overrides:
        for k, v in _runtime_overrides.items():
            prefix = f"exchanges.{exchange}."
            if k.startswith(prefix):
                merged[k[len(prefix):]] = v
            elif not k.startswith("exchanges."):
                merged[k] = v
        if not _runtime_overrides_logged:
            core.LOGGER.info("v5 CLI overrides active: %s", _runtime_overrides)
            _runtime_overrides_logged = True

    return merged


def _apply_entry_tuning(exchange: str) -> None:
    """Apply user-controlled aggressiveness knobs from params.json."""
    global _last_tuning_snapshot
    p = _load_local_params(exchange)

    # 0=strict(default), 1=balanced, 2=active (more entries).
    aggr = int(p.get("entry_aggressiveness", 0))
    aggr = int(_clamp(float(aggr), 0.0, 2.0))

    # Scale volume-spike requirement down as aggressiveness increases.
    # aggr 0: x1.00, aggr 1: x0.90, aggr 2: x0.80
    vmult_scale = float(p.get("vmult_scale", 1.0 - 0.1 * aggr))
    vmult_scale = _clamp(vmult_scale, 0.75, 1.10)

    # Relax RSI extremes toward center as aggressiveness increases.
    # Each step moves ±2 points by default (configurable).
    rsi_step = float(p.get("rsi_relax_step", 2.0))
    rsi_step = _clamp(rsi_step, 0.0, 5.0)

    new_vmult = _clamp(core.VMULT * vmult_scale, 1.2, 3.0)
    new_rsi_ob = _clamp(core.RSI_OB - (rsi_step * aggr), 60.0, 85.0)
    new_rsi_os = _clamp(core.RSI_OS + (rsi_step * aggr), 15.0, 40.0)

    core.VMULT = new_vmult
    core.RSI_OB = new_rsi_ob
    core.RSI_OS = new_rsi_os

    # Optional extra knob for Bollinger sensitivity.
    bb_scale = float(p.get("bb_std_scale", 1.0))
    bb_scale = _clamp(bb_scale, 0.85, 1.15)
    core.BB_STD = _clamp(core.BB_STD * bb_scale, 1.6, 2.5)

    snap = (core.VMULT, core.RSI_OB, core.RSI_OS, core.BB_STD)
    if snap != _last_tuning_snapshot:
        core.LOGGER.info(
            "v5 entry tuning: aggr=%d vmult=%.2f rsi_ob=%.1f rsi_os=%.1f bb_std=%.2f",
            aggr, core.VMULT, core.RSI_OB, core.RSI_OS, core.BB_STD,
        )
        _last_tuning_snapshot = snap


def load_params(exchange: str = "bybit") -> None:
    """Load params then enforce non-negotiable safety caps for v5."""
    # Keep v5 state isolated from v4 regardless of core defaults.
    core.STATE_FILE = Path(f"data/experiment_v5_{exchange}_state.json")
    _orig_load_params(exchange)

    # Conservative risk profile.
    core.LEVERAGE = min(core.LEVERAGE, 3 if exchange == "bybit" else 1)
    core.MAXRISKPCT = min(core.MAXRISKPCT, 0.01)               # <=1% account risk
    core.MAX_OPEN_POSITIONS = min(core.MAX_OPEN_POSITIONS, 2)  # low portfolio exposure
    core.NORDERSPERHOUR = min(core.NORDERSPERHOUR, 6)          # avoid overtrading
    core.MAX_DAILY_DRAWDOWN = min(core.MAX_DAILY_DRAWDOWN, 0.1)

    # Signal quality and guardrails.
    core.VMULT = max(core.VMULT, 1.8)
    core.MIN_ATR_PCT = max(core.MIN_ATR_PCT, 0.0004)
    core.PANIC_DROP_PCT = max(core.PANIC_DROP_PCT, 0.02)
    core.PROFIT_LOCK_TRIGGER_PCT = min(core.PROFIT_LOCK_TRIGGER_PCT, 0.008)
    core.PROFIT_LOCK_SL_PCT = max(core.PROFIT_LOCK_SL_PCT, 0.003)

    # Keep practical trade target around 1-2%.
    core.MAX_TP_PCT = min(core.MAX_TP_PCT, 0.02)
    core.MAX_SL_PCT = min(core.MAX_SL_PCT, 0.012)
    core.MIN_SL_PCT = max(core.MIN_SL_PCT, 0.004)

    # User-controlled signal aggressiveness (from params.json).
    _apply_entry_tuning(exchange)


def check_macro_trend(session, symbol: str) -> str | None:
    """Lenient macro gate: block only on explicit opposite trend."""
    return _orig_check_macro_trend(session, symbol)


def md_signal_gate(symbol: str, side: str) -> bool:
    """Optional live market-data confirmation gate.

    Returns True  → allow entry (or market_data unavailable — fail open).
    Returns False → block entry because live signals oppose the direction.

    Gate logic (requires BOTH to agree if data is available):
      - ob_imbalance must not strongly oppose the intended side.
      - 5m trade pressure must not strongly oppose the intended side.

    Thresholds are intentionally generous so this is a veto-only filter,
    not a required positive confirmation.  If the store has no data for the
    symbol yet, allow the trade (fail open).
    """
    if not _MD_AVAILABLE or _md is None:
        return True
    try:
        store = _md.get_signal_store()
        sig   = store.get(symbol)
        if sig is None:
            return True   # no data yet — allow
        p = _load_local_params("bybit")
        imb_thresh  = float(p.get("md_imbalance_block_thresh",  0.40))
        pres_thresh = float(p.get("md_pressure_block_thresh",   0.30))
        if side == "Buy":
            # Block if book is heavily ask-sided or trade flow is strongly negative
            if sig.ob_imbalance < -imb_thresh and sig.trade_pressure_5m < -pres_thresh:
                core.LOGGER.info(
                    "  %-20s  MD gate: BLOCK Buy — imb=%.3f pres5m=%.3f",
                    symbol, sig.ob_imbalance, sig.trade_pressure_5m,
                )
                return False
        else:  # Sell
            if sig.ob_imbalance > imb_thresh and sig.trade_pressure_5m > pres_thresh:
                core.LOGGER.info(
                    "  %-20s  MD gate: BLOCK Sell — imb=%.3f pres5m=%.3f",
                    symbol, sig.ob_imbalance, sig.trade_pressure_5m,
                )
                return False
    except Exception:
        pass   # never block due to market_data errors
    return True


def calc_tp_sl(side: str, entry: float, atr: float, tick_size: str) -> tuple[str, str]:
    """v5 TP/SL sizing targeting ~1-2% TP with tighter SL."""
    e = Decimal(str(entry))
    tick = Decimal(tick_size)

    # ATR-adaptive ratios with hard caps/floors.
    atr_pct = (atr / entry) if (entry > 0 and atr > 0) else 0.0
    tp_pct = _clamp(atr_pct * 1.8, 0.010, 0.020)
    sl_pct = _clamp(atr_pct * 1.0, 0.006, 0.012)

    # Fee floor so net TP is not eaten by round-trip fees.
    fee_floor = max(2 * float(core.FEE_RATE), 0.0)
    tp_pct = max(tp_pct, fee_floor + 0.003)

    if side == "Buy":
        tp = core._round_down(e * (Decimal("1") + Decimal(str(tp_pct))), tick)
        sl = core._round_down(e * (Decimal("1") - Decimal(str(sl_pct))), tick)
    else:
        tp = core._round_down(e * (Decimal("1") - Decimal(str(tp_pct))), tick)
        sl = core._round_up(e * (Decimal("1") + Decimal(str(sl_pct))), tick)
    return format(tp.normalize(), "f"), format(sl.normalize(), "f")


def main() -> None:
    _parse_cli_overrides()

    # Resolve exchange early so the log file is named correctly.
    _exchange_for_log = "bybit"
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--exchange" and i < len(sys.argv):
            _exchange_for_log = sys.argv[i]
            break
        if arg.startswith("--exchange="):
            _exchange_for_log = arg.split("=", 1)[1]
            break

    _debug_for_log = "--debug" in sys.argv
    from utils.logging_setup import setup_logging
    setup_logging(f"experiment_v5_{_exchange_for_log}", debug=_debug_for_log)

    # Monkeypatch core behavior for v5.
    core.load_params = load_params
    core.check_macro_trend = check_macro_trend
    core.calc_tp_sl = calc_tp_sl

    # Wire live market-data gate if the package is available.
    if _MD_AVAILABLE:
        # Start the WebSocket feed so SignalStore is populated before the first
        # scan cycle.  start_market_data() is idempotent — safe to call even if
        # experiment_v6 is also running in a separate process.
        _md.start_market_data(csv_path=HERE / "symbol_list.csv")  # type: ignore[union-attr]
        core.LOGGER.info("v5: market_data WebSocket feed started")
        core._extra_signal_gate = md_signal_gate  # type: ignore[attr-defined]
        core.LOGGER.info("v5: market_data signal gate enabled")
    else:
        core.LOGGER.info("v5: market_data not installed — running without MD gate")

    core.main()


if __name__ == "__main__":
    main()
