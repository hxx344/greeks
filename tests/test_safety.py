import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.engine import TradingEngine
from app.main import app
from app.models import CloseRequest, OpenRequest, Position, RfqCreateRequest, RfqExecuteRequest
from app.strategy import build_iron_condor, demo_chain


class SafetyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.engine = TradingEngine(Settings(
            _env_file=None, state_file=f"{directory.name}/state.json",
            live_trading=True, bybit_testnet=False,
            bybit_api_key="test", bybit_api_secret="test",
        ))
        now = datetime.now(timezone.utc)
        self.engine.chain = demo_chain(now)
        self.engine.chain_source = "bybit"
        self.engine.chain_updated_at = now
        self.preview = build_iron_condor(self.engine.chain, now, qty=0.01)
        self.engine.make_preview = AsyncMock(return_value=self.preview)
        self.engine.refresh_chain = AsyncMock(return_value=self.engine.chain)
        self.engine._validate_open_calendar = Mock()
        self.engine._capture_pm_baseline = AsyncMock()
        self.engine.follow_bbo_order = AsyncMock(return_value={"status": "filled"})
        self.engine.client.execute_quote = AsyncMock(return_value={"status": "PendingFill"})
        self.engine.rfq_state = {
            "rfq_id": "rfq-1", "status": "Active",
            "legs": [{"symbol": leg.symbol, "side": leg.side, "qty": str(leg.qty)} for leg in self.preview.legs],
            "quotes": [{"quoteId": "quote-1", "quoteSellList": [
                {"symbol": leg.symbol, "qty": str(leg.qty), "price": str(leg.mark_price)}
                for leg in self.preview.legs
            ]}],
        }
        self.rfq_request = RfqExecuteRequest(confirm_live=True, rfq_id="rfq-1", quote_id="quote-1", quote_side="Sell")

    def test_rfq_respects_all_live_switches(self):
        for field, value in (("live_trading", False), ("bybit_testnet", True), ("bybit_api_key", ""), ("bybit_api_secret", "")):
            with self.subTest(field=field), patch.object(self.engine.settings, field, value):
                with self.assertRaisesRegex(ValueError, "disabled"):
                    asyncio.run(self.engine.execute_rfq(self.rfq_request))
        self.engine.client.execute_quote.assert_not_awaited()

    def test_live_open_and_close_require_confirmation(self):
        for method, request in ((self.engine.open_position, OpenRequest()), (self.engine.close_position, CloseRequest())):
            with self.assertRaisesRegex(ValueError, "confirmation"):
                asyncio.run(method(request))
        self.engine.make_preview.assert_not_awaited()

    def test_stale_snapshot_blocks_open_and_rfq(self):
        self.engine.chain_updated_at -= timedelta(seconds=60)
        for method, request in ((self.engine.open_position, OpenRequest(confirm_live=True, quantity=0.01)), (self.engine.execute_rfq, self.rfq_request)):
            with self.assertRaisesRegex(ValueError, "stale"):
                asyncio.run(method(request))
        self.engine.follow_bbo_order.assert_not_awaited()
        self.engine.client.execute_quote.assert_not_awaited()

    def test_maximum_loss_is_checked_even_when_margin_is_low(self):
        self.preview.max_loss_usd = 2501
        self.preview.estimated_margin_usd = 1
        with self.assertRaisesRegex(ValueError, "Risk limit"):
            asyncio.run(self.engine.open_position(OpenRequest(confirm_live=True, quantity=0.01)))
        self.engine.follow_bbo_order.assert_not_awaited()

    def test_rfq_rejects_incomplete_quote(self):
        self.engine.rfq_state["quotes"][0]["quoteSellList"].pop()
        with self.assertRaisesRegex(ValueError, "four requested"):
            asyncio.run(self.engine.execute_rfq(self.rfq_request))
        self.engine.client.execute_quote.assert_not_awaited()

    def test_rfq_rejects_changed_quantity_and_invalid_price(self):
        quote_leg = self.engine.rfq_state["quotes"][0]["quoteSellList"][0]
        for change in ({"qty": "0.02"}, {"price": "nan"}, {"price": "-1"}):
            with self.subTest(change=change), patch.dict(quote_leg, change):
                with self.assertRaisesRegex(ValueError, "invalid price or mismatched quantity"):
                    asyncio.run(self.engine.execute_rfq(self.rfq_request))
        self.engine.client.execute_quote.assert_not_awaited()

    def test_rfq_rejects_expired_quote(self):
        self.engine.rfq_state["quotes"][0]["expiresAt"] = "1"
        with self.assertRaisesRegex(ValueError, "expired"):
            asyncio.run(self.engine.execute_rfq(self.rfq_request))
        self.engine.client.execute_quote.assert_not_awaited()

    def test_rfq_checks_actual_quote_loss(self):
        for leg, quoted in zip(self.preview.legs, self.engine.rfq_state["quotes"][0]["quoteSellList"]):
            quoted["price"] = "200000" if leg.side == "Buy" else "1"
        with self.assertRaisesRegex(ValueError, "maximum loss"):
            asyncio.run(self.engine.execute_rfq(self.rfq_request))
        self.engine.client.execute_quote.assert_not_awaited()

    def test_concurrent_rfq_execution_submits_once(self):
        async def run():
            return await asyncio.gather(self.engine.execute_rfq(self.rfq_request), self.engine.execute_rfq(self.rfq_request), return_exceptions=True)
        results = asyncio.run(run())
        self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)
        self.engine.client.execute_quote.assert_awaited_once()

    def test_uncertain_rfq_execution_is_persisted_and_not_retried(self):
        self.engine.client.execute_quote.side_effect = httpx.ReadTimeout("response lost")
        with self.assertRaises(httpx.ReadTimeout):
            asyncio.run(self.engine.execute_rfq(self.rfq_request))
        restored = TradingEngine(self.engine.settings)
        self.assertEqual(restored.rfq_state["status"], "ExecutionUnknown")
        with self.assertRaisesRegex(ValueError, "no longer available"):
            asyncio.run(restored.execute_rfq(self.rfq_request))
        self.engine.client.execute_quote.assert_awaited_once()

    def test_active_rfq_cannot_be_overwritten(self):
        with self.assertRaisesRegex(ValueError, "already active"):
            asyncio.run(self.engine.create_rfq(RfqCreateRequest(quantity=0.01)))

    def test_simulated_close_preserves_live_tracking(self):
        self.engine.settings.live_trading = False
        self.engine.preview = self.preview
        self.engine.active_strategy_symbols = {"LIVE"}
        self.engine.active_strategy_sizes = {"LIVE|Buy": 0.03}
        self.engine.active_strategy_group_id = "live-group"
        self.engine.positions = [Position(symbol=leg.symbol, side=leg.side, size=leg.qty, avg_price=leg.mark_price, mark_price=leg.mark_price, unrealised_pnl=0) for leg in self.preview.legs]
        asyncio.run(self.engine.close_position(CloseRequest()))
        self.assertEqual(self.engine.positions, [])
        self.assertEqual(self.engine.active_strategy_symbols, {"LIVE"})
        self.assertEqual(self.engine.active_strategy_sizes, {"LIVE|Buy": 0.03})
        self.assertEqual(self.engine.active_strategy_group_id, "live-group")

    def test_quantity_models_reject_nonfinite_numbers(self):
        for model in (OpenRequest, RfqCreateRequest):
            for value in (float("inf"), float("nan"), -1, 0):
                with self.subTest(model=model, value=value), self.assertRaises(ValidationError):
                    model(quantity=value)

    def test_settings_reject_invalid_hour_and_nonfinite_risk(self):
        for values in ({"open_hour_utc": 24}, {"max_risk_usd": float("inf")}, {"recv_window_ms": 0}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                Settings(_env_file=None, **values)

    def test_http_quantity_validation_rejects_nonfinite_input(self):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                for path in ("/api/strategy/preview", "/api/dashboard/market"):
                    for value in ("inf", "nan", "0", "-1"):
                        response = await client.get(path, params={"quantity": value})
                        self.assertEqual(response.status_code, 422)
                for path in ("/api/trading/open", "/api/rfq/create"):
                    response = await client.post(path, json={"quantity": "Infinity"})
                    self.assertEqual(response.status_code, 422)
        asyncio.run(run())

    def test_strategy_ignores_missing_market_prices(self):
        bad_wing = self.engine.chain[0].model_copy(update={"symbol": "NO-QUOTE", "delta": 0.1, "mark_price": 0})
        result = build_iron_condor([bad_wing, *self.engine.chain], datetime.now(timezone.utc), qty=0.01)
        self.assertNotIn("NO-QUOTE", {leg.symbol for leg in result.legs})

    def test_strategy_reports_missing_option_type(self):
        calls = [item for item in self.engine.chain if item.option_type == "Call"]
        with self.assertRaisesRegex(ValueError, "both calls and puts"):
            build_iron_condor(calls, datetime.now(timezone.utc))

    def test_strategy_rejects_crossed_short_strikes(self):
        options = [item.model_copy(update={"delta": 0.45 if (item.option_type == "Call" and item.strike == 82000) or (item.option_type == "Put" and item.strike == 117000) else 0.1}) for item in self.engine.chain]
        with self.assertRaisesRegex(ValueError, "short put strike"):
            build_iron_condor(options, datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
