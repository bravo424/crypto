"""Bithumb REST API adapter for experiment_v1.

Bithumb v1 REST API is structurally identical to Upbit v1 API.  Same JWT
auth scheme, same endpoints, same response shapes.  Differences:
  - Base URL  : https://api.bithumb.com/v1   (vs api.upbit.com/v1)
  - JWT needs a timestamp field (ms since epoch) in addition to the nonce.

Mimics the pybit HTTP interface so runner.py works unchanged for all exchanges.

KRW market notes:
  - Long-only (Buy = open, Sell = close).
  - No leverage, no native TP/SL.
  - Symbol format in symbols_bithumb.json: XRP/KRW  ->  Bithumb market KRW-XRP.
  - All prices normalised to USDT so runner.py sees consistent values.
    place_order() converts back to KRW before sending the request.
"""
from __future__ import annotations

import hashlib
import time
import uuid
import urllib.parse

import jwt        # PyJWT >= 2.x
import requests
from datetime import datetime, timezone

BITHUMB_BASE = "https://api.bithumb.com/v1"

# KRW/USDT rate cache
_krw_rate_cache: tuple[float, float] | None = None   # (rate, timestamp)
_KRW_RATE_TTL = 60.0
_KRW_RATE_FALLBACK = 1450.0


class BithumbSession:
    def __init__(self, access_key: str, secret_key: str) -> None:
        self.access_key = access_key
        self.secret_key = secret_key

    # ---- symbol / market helpers ---------------------------------------------

    @staticmethod
    def to_market(symbol: str) -> str:
        """XRP/KRW -> KRW-XRP  |  BTCUSDT -> USDT-BTC"""
        if "/" in symbol:
            base, quote = symbol.split("/", 1)
            return f"{quote}-{base}"
        if symbol.endswith("USDT"):
            return f"USDT-{symbol[:-4]}"
        return symbol

    @staticmethod
    def _is_krw_market(market: str) -> bool:
        return market.startswith("KRW-")

    @staticmethod
    def _tick_for_usdt_price(price_usdt: float) -> str:
        """Return a sensible USDT tick-size string."""
        if price_usdt >= 10_000:  return "1"
        if price_usdt >= 1_000:   return "0.1"
        if price_usdt >= 100:     return "0.01"
        if price_usdt >= 10:      return "0.001"
        if price_usdt >= 1:       return "0.0001"
        if price_usdt >= 0.1:     return "0.00001"
        if price_usdt >= 0.01:    return "0.000001"
        return "0.00000001"

    @staticmethod
    def _krw_tick(krw_price: float) -> int:
        """Return Bithumb\'s official KRW tick unit (호가단위) for a given KRW price level."""
        if krw_price >= 2_000_000:   return 1_000
        if krw_price >= 1_000_000:   return 500
        if krw_price >= 500_000:     return 100
        if krw_price >= 100_000:     return 50
        if krw_price >= 10_000:      return 10
        if krw_price >= 1_000:       return 5
        if krw_price >= 100:         return 1
        return 1  # < 100 KRW: 1 KRW is fine

    # ---- KRW/USDT rate -------------------------------------------------------

    def _krw_usdt_rate(self) -> float:
        """Return cached KRW-per-USDT rate, refreshed every 60 s."""
        global _krw_rate_cache
        now = time.time()
        if _krw_rate_cache and now - _krw_rate_cache[1] < _KRW_RATE_TTL:
            return _krw_rate_cache[0]
        try:
            resp = requests.get(
                f"{BITHUMB_BASE}/ticker",
                params={"markets": "KRW-USDT"},
                timeout=5,
            )
            if resp.ok:
                data = resp.json()
                if isinstance(data, list) and data:
                    rate = float(data[0]["trade_price"])
                    _krw_rate_cache = (rate, now)
                    return rate
        except Exception:
            pass
        return _KRW_RATE_FALLBACK

    # ---- JWT auth ------------------------------------------------------------

    def _make_jwt(self, params: dict | None = None) -> str:
        """Build a signed JWT for the Authorization header."""
        payload: dict = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
            "timestamp": int(time.time() * 1000),
        }
        if params:
            qs = urllib.parse.urlencode(params).encode()
            payload["query_hash"] = hashlib.sha512(qs).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        return jwt.encode(payload, self.secret_key, algorithm="HS256")

    def _auth_headers(self, params: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {self._make_jwt(params)}"}

    # ---- public market data (no auth) ----------------------------------------

    def get_kline(self, category=None, symbol: str = "", interval: str = "5",
                  limit: int = 3, **_kw) -> dict:
        """Fetch OHLCV candles in Bybit list format with USDT-normalised prices."""
        market = self.to_market(symbol)
        resp = requests.get(
            f"{BITHUMB_BASE}/candles/minutes/{interval}",
            params={"market": market, "count": limit},
            timeout=10,
        )
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list):
            return {"result": {"list": []}}
        rate = self._krw_usdt_rate() if self._is_krw_market(market) else 1.0
        def _to_ms(iso: str) -> str:
            dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S")
            return str(int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000))

        rows = [
            [
                _to_ms(c["candle_date_time_utc"]),

                str(c["opening_price"] / rate),
                str(c["high_price"]    / rate),
                str(c["low_price"]     / rate),
                str(c["trade_price"]   / rate),
                str(c["candle_acc_trade_volume"]),
                str(c.get("candle_acc_trade_price", 0)),
            ]
            for c in raw
        ]
        return {"result": {"list": rows}}

    def get_tickers(self, category=None, symbol: str = "", **_kw) -> dict:
        market = self.to_market(symbol)
        resp = requests.get(
            f"{BITHUMB_BASE}/ticker",
            params={"markets": market},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list) or not data:
            return {"result": {"list": []}}
        price_raw = float(data[0]["trade_price"])
        rate = self._krw_usdt_rate() if self._is_krw_market(market) else 1.0
        price = str(price_raw / rate)
        return {"result": {"list": [{"markPrice": price}]}}

    def get_instruments_info(self, category=None, symbol: str = "", **_kw) -> dict:
        """Return instrument metadata in Bybit lotSizeFilter / priceFilter shape."""
        try:
            market = self.to_market(symbol)
            resp = requests.get(
                f"{BITHUMB_BASE}/ticker",
                params={"markets": market},
                timeout=10,
            )
            resp.raise_for_status()
            raw = resp.json()
            price_raw = float(raw[0]["trade_price"]) if isinstance(raw, list) and raw else 0.0
            rate = self._krw_usdt_rate() if self._is_krw_market(market) else 1.0
            tick = self._tick_for_usdt_price(price_raw / rate)
        except Exception:
            tick = "0.00000001"
        return {"result": {"list": [{
            "lotSizeFilter": {
                "qtyStep": "0.00000001",
                "minOrderQty": "0.00000001",
                "minNotionalValue": "1",
            },
            "priceFilter": {"tickSize": tick},
        }]}}

    # ---- private account data ------------------------------------------------

    def get_positions(self, category=None, settleCoin=None, **_kw) -> dict:
        """Return open KRW spot holdings as Bybit-style position dicts.

        avg_buy_price converted from KRW to USDT.
        Symbols returned as XRP/KRW to match symbols_bithumb.json.
        """
        resp = requests.get(
            f"{BITHUMB_BASE}/accounts",
            headers=self._auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()

        rate = self._krw_usdt_rate()
        positions = []
        skip = {"USDT", "KRW"}  # BTC/KRW is a valid trading symbol — don't skip BTC
        for acct in resp.json():
            currency = acct["currency"]
            if currency in skip:
                continue
            balance = float(acct.get("balance") or 0) + float(acct.get("locked") or 0)
            if balance <= 1e-8:
                continue
            avg_price_krw = float(acct.get("avg_buy_price") or 0)
            avg_price_usdt = avg_price_krw / rate if avg_price_krw else 0.0
            positions.append({
                "symbol": f"{currency}/KRW",
                "side": "Buy",
                "size": str(balance),
                "avgPrice": str(avg_price_usdt),
                "markPrice": str(avg_price_usdt),
                "unrealisedPnl": "0",
                "positionIM": str(balance * avg_price_usdt),
                "takeProfit": "",
                "stopLoss": "",
                "leverage": "1",
            })
        return {"result": {"list": positions}}

    def get_wallet_balance(self, accountType=None, **_kw) -> dict:
        """Return available balance as USDT equivalent (KRW converted at cached rate)."""
        resp = requests.get(
            f"{BITHUMB_BASE}/accounts",
            headers=self._auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        rate = self._krw_usdt_rate()
        total = avail = 0.0
        for acct in resp.json():
            cur  = acct["currency"]
            bal  = float(acct.get("balance") or 0)
            lock = float(acct.get("locked")  or 0)
            if cur == "USDT":
                total += bal + lock
                avail += bal
            elif cur == "KRW":
                total += (bal + lock) / rate
                avail += bal / rate
        if total == 0.0:
            return {"result": {"list": [{"totalEquity": "0", "totalAvailableBalance": "0", "totalPerpUPL": "0"}]}}
        return {"result": {"list": [{
            "totalEquity": str(total),
            "totalAvailableBalance": str(avail),
            "totalPerpUPL": "0",
        }]}}

    # ---- order management ----------------------------------------------------

    def place_order(self, category=None, symbol: str = "", side: str = "",
                    orderType: str = "Limit", price=None, qty=None,
                    timeInForce=None, reduceOnly: bool = False, **_kw) -> dict:
        market = self.to_market(symbol)
        bithumb_side = "bid" if side == "Buy" else "ask"

        # Bithumb ord_type:
        #   "limit"  = limit order (Buy or Sell)
        #   "market" = market order
        # Note: Bithumb does not support "post_only"; PostOnly is treated as limit.
        if reduceOnly:
            ord_type = "market"
        elif side == "Sell" and orderType == "Limit" and price is not None:
            ord_type = "limit"
        elif side == "Sell":
            ord_type = "market"
        else:
            ord_type = "limit"

        if self._is_krw_market(market):
            rate = self._krw_usdt_rate()
            # price arrives in USDT from runner.py -- convert to KRW and snap
            # to Bithumb's official tick unit (호가단위) for that price level.
            # Use integer KRW and guard against tier-boundary crossing after snap,
            # which can happen due to floating-point imprecision in the USDT→KRW
            # round-trip (e.g. raw_krw = 499999.9 should use tick 50, not 100).
            if price is not None:
                krw_int = int(round(float(price) * rate))  # USDT → nearest integer KRW
                tick = self._krw_tick(krw_int)
                snapped = (krw_int // tick) * tick
                # After flooring we might cross into a lower tier; re-snap if tick changed.
                tick2 = self._krw_tick(snapped)
                if tick2 != tick:
                    snapped = (snapped // tick2) * tick2
                krw_price = str(snapped)
            else:
                krw_price = None
        else:
            krw_price = str(price) if price is not None else None

        if ord_type == "market":
            params = {
                "market": market,
                "side": bithumb_side,
                "ord_type": "market",
                "volume": str(qty),
            }
        else:
            params = {
                "market": market,
                "side": bithumb_side,
                "ord_type": ord_type,
                "volume": str(qty),
                "price": krw_price,
            }

        resp = requests.post(
            f"{BITHUMB_BASE}/orders",
            json=params,
            headers=self._auth_headers(params),
            timeout=10,
        )
        if not resp.ok:
            raise requests.HTTPError(
                f"{resp.status_code} placing order on {market}: {resp.text}",
                response=resp,
            )
        return {"result": {"orderId": resp.json()["uuid"]}}

    def cancel_order(self, category=None, symbol=None, orderId=None, **_kw) -> dict:
        """Cancel a pending order.

        Raises with 110001 in message if already filled so runner.py fill-detection fires.
        """
        params = {"uuid": orderId}
        resp = requests.delete(
            f"{BITHUMB_BASE}/order",
            params=params,
            headers=self._auth_headers(params),
            timeout=10,
        )
        if not resp.ok:
            raise RuntimeError(
                f"110001 cancel failed for order {orderId}: {resp.status_code} {resp.text}"
            )
        return resp.json()

    def get_order(self, orderId: str, **_kw) -> dict:
        """Fetch a single order by UUID.

        Returned dict includes ``state``: ``'wait'`` = pending, ``'done'`` = filled,
        ``'cancel'`` = cancelled.  ``price`` is the order's limit price.
        """
        params = {"uuid": orderId}
        resp = requests.get(
            f"{BITHUMB_BASE}/order",
            params=params,
            headers=self._auth_headers(params),
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def cancel_all_orders(self, **_kw) -> dict:
        """Cancel every open (waiting) order."""
        params = {"state": "wait"}
        resp = requests.get(
            f"{BITHUMB_BASE}/orders",
            params=params,
            headers=self._auth_headers(params),
            timeout=10,
        )
        resp.raise_for_status()

        cancelled = []
        for order in resp.json():
            del_params = {"uuid": order["uuid"]}
            del_resp = requests.delete(
                f"{BITHUMB_BASE}/order",
                params=del_params,
                headers=self._auth_headers(del_params),
                timeout=10,
            )
            if del_resp.ok:
                cancelled.append({
                    "orderId": order["uuid"],
                    "symbol": order.get("market", ""),
                    "side": order.get("side", ""),
                    "qty": order.get("volume") or "",
                })
        return {"result": {"list": cancelled}}

    # ---- no-ops (spot has no leverage or native TP/SL) -----------------------

    def set_leverage(self, **_kw) -> None:
        pass

    def set_trading_stop(self, **_kw) -> None:
        pass
