import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import httpx


class BybitError(RuntimeError):
    pass


class BybitClient:
    def __init__(self, api_key: str = "", api_secret: str = "", testnet: bool = True, recv_window: int = 5000):
        self.api_key = api_key
        self.api_secret = api_secret
        self.recv_window = recv_window
        self.public_base_url = "https://api.bybit.com"
        self.private_base_url = "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None, private: bool = False) -> dict[str, Any]:
        params = params or {}
        body = body or {}
        timestamp = str(int(time.time() * 1000))
        headers = {"Content-Type": "application/json"}
        # httpx preserves mapping insertion order when encoding query params;
        # sign the same order that is sent on the wire.
        query = urlencode(params)
        if private:
            if not self.api_key or not self.api_secret:
                raise BybitError("Bybit API credentials are not configured")
            payload = query if method == "GET" else json.dumps(body, separators=(",", ":"))
            sign_target = timestamp + self.api_key + str(self.recv_window) + payload
            signature = hmac.new(self.api_secret.encode(), sign_target.encode(), hashlib.sha256).hexdigest()
            headers.update({"X-BAPI-API-KEY": self.api_key, "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": str(self.recv_window), "X-BAPI-SIGN": signature})
        async with httpx.AsyncClient(timeout=12) as client:
            base_url = self.private_base_url if private else self.public_base_url
            # Send the exact bytes used for V5 signature generation. Passing
            # ``json=body`` lets httpx serialize the object again, which can
            # produce a different payload and causes Bybit Error sign.
            request_kwargs = {"params": params if method == "GET" else None, "headers": headers}
            if method != "GET":
                request_kwargs["content"] = json.dumps(body, separators=(",", ":"))
            response = await client.request(method, base_url + path, **request_kwargs)
        response.raise_for_status()
        data = response.json()
        if data.get("retCode", 0) != 0:
            raise BybitError(data.get("retMsg", "Bybit request failed"))
        return data.get("result", {})

    async def instruments(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/v5/market/instruments-info", {"category": "option", "baseCoin": "BTC", "limit": 1000})
        return result.get("list", [])

    async def tickers(self, symbol: str | None = None) -> list[dict[str, Any]]:
        params = {"category": "option", "symbol": symbol} if symbol else {"category": "option", "baseCoin": "BTC"}
        result = await self._request("GET", "/v5/market/tickers", params)
        return result.get("list", [])

    async def underlying_ticker(self) -> dict[str, Any]:
        result = await self._request("GET", "/v5/market/tickers", {"category": "linear", "symbol": "BTCUSDT"})
        return (result.get("list") or [{}])[0]

    async def positions(self) -> list[dict[str, Any]]:
        result = await self._request("GET", "/v5/position/list", {"category": "option", "baseCoin": "BTC"}, private=True)
        return result.get("list", [])

    async def account_info(self) -> dict[str, Any]:
        return await self._request("GET", "/v5/account/info", private=True)

    async def wallet_balance(self) -> dict[str, Any]:
        result = await self._request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": "USDT,USDC"}, private=True)
        return (result.get("list") or [{}])[0]

    async def executions(self, order_link_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"category": "option", "baseCoin": "BTC", "limit": 50}
        if order_link_id:
            params["orderLinkId"] = order_link_id
        result = await self._request("GET", "/v5/execution/list", params, private=True)
        return result.get("list", [])

    async def place_limit_order(self, symbol: str, side: str, qty: float, price: float, order_link_id: str, reduce_only: bool = False) -> dict[str, Any]:
        return await self._request("POST", "/v5/order/create", body={"category": "option", "symbol": symbol, "side": side, "orderType": "Limit", "qty": str(qty), "price": str(price), "timeInForce": "GTC", "orderLinkId": order_link_id, "reduceOnly": reduce_only}, private=True)

    async def amend_order(self, symbol: str, order_link_id: str, price: float) -> dict[str, Any]:
        return await self._request("POST", "/v5/order/amend", body={"category": "option", "symbol": symbol, "orderLinkId": order_link_id, "price": str(price)}, private=True)

    async def cancel_order(self, symbol: str, order_link_id: str) -> dict[str, Any]:
        return await self._request("POST", "/v5/order/cancel", body={"category": "option", "symbol": symbol, "orderLinkId": order_link_id}, private=True)

    async def place_market_order(self, symbol: str, side: str, qty: float, order_link_id: str, reduce_only: bool = False) -> dict[str, Any]:
        return await self._request("POST", "/v5/order/create", body={"category": "option", "symbol": symbol, "side": side, "orderType": "Market", "qty": str(qty), "timeInForce": "IOC", "orderLinkId": order_link_id, "reduceOnly": reduce_only}, private=True)

    async def rfq_config(self) -> dict[str, Any]:
        return await self._request("GET", "/v5/rfq/config", private=True)

    async def create_rfq(self, counterparties: list[str], legs: list[dict[str, Any]], rfq_link_id: str, strategy_type: str = "custom") -> dict[str, Any]:
        return await self._request("POST", "/v5/rfq/create-rfq", body={"counterparties": counterparties, "rfqLinkId": rfq_link_id, "strategyType": strategy_type, "list": legs}, private=True)

    async def rfq_realtime(self, rfq_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"traderType": "request"}
        if rfq_id:
            params["rfqId"] = rfq_id
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
