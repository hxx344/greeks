import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.engine import TradingEngine
from app.main import app
from app.models import OpenRequest, Position, RfqCreateRequest, TradePlanRequest, TradeTaskRequest
from app.strategy import StrategyUnavailable, SundayExpiryUnavailable, demo_chain
from app.trade_tasks import TradeConflict
from pydantic import SecretStr


class MarketAvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 9, 7, 23, 45, tzinfo=timezone.utc)
        for module in ("app.main", "app.engine", "app.trade_tasks"):
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
        self.addAsyncCleanup(self.engine.client.close)

    def unavailable_chain(self, *, crossed=False):
        strikes = sorted({item.strike for item in self.sunday_chain})
        targets = {"Call": strikes[3], "Put": strikes[4] if crossed else strikes[3]}
        return [item.model_copy(update={"delta": .45 if item.strike == targets[item.option_type] else .10})
                for item in self.sunday_chain]

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

    async def test_same_or_crossed_short_strikes_keep_real_market_visible_in_both_modes(self):
        for crossed in (False, True):
            self.engine.chain = self.unavailable_chain(crossed=crossed)
            for mode in ("iron_condor", "short_strangle"):
                with self.subTest(crossed=crossed, mode=mode):
                    response = await self.request(f"/api/dashboard/market?quantity=0.01&strategy_mode={mode}")
                    self.assertEqual(response.status_code, 200)
                    data = response.json()
                    self.assertEqual(data["status"], "strategy_unavailable")
                    self.assertEqual(data["reason_code"], "short_strike_order")
                    self.assertTrue(data["read_only"])
                    self.assertIsNone(data["preview"])
                    self.assertIsNone(self.engine.preview)
                    self.assertEqual(data["chain"]["btc_price"], 100000)
                    self.assertEqual(data["chain"]["updated_at"], self.now.isoformat())
                    self.assertEqual(data["chain"]["items"], [item.model_dump(mode="json") for item in self.engine.chain])
                    self.assertIn("live_enabled", data["config"])
                    self.assertIn("行权价", data["message"])
                    rejected = await self.request(f"/api/strategy/preview?quantity=0.01&strategy_mode={mode}")
                    self.assertEqual(rejected.status_code, 422)
                    self.assertIn("short put strike", rejected.json()["detail"])

    async def test_unavailable_structure_clears_previous_preview_and_recovers_with_new_quotes(self):
        self.engine.chain = self.sunday_chain
        self.assertEqual((await self.request()).json()["status"], "ready")
        self.assertIsNotNone(self.engine.preview)
        self.engine.chain = self.unavailable_chain()
        self.assertEqual((await self.request()).json()["status"], "strategy_unavailable")
        self.assertIsNone(self.engine.preview)
        self.engine.chain = self.sunday_chain
        ready = (await self.request()).json()
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(len(ready["preview"]["legs"]), 4)
        self.assertFalse(ready.get("read_only", False))

    async def test_missing_wings_only_blocks_condor_and_missing_side_is_local_strategy_state(self):
        self.engine.chain = self.sunday_chain
        preview = await self.engine.make_preview(.01)
        shorts = {leg.symbol for leg in preview.legs if leg.side == "Sell"}
        self.engine.chain = [item for item in self.sunday_chain if item.symbol in shorts]
        condor = (await self.request()).json()
        self.assertEqual(condor["reason_code"], "missing_protective_wings")
        self.assertEqual(len(condor["chain"]["items"]), 2)
        self.assertEqual((await self.request("/api/dashboard/market?strategy_mode=short_strangle")).json()["status"], "ready")
        self.engine.chain = [item for item in self.sunday_chain if item.option_type == "Call"]
        missing = (await self.request()).json()
        self.assertEqual(missing["reason_code"], "missing_option_side")
        self.assertTrue(missing["chain"]["items"])

    async def test_strategy_unavailability_never_disguises_stale_or_invalid_market(self):
        self.engine.chain = self.unavailable_chain()
        for source, updated in (("bybit", self.now - timedelta(minutes=5)), ("bybit", None),
                                ("bybit", self.now + timedelta(seconds=1)), ("unavailable", self.now)):
            with self.subTest(source=source, updated=updated):
                self.engine.chain_source, self.engine.chain_updated_at = source, updated
                response = await self.request()
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("preview", response.json())
        self.engine.chain_source, self.engine.chain_updated_at = "bybit", self.now
        with patch.object(self.engine, "make_preview", new=AsyncMock(side_effect=ValueError("Invalid margin configuration"))):
            self.assertEqual((await self.request()).status_code, 422)

    async def test_read_only_market_cannot_create_or_start_opening_but_close_plan_remains_available(self):
        self.engine.chain = self.sunday_chain
        original = await self.engine.prepare_trade_plan(TradePlanRequest(operation="open", quantity=.01))
        self.engine.chain = self.unavailable_chain()
        self.assertEqual((await self.request()).json()["status"], "strategy_unavailable")
        with self.assertRaises(StrategyUnavailable):
            await self.engine.prepare_trade_plan(TradePlanRequest(operation="open", quantity=.01))
        with self.assertRaises(TradeConflict):
            await self.engine.start_trade_task(TradeTaskRequest(plan_id=original["plan_id"], request_id="old-plan"))
        self.assertFalse(self.engine.trade_workers)
        self.assertFalse(self.engine.order_journal)
        self.assertFalse(self.engine.execution_groups)
        self.engine.settings.bybit_api_key = SecretStr("test-key")
        self.engine.settings.bybit_api_secret = SecretStr("test-secret")
        self.engine.client.rfq_config = AsyncMock(return_value={"counterparties": ["TEST"]})
        self.engine.client.create_rfq = AsyncMock()
        with self.assertRaises(StrategyUnavailable):
            await self.engine.create_rfq(RfqCreateRequest(quantity=.01))
        self.engine.client.create_rfq.assert_not_awaited()
        leg = self.engine.chain[0]
        self.engine.positions = [Position(symbol=leg.symbol, side="Sell", size=.01, avg_price=leg.mark_price,
                                           mark_price=leg.mark_price, unrealised_pnl=0, source="demo")]
        closing = await self.engine.prepare_trade_plan(TradePlanRequest(operation="close"))
        self.assertEqual(closing["legs"][0]["symbol"], leg.symbol)
        self.assertEqual(closing["legs"][0]["side"], "Buy")
        self.assertEqual(closing["legs"][0]["qty"], .01)

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
