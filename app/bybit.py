import hashlib
import hmac
import json
import time
from typing import Any

import httpx


class BybitError(RuntimeError):
    pass


class BybitClient:
    def __init__(self, api_key: str = "", api_secret: str = "", testnet: bool = True, recv_window: int = 5000, market_testnet: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.recv_window = recv_window
        self.public_base_url = "https://api-testnet.bybit.com" if market_testnet else "https://api.bybit.com"
        self.private_base_url = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"
        self._client: httpx.AsyncClient | None = None

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None or getattr(self._client, "is_closed", False):
            limits = httpx.Limits(max_connections=30, max_keepalive_connections=15, keepalive_expiry=30)
            self._client = httpx.AsyncClient(timeout=12, limits=limits)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not getattr(self._client, "is_closed", False):
            await self._client.aclose()

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None, private: bool = False) -> dict[str, Any]:
        params = params or {}
        body = body or {}
        timestamp = str(int(time.time() * 1000))
        headers = {"Content-Type": "application/json"}
        # httpx preserves mapping insertion order when encoding query params;
        # sign the same order that is sent on the wire.
        query = str(httpx.QueryParams(params))
        encoded_body = json.dumps(body, separators=(",", ":"), allow_nan=False) if method != "GET" else None
        if private:
            if not self.api_key or not self.api_secret:
                raise BybitError("Bybit API credentials are not configured")
            payload = query if method == "GET" else encoded_body
            sign_target = timestamp + self.api_key + str(self.recv_window) + payload
            signature = hmac.new(self.api_secret.encode(), sign_target.encode(), hashlib.sha256).hexdigest()
            headers.update({"X-BAPI-API-KEY": self.api_key, "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": str(self.recv_window), "X-BAPI-SIGN": signature})
        client = self._http_client()
        base_url = self.private_base_url if private else self.public_base_url
        # Send the exact bytes used for V5 signature generation. Passing
        # ``json=body`` lets httpx serialize the object again, which can
        # produce a different payload and causes Bybit Error sign.
        request_kwargs = {"params": params if method == "GET" else None, "headers": headers}
        if method != "GET":
            request_kwargs["content"] = encoded_body
        response = await client.request(method, base_url + path, **request_kwargs)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise BybitError("Invalid Bybit JSON response") from exc
        if not isinstance(data, dict) or type(data.get("retCode")) is not int:
            raise BybitError("Invalid Bybit response envelope")
        if data["retCode"] != 0:
            raise BybitError(data.get("retMsg", "Bybit request failed"))
        if not isinstance(data.get("result"), dict):
            raise BybitError("Invalid Bybit response result")
        return data["result"]

    async def _pages(self, path: str, params: dict[str, Any], *, private: bool = False, max_items: int | None = None, identity: str | tuple[str, ...] = "symbol") -> list[dict[str, Any]]:
        params = dict(params)
        items = []
        seen_items = set()
        seen_cursors = set()
        # A broken/repeating cursor must fail rather than return partial data.
        for _ in range(100):
            result = await self._request("GET", path, params, private=private)
            rows = result.get("list")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise BybitError("Invalid Bybit paginated response")
            for row in rows:
                fields = (identity,) if isinstance(identity, str) else identity
                values = tuple(row.get(field) for field in fields)
                if not all(isinstance(value, str) for value in values) or not values[0]:
                    raise BybitError("Missing identity in Bybit paginated response")
                key = values
                if key not in seen_items:
                    seen_items.add(key)
                    items.append(row)
            if max_items is not None and len(items) >= max_items:
                return items[:max_items]
            cursor = result.get("nextPageCursor")
            if cursor in (None, ""):
                return items
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise BybitError("Invalid or repeated Bybit pagination cursor")
            seen_cursors.add(cursor)
            params["cursor"] = cursor
        raise BybitError("Bybit pagination exceeded the page limit")

    async def instruments(self) -> list[dict[str, Any]]:
        return await self._pages("/v5/market/instruments-info", {"category": "option", "baseCoin": "BTC", "limit": 1000})

    async def tickers(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"category": "option", "symbol": symbol} if symbol else {"category": "option", "baseCoin": "BTC"}
        result = await self._request("GET", "/v5/market/tickers", params)
        return result.get("list", [])

    async def underlying_ticker(self) -> dict[str, Any]:
        result = await self._request("GET", "/v5/market/tickers", {"category": "linear", "symbol": "BTCUSDT"})
        return (result.get("list") or [{}])[0]

    async def positions(self) -> list[dict[str, Any]]:
        return await self._pages("/v5/position/list", {"category": "option", "baseCoin": "BTC", "limit": 200}, private=True, identity=("symbol", "side"))

    async def account_info(self) -> dict[str, Any]:
        return await self._request("GET", "/v5/account/info", private=True)

    async def closed_option_positions(self, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        # The endpoint permits at most seven days per query. Preserve integer
        # timestamps while adapting identities for the shared pagination checks.
        rows = []
        while start_ms <= end_ms:
            stop = min(end_ms, start_ms + 7 * 86400000 - 1)
            cursor = None
            seen = set()
            for _ in range(100):
                params = {"category": "option", "symbol": symbol, "startTime": start_ms, "endTime": stop, "limit": 100}
                if cursor:
                    params["cursor"] = cursor
                result = await self._request("GET", "/v5/position/get-closed-positions", params, private=True)
                page = result.get("list")
                if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
                    raise BybitError("Invalid closed option positions response")
                rows.extend(page)
                cursor = result.get("nextPageCursor")
                if cursor in (None, ""):
                    break
                if not isinstance(cursor, str) or cursor in seen:
                    raise BybitError("Invalid closed option positions cursor")
                seen.add(cursor)
            else:
                raise BybitError("Closed option positions exceeded page limit")
            start_ms = stop + 1
        return rows

    async def rfq_history(self, rfq_id: str | None, rfq_link_id: str | None = None) -> list[dict[str, Any]]:
        params = {"traderType": "request", **({"rfqId": rfq_id} if rfq_id else {"rfqLinkId": rfq_link_id})}
        result = await self._request("GET", "/v5/rfq/rfq-list", params, private=True)
        rows = result.get("list")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise BybitError("Invalid RFQ history response")
        return rows

    async def wallet_balance(self) -> dict[str, Any]:
        result = await self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT,USDC"}, private=True)
        return (result.get("list") or [{}])[0]

    async def portfolio_margin(self, base_coin: str = "BTC") -> dict[str, Any]:
        return await self._request("GET", "/v5/asset/portfolio-margin", {"baseCoin": base_coin}, private=True)

    async def executions(self, order_link_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": "option", "baseCoin": "BTC", "limit": 50}
        if order_link_id:
            params["orderLinkId"] = order_link_id
        return await self._pages("/v5/execution/list", params, private=True, identity="execId", max_items=None if order_link_id else 100)

    async def order(self, symbol: str, order_link_id: str) -> dict[str, Any] | None:
        params = {"category": "option", "symbol": symbol, "orderLinkId": order_link_id}
        # Closed realtime records can disappear after an exchange restart.
        # Absence from both endpoints is still unknown, never a cancellation.
        for path in ("/v5/order/realtime", "/v5/order/history"):
            result = await self._request("GET", path, params, private=True)
            match = next((item for item in result.get("list", []) if item.get("orderLinkId") == order_link_id and item.get("symbol") == symbol), None)
            if match is not None:
                return match
        return None

    async def place_limit_order(self, symbol: str, side: str, qty: float, price: float, order_link_id: str, reduce_only: bool = False) -> dict[str, Any]:
        return await self._request("POST", "/v5/order/create", body={"category": "option", "symbol": symbol, "side": side, "orderType": "Limit", "qty": str(qty), "price": str(price), "timeInForce": "GTC", "orderLinkId": order_link_id, "reduceOnly": reduce_only}, private=True)

    async def amend_order(self, symbol: str, order_link_id: str, price: float) -> dict[str, Any]:
        return await self._request("POST", "/v5/order/amend", body={"category": "option", "symbol": symbol, "orderLinkId": order_link_id, "price": str(price)}, private=True)

    async def cancel_order(self, symbol: str, order_link_id: str) -> dict[str, Any]:
        return await self._request("POST", "/v5/order/cancel", body={"category": "option", "symbol": symbol, "orderLinkId": order_link_id}, private=True)

    async def place_ioc_order(self, symbol: str, side: str, qty: float, price: float, order_link_id: str, reduce_only: bool = False) -> dict[str, Any]:
        return await self._request("POST", "/v5/order/create", body={"category": "option", "symbol": symbol, "side": side, "orderType": "Limit", "price": str(price), "qty": str(qty), "timeInForce": "IOC", "orderLinkId": order_link_id, "reduceOnly": reduce_only}, private=True)

    async def rfq_config(self) -> dict[str, Any]:
        return await self._request("GET", "/v5/rfq/config", private=True)

    async def create_rfq(self, counterparties: list[str], legs: list[dict[str, Any]], rfq_link_id: str, strategy_type: str = "custom") -> dict[str, Any]:
        return await self._request("POST", "/v5/rfq/create-rfq", body={"counterparties": counterparties, "rfqLinkId": rfq_link_id, "strategyType": strategy_type, "list": legs}, private=True)

    async def rfq_realtime(self, rfq_id: str | None = None, rfq_link_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"traderType": "request"}
        if rfq_id:
            params["rfqId"] = rfq_id
        elif rfq_link_id:
            params["rfqLinkId"] = rfq_link_id
        result = await self._request("GET", "/v5/rfq/rfq-realtime", params, private=True)
        return result.get("list", [])

    async def quote_realtime(self, rfq_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"traderType": "request"}
        if rfq_id:
            params["rfqId"] = rfq_id
        result = await self._request("GET", "/v5/rfq/quote-realtime", params, private=True)
        return result.get("list", [])

    async def execute_quote(self, rfq_id: str, quote_id: str, quote_side: str) -> dict[str, Any]:
        return await self._request("POST", "/v5/rfq/execute-quote", body={"rfqId": rfq_id, "quoteId": quote_id, "quoteSide": quote_side}, private=True)

    async def cancel_rfq(self, rfq_id: str) -> dict[str, Any]:
        return await self._request("POST", "/v5/rfq/cancel-rfq", body={"rfqId": rfq_id}, private=True)
