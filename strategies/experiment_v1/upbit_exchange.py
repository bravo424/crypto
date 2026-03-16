"""Upbit REST API adapter for experiment_v1.

Mimics the pybit HTTP interface (get_kline, get_tickers, get_instruments_info,
get_positions, get_wallet_balance, place_order, cancel_order,
cancel_all_orders, set_leverage, set_trading_stop) so runner.py works
unchanged for both exchanges.

Upbit spot notes:
  - Long-only (Buy orders only; Sell = close).
  - No leverage.
  - No native TP/SL — runner.py implements manual price-level checks.
  - Symbol mapping: BTCUSDT  →  USDT-BTC  (Upbit USDT market).
"""
from __future__ import annotations

import hashlib
import uuid
import urllib.parse

import jwt        # PyJWT >= 2.x  (added to requirements.txt)
import requests

UPBIT_BASE = "https://api.upbit.com/v1"


class UpbitSession:
    def __init__(self, access_key: str, secret_key: str) -> None:
        self.access_key = access_key
        self.secret_key = secret_key

    # ── symbol helpers ────────────────────────────────────────────────────────

    @staticmethod
    def to_market(symbol: str) -> str:
        """BTCUSDT  →  USDT-BTC"""
        if symbol.endswith("USDT"):
            return f"USDT-{symbol[:-4]}"
        return symbol

    @staticmethod
    def _tick_for_price(price: float) -> str:
        """Return a sensible tick-size string given the current price."""
        if price >= 100_000:  return "1"
        if price >= 10_000:   return "0.1"
        if price >= 1_000:    return "0.01"
        if price >= 100:      return "0.001"
        if price >= 10:       return "0.0001"
        if price >= 1:        return "0.00001"
        if price >= 0.1:      return "0.000001"
        return "0.00000001"

    # ── JWT auth ──────────────────────────────────────────────────────────────

    def _make_jwt(self, params: dict | None = None) -> str:
        """Build a signed JWT for the Authorization header.

        Upbit requires SHA-512 of the URL-encoded parameter string when the
        request carries body or query parameters.
        """
        payload: dict = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
        }
        if params:
            qs = urllib.parse.urlencode(params).encode()
            payload["query_hash"] = hashlib.sha512(qs).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        return jwt.encode(payload, self.secret_key, algorithm="HS256")

    def _auth_headers(self, params: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {self._make_jwt(params)}"}

    # ── public market data (no auth) ─────────────────────────────────────────

    def get_kline(self, category=None, symbol: str = "", interval: str = "5",
                  limit: int = 3, **_kw) -> dict:
        """Fetch OHLCV candles and return them in Bybit list format.

        Bybit candle tuple: [startTime, open, high, low, close, volume, turnover]
        Upbit returns newest candle first, which matches Bybit's ordering.
        """
        market = self.to_market(symbol)
        resp = requests.get(
            f"{UPBIT_BASE}/candles/minutes/{interval}",
            params={"market": market, "count": limit},
            timeout=10,
        )
        resp.raise_for_status()
        rows = [
            [
                c["candle_date_time_utc"],          # start time (unique per candle)
                str(c["opening_price"]),
                str(c["high_price"]),
                str(c["low_price"]),
                str(c["trade_price"]),              # close
                str(c["candle_acc_trade_volume"]),  # volume
                str(c.get("candle_acc_trade_price", 0)),  # turnover
            ]
            for c in resp.json()
        ]
        return {"result": {"list": rows}}

    def get_tickers(self, category=None, symbol: str = "", **_kw) -> dict:
        market = self.to_market(symbol)
        resp = requests.get(
            f"{UPBIT_BASE}/ticker",
            params={"markets": market},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        price = str(data[0]["trade_price"]) if data else "0"
        return {"result": {"list": [{"markPrice": price}]}}

    def get_instruments_info(self, category=None, symbol: str = "", **_kw) -> dict:
        """Return instrument metadata in Bybit lotSizeFilter / priceFilter shape."""
        try:
            market = self.to_market(symbol)
            resp = requests.get(
                f"{UPBIT_BASE}/ticker",
                params={"markets": market},
                timeout=10,
            )
            resp.raise_for_status()
            price = float(resp.json()[0]["trade_price"])
            tick = self._tick_for_price(price)
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

    # ── private account data ──────────────────────────────────────────────────

    def get_positions(self, category=None, settleCoin=None, **_kw) -> dict:
        """Return open spot holdings as Bybit-style position dicts.

        Only coins tradeable in the USDT market (non-KRW/BTC/USDT) are
        included.  Balance and locked qty are summed for the size field.
        TP/SL are empty strings — the runner manages them in state.
        """
        resp = requests.get(
            f"{UPBIT_BASE}/accounts",
            headers=self._auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()

        positions = []
        skip = {"USDT", "KRW", "BTC"}
        for acct in resp.json():
            currency = acct["currency"]
            if currency in skip:
                continue
            balance = float(acct.get("balance") or 0) + float(acct.get("locked") or 0)
            if balance <= 1e-8:
                continue
            avg_price = float(acct.get("avg_buy_price") or 0)
            positions.append({
                "symbol": f"{currency}USDT",
                "side": "Buy",
                "size": str(balance),
                "avgPrice": str(avg_price),
                "markPrice": str(avg_price),
                "unrealisedPnl": "0",
                "positionIM": str(balance * avg_price),
                "takeProfit": "",
                "stopLoss": "",
                "leverage": "1",
            })
        return {"result": {"list": positions}}

    def get_wallet_balance(self, accountType=None, **_kw) -> dict:
        """Return USDT balance in Bybit unified account shape."""
        resp = requests.get(
            f"{UPBIT_BASE}/accounts",
            headers=self._auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        for acct in resp.json():
            if acct["currency"] == "USDT":
                bal = float(acct.get("balance") or 0) + float(acct.get("locked") or 0)
                avail = float(acct.get("balance") or 0)
                return {"result": {"list": [{
                    "totalEquity": str(bal),
                    "totalAvailableBalance": str(avail),
                    "totalPerpUPL": "0",
                }]}}
        return {"result": {"list": [{"totalEquity": "0", "totalAvailableBalance": "0", "totalPerpUPL": "0"}]}}

    # ── order management ──────────────────────────────────────────────────────

    def place_order(self, category=None, symbol: str = "", side: str = "",
                    orderType: str = "Limit", price=None, qty=None,
                    timeInForce=None, reduceOnly: bool = False, **_kw) -> dict:
        market = self.to_market(symbol)
        upbit_side = "bid" if side == "Buy" else "ask"

        if reduceOnly or side == "Sell":
            # Sell / close → market order
            params = {
                "market": market,
                "side": upbit_side,
                "ord_type": "market",
                "volume": str(qty),
            }
        else:
            # Buy → limit order
            params = {
                "market": market,
                "side": upbit_side,
                "ord_type": "limit",
                "volume": str(qty),
                "price": str(price),
            }

        resp = requests.post(
            f"{UPBIT_BASE}/orders",
            json=params,
            headers=self._auth_headers(params),
            timeout=10,
        )
        resp.raise_for_status()
        return {"result": {"orderId": resp.json()["uuid"]}}

    def cancel_order(self, category=None, symbol=None, orderId=None, **_kw) -> dict:
        """Cancel a pending order.

        If the order is already filled Upbit returns a non-2xx status.  We
        raise with "110001" in the message so runner.py's fill-detection path
        fires and keeps the position record.
        """
        params = {"uuid": orderId}
        resp = requests.delete(
            f"{UPBIT_BASE}/order",
            params=params,
            headers=self._auth_headers(params),
            timeout=10,
        )
        if not resp.ok:
            raise RuntimeError(
                f"110001 cancel failed for order {orderId}: {resp.status_code} {resp.text}"
            )
        return resp.json()

    def cancel_all_orders(self, **_kw) -> dict:
        """Cancel every open order and return a Bybit-compatible result dict."""
        resp = requests.get(
            f"{UPBIT_BASE}/orders/open",
            headers=self._auth_headers(),
            timeout=10,
        )
        resp.raise_for_status()

        cancelled = []
        for order in resp.json():
            params = {"uuid": order["uuid"]}
            del_resp = requests.delete(
                f"{UPBIT_BASE}/order",
                params=params,
                headers=self._auth_headers(params),
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

    # ── no-ops (Upbit spot has no leverage or native TP/SL) ──────────────────

    def set_leverage(self, **_kw) -> None:          # pylint: disable=no-self-use
        pass

    def set_trading_stop(self, **_kw) -> None:      # pylint: disable=no-self-use
        pass
