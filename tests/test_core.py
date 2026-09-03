import asyncio
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx

from app.bybit import BybitClient
from app.config import Settings
from app.main import app
from app.strategy import build_iron_condor, demo_chain


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeAsyncClient:
    last_request = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def request(self, method, url, **kwargs):
        FakeAsyncClient.last_request = (method, url, kwargs)
        return FakeResponse({"retCode": 0, "result": {"ok": True}})


class CoreTests(unittest.TestCase):
    def test_strategy_builds_four_legs(self):
        now = datetime.now(timezone.utc)
        preview = build_iron_condor(demo_chain(now), now, target_dte=2, qty=0.01)
        self.assertEqual(len(preview.legs), 4)
        self.assertEqual({leg.side for leg in preview.legs}, {"Buy", "Sell"})

    def test_strategy_max_loss_uses_each_wing_width(self):
        now = datetime.now(timezone.utc)
        preview = build_iron_condor(demo_chain(now), now, target_dte=2, qty=0.01)
        short_call = next(leg for leg in preview.legs if leg.option_type == "Call" and leg.side == "Sell")
        long_call = next(leg for leg in preview.legs if leg.option_type == "Call" and leg.side == "Buy")
        short_put = next(leg for leg in preview.legs if leg.option_type == "Put" and leg.side == "Sell")
        long_put = next(leg for leg in preview.legs if leg.option_type == "Put" and leg.side == "Buy")
        width = max(long_call.strike - short_call.strike, short_put.strike - long_put.strike)
        expected = round(max(0.0, width * 0.01 - preview.net_credit_usd), 2)
        self.assertEqual(preview.max_loss_usd, expected)

    def test_market_fallback_is_disabled_by_default(self):
        settings = Settings(_env_file=None)
        self.assertFalse(settings.allow_market_fallback)

    def test_get_signature_matches_wire_query_order(self):
        client = BybitClient("key", "secret", testnet=False, recv_window=5000)
        with patch("app.bybit.httpx.AsyncClient", FakeAsyncClient), patch("app.bybit.time.time", return_value=1700000000):
            asyncio.run(client._request("GET", "/private", {"category": "option", "baseCoin": "BTC"}, private=True))
        _, _, kwargs = FakeAsyncClient.last_request
        query = "category=option&baseCoin=BTC"
        expected = hmac.new(b"secret", ("1700000000000key5000" + query).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(kwargs["params"], {"category": "option", "baseCoin": "BTC"})
        self.assertEqual(kwargs["headers"]["X-BAPI-SIGN"], expected)

    def test_post_signature_uses_exact_compact_body(self):
        client = BybitClient("key", "secret", testnet=False, recv_window=5000)
        body = {"category": "option", "symbol": "BTC-TEST", "qty": "1"}
        compact = json.dumps(body, separators=(",", ":"))
        with patch("app.bybit.httpx.AsyncClient", FakeAsyncClient), patch("app.bybit.time.time", return_value=1700000000):
            asyncio.run(client._request("POST", "/private", body=body, private=True))
        _, _, kwargs = FakeAsyncClient.last_request
        expected = hmac.new(b"secret", ("1700000000000key5000" + compact).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(kwargs["content"], compact)
        self.assertEqual(kwargs["headers"]["X-BAPI-SIGN"], expected)

    def test_health_endpoint_is_local_system_smoke_test(self):
        async def call():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get("/api/health")

        response = asyncio.run(call())
        self.assertEqual(response.status_code, 200)
        self.assertIn("live_enabled", response.json())


if __name__ == "__main__":
    unittest.main()
