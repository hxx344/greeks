import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.engine import TradingEngine
from app.main import app
from app.strategy import demo_chain


class MarketAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.engine = TradingEngine(Settings(_env_file=None, state_file=f"{directory.name}/state.json"))
        self.now = datetime.now(timezone.utc)
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
