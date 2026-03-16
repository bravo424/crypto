from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class NoticeSummary:
    notice_id: str
    title: str
    url: str
    published_at: datetime | None = None


@dataclass(slots=True)
class ListingSignal:
    notice_id: str
    title: str
    url: str
    asset_symbol: str
    listing_at: datetime | None = None
    detected_at: datetime | None = None


@dataclass(slots=True)
class BybitInstrument:
    symbol: str
    status: str
    is_pre_listing: bool
    launch_at: datetime | None
    continuous_trading_at: datetime | None
    qty_step: str
    min_order_qty: str
    min_notional_value: str
    last_price: str | None = None


@dataclass(slots=True)
class PendingTrade:
    notice_id: str
    asset_symbol: str
    bybit_symbol: str
    execute_at: str
    source_title: str
    source_url: str
    listing_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PendingTrade":
        return cls(**payload)
