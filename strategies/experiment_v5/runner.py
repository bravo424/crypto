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

import json
from decimal import Decimal
from pathlib import Path

from strategies.experiment_v4 import runner as core

HERE = Path(__file__).resolve().parent

# Make v4 core read v5-local files.
core.HERE = HERE

_orig_load_params = core.load_params
_orig_check_macro_trend = core.check_macro_trend
_last_tuning_snapshot: tuple[float, float, float, float] | None = None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _load_local_params(exchange: str) -> dict:
    """Load v5 params merged with exchange-specific overrides."""
    with (HERE / "params.json").open(encoding="utf-8") as fh:
        p = json.load(fh)
    ex = p.get("exchanges", {}).get(exchange, {})
    return {**p, **ex}


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
    # Monkeypatch core behavior for v5.
    core.load_params = load_params
    core.check_macro_trend = check_macro_trend
    core.calc_tp_sl = calc_tp_sl

    core.main()


if __name__ == "__main__":
    main()
