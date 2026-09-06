import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.engine import TradingEngine
from app.main import app
from app.models import OpenRequest, RfqCreateRequest
from app.strategy import SundayExpiryUnavailable, demo_chain
from pydantic import SecretStr


class MarketAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, 23, 45, tzinfo=timezone.utc)
        for module in ("app.main", "app.engine"):
            clock = patch(f"{module}.datetime", wraps=datetime)
            clock.start().now.return_value = self.now
            self.addCleanup(clock.stop)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.engine = TradingEngine(Settings(_env_file=None, state_file=f"{directory.name}/state.json"))
        self.sunday_chain = demo_chain(self.now)
        self.engine.chain = [item.model_copy(update={"expiry": item.expiry + timedelta(days=1)}) for item in self.sunday_chain]
        self.engine.chain_source = "bybit"
        self.engine.chain_updated_at = self.now
        self.engine.btc_price = 100000
        self.engine.refresh_chain = AsyncMock()

    async def request(self, path="/api/dashboard/market?quantity=0.01"):
        with patch("app.main.engine", self.engine), patch("app.main.settings", self.engine.settings):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                return await client.get(path)

    async def test_missing_sunday_is_normal_wait_with_market_and_config(self):
        self.engine.chain.extend(item.model_copy(update={"expiry": item.expiry - timedelta(days=7)}) for item in self.sunday_chain)
        response = await self.request()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "waiting_for_listing")
        self.assertIsNone(data["preview"])
        self.assertEqual(data["chain"]["source"], "bybit")
        self.assertEqual(data["chain"]["btc_price"], 100000)
        self.assertEqual(data["chain"]["items"], [])
        self.assertIn("live_enabled", data["config"])
        # The trading preview API still refuses an unavailable strategy.
        self.assertEqual((await self.request("/api/strategy/preview?quantity=0.01")).status_code, 422)

    async def test_new_sunday_contracts_restore_ready_preview(self):
        self.assertEqual((await self.request()).json()["status"], "waiting_for_listing")
        self.engine.chain = self.sunday_chain
        data = (await self.request()).json()
        self.assertEqual(data["status"], "ready")
        self.assertEqual(len(data["preview"]["legs"]), 4)

    async def test_two_calendar_days_ahead_quotes_are_read_only_and_sunday_recovers(self):
        expiry = (self.now + timedelta(days=2)).replace(hour=8, minute=0)
        observation = [item.model_copy(update={"expiry": expiry, "symbol": f"observe-{i}"}) for i, item in enumerate(self.sunday_chain)]
        self.engine.chain.extend(observation)
        data = (await self.request()).json()
        self.assertTrue(data["read_only"])
        self.assertIsNone(data["preview"])
        self.assertIsNone(self.engine.preview)
        self.assertEqual(data["chain"]["expiry"], expiry.isoformat())
        self.assertEqual(len(data["chain"]["items"]), len(observation))
        self.assertTrue(all(item["symbol"].startswith("observe-") for item in data["chain"]["items"]))
        self.assertIn("仅供查看", data["message"])
        self.assertEqual((await self.request("/api/strategy/preview?quantity=0.01")).status_code, 422)
        with self.assertRaises(SundayExpiryUnavailable):
            await self.engine.open_position(OpenRequest(quantity=.01))
        self.assertEqual(self.engine.positions, [])
        self.engine.settings.bybit_api_key = SecretStr("test-key")
        self.engine.settings.bybit_api_secret = SecretStr("test-secret")
        self.engine.client.rfq_config = AsyncMock(return_value={"counterparties": ["TEST"]})
        self.engine.client.create_rfq = AsyncMock()
        with self.assertRaises(SundayExpiryUnavailable):
            await self.engine.create_rfq(RfqCreateRequest(quantity=.01))
        self.engine.client.create_rfq.assert_not_awaited()
        self.engine.chain.extend(self.sunday_chain)
        ready = (await self.request()).json()
        self.assertEqual(ready["status"], "ready")
        self.assertFalse(ready.get("read_only", False))
        self.assertTrue(all(not item["symbol"].startswith("observe-") for item in ready["chain"]["items"]))

    async def test_stale_missing_or_unavailable_market_is_not_listing_wait(self):
        for source, updated in (("bybit", self.now - timedelta(minutes=5)), ("bybit", None), ("unavailable", self.now)):
            self.engine.chain_source = source
            self.engine.chain_updated_at = updated
            self.assertEqual((await self.request()).status_code, 503)
        self.engine.chain = []
        self.assertEqual((await self.request()).status_code, 422)

    async def test_existing_sunday_with_bad_quotes_is_not_listing_wait(self):
        self.engine.chain.extend(item.model_copy(update={"delta": 0, "mark_price": 0}) for item in self.sunday_chain)
        response = await self.request()
        self.assertEqual(response.status_code, 422)
        self.assertIn("报价", response.json()["detail"])

    async def test_manual_refresh_reloads_instrument_catalog(self):
        del self.engine.refresh_chain
        self.engine.raw_instruments = [{"symbol": "previous"}]
        self.engine.instruments_updated_at = self.now
        self.engine.client.instruments = AsyncMock(return_value=[])
        self.engine.client.tickers = AsyncMock(return_value=[])
        self.engine.client.underlying_ticker = AsyncMock(return_value={})
        await self.engine.refresh_chain(force=True)
        self.engine.client.instruments.assert_not_awaited()
        await self.engine.refresh_chain(force=True, refresh_instruments=True)
        self.engine.client.instruments.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
