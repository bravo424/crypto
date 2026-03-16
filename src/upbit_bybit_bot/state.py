from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from upbit_bybit_bot.models import PendingTrade


@dataclass(slots=True)
class BotState:
    seen_notice_ids: set[str] = field(default_factory=set)
    pending_trades: list[PendingTrade] = field(default_factory=list)
    executed_trade_keys: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> "BotState":
        if not path.exists():
            return cls()

        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            seen_notice_ids=set(payload.get("seen_notice_ids", [])),
            pending_trades=[PendingTrade.from_dict(item) for item in payload.get("pending_trades", [])],
            executed_trade_keys=set(payload.get("executed_trade_keys", [])),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "seen_notice_ids": sorted(self.seen_notice_ids),
            "pending_trades": [trade.to_dict() for trade in self.pending_trades],
            "executed_trade_keys": sorted(self.executed_trade_keys),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def add_pending_trade(self, trade: PendingTrade) -> None:
        key = self.make_trade_key(trade.notice_id, trade.asset_symbol)
        if key in self.executed_trade_keys:
            return
        if any(self.make_trade_key(item.notice_id, item.asset_symbol) == key for item in self.pending_trades):
            return
        self.pending_trades.append(trade)

    def mark_notice_seen(self, notice_id: str) -> None:
        self.seen_notice_ids.add(notice_id)

    def mark_trade_executed(self, notice_id: str, asset_symbol: str) -> None:
        self.executed_trade_keys.add(self.make_trade_key(notice_id, asset_symbol))
        self.pending_trades = [
            trade
            for trade in self.pending_trades
            if self.make_trade_key(trade.notice_id, trade.asset_symbol) != self.make_trade_key(notice_id, asset_symbol)
        ]

    def is_notice_seen(self, notice_id: str) -> bool:
        return notice_id in self.seen_notice_ids

    def is_trade_known(self, notice_id: str, asset_symbol: str) -> bool:
        trade_key = self.make_trade_key(notice_id, asset_symbol)
        if trade_key in self.executed_trade_keys:
            return True
        return any(self.make_trade_key(item.notice_id, item.asset_symbol) == trade_key for item in self.pending_trades)

    @staticmethod
    def make_trade_key(notice_id: str, asset_symbol: str) -> str:
        return f"{notice_id}:{asset_symbol}"
