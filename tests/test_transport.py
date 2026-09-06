import hashlib
import hmac
import tempfile
import unittest
from unittest.mock import AsyncMock

import httpx

from app.bybit import BybitClient, BybitError
from app.config import Settings
from app.engine import TradingEngine
from app.models import OrderResult


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_signatures_match_actual_query_and_body_bytes(self):
        requests = []
        def respond(request):
            requests.append(request)
            payload = request.url.query if request.method == "GET" else request.content
            prefix = request.headers["x-bapi-timestamp"] + "test-key5000"
            signature = hmac.new(b"test-secret", prefix.encode() + payload, hashlib.sha256).hexdigest()
            self.assertEqual(request.headers["x-bapi-sign"], signature)
            return httpx.Response(200, json={"retCode": 0, "result": {}})
        client = BybitClient("test-key", "test-secret")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            client._client = transport
            await client._request("GET", "/test", params={"cursor": "a +/中文=", "flag": True, "ids": ["a", "b"]}, private=True)
            await client._request("POST", "/test", body={"text": "中文", "reduceOnly": True}, private=True)
            with self.assertRaises(ValueError):
                await client._request("POST", "/test", body={"qty": float("nan")}, private=True)
        self.assertEqual(len(requests), 2)

    async def test_malformed_exchange_envelopes_cannot_look_successful(self):
        payloads = [b"not json", b"[]", b"{}", b'{"retCode":false,"result":{}}',
                    b'{"retCode":"0","result":{}}', b'{"retCode":0}',
                    b'{"retCode":0,"result":[]}', b'{"retCode":10001,"retMsg":"bad request"}']
        client = BybitClient()
        for payload in payloads:
            with self.subTest(payload=payload):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))) as transport:
                    client._client = transport
                    with self.assertRaises(BybitError):
                        await client._request("GET", "/test")


class PaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_instrument_pages_follow_cursor_and_deduplicate(self):
        cursors = []
        async def page(method, path, params, **kwargs):
            cursors.append(params.get("cursor"))
            if not params.get("cursor"):
                return {"list": [{"symbol": "A"}], "nextPageCursor": "cursor +/="}
            return {"list": [{"symbol": "A"}, {"symbol": "B"}]}
        client = BybitClient()
        client._request = AsyncMock(side_effect=page)
        self.assertEqual(await client.instruments(), [{"symbol": "A"}, {"symbol": "B"}])
        self.assertEqual(cursors, [None, "cursor +/="])

    async def test_position_pages_preserve_opposite_sides(self):
        client = BybitClient()
        client._request = AsyncMock(side_effect=[
            {"list": [{"symbol": "A", "side": "Buy"}], "nextPageCursor": "next"},
            {"list": [{"symbol": "A", "side": "Sell"}]}])
        self.assertEqual(len(await client.positions()), 2)

    async def test_order_executions_follow_all_pages_but_dashboard_is_bounded(self):
        client = BybitClient()
        pages = [{"list": [{"execId": str(i)} for i in range(start, start + 50)], "nextPageCursor": str(start + 50)} for start in (0, 50, 100)]
        pages[-1]["nextPageCursor"] = ""
        client._request = AsyncMock(side_effect=pages)
        self.assertEqual(len(await client.executions("order-link")), 150)
        self.assertEqual(client._request.await_count, 3)
        client._request = AsyncMock(side_effect=pages)
        self.assertEqual(len(await client.executions()), 100)
        self.assertEqual(client._request.await_count, 2)

    async def test_bad_pages_fail_instead_of_returning_partial_holdings(self):
        for invalid in ({}, {"list": None}, {"list": [None]}, {"list": [{}]},
                        {"list": [], "nextPageCursor": 123}, {"list": [], "nextPageCursor": "next"}):
            with self.subTest(invalid=invalid):
                client = BybitClient()
                client._request = AsyncMock(side_effect=[{"list": [{"symbol": "A"}], "nextPageCursor": "next"}, invalid])
                with self.assertRaises(BybitError):
                    await client.instruments()

    async def test_page_limit_bounds_unending_distinct_cursors(self):
        client = BybitClient()
        client._request = AsyncMock(side_effect=[{"list": [], "nextPageCursor": str(i)} for i in range(100)])
        params = {"category": "option"}
        with self.assertRaisesRegex(BybitError, "page limit"):
            await client._pages("/test", params)
        self.assertEqual(params, {"category": "option"})
        self.assertEqual(client._request.await_count, 100)


class CompleteExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_order_fee_totals_survive_dashboard_truncation_and_refresh_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = TradingEngine(Settings(_env_file=None, state_file=f"{directory}/state.json", bybit_api_key="test", bybit_api_secret="test"))
            rows = [{"symbol": "BTC-OPTION", "side": "Buy", "orderId": "order", "orderLinkId": "link",
                     "execId": str(i), "execQty": "0.001", "execPrice": str(100 + i), "execFee": "0.01",
                     "feeCurrency": "USDT", "execTime": str(i * 1000)} for i in range(150)]
            engine.client.executions = AsyncMock(side_effect=[rows, BybitError("offline")])
            await engine.load_recent_executions(["link", "link"])
            engine.client.executions.assert_awaited_once_with("link")
            await engine.load_recent_executions(["link"])
            result = OrderResult(symbol="BTC-OPTION", side="Buy", qty=0.15, status="filled", order_link_id="link")
            engine._attach_execution_details([result])
            self.assertEqual(len(engine.last_executions), 100)
            self.assertEqual(result.exec_qty, 0.15)
            self.assertEqual(result.exec_fee, 1.5)
            self.assertEqual(result.exec_price, 174.5)
            self.assertEqual(result.execution_id, "149")


if __name__ == "__main__":
    unittest.main()
