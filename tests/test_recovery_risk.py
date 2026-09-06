import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.config import Settings
from app.engine import TradingEngine
from app.main import app
from app.models import OpenRequest
from app.orders import OrderExecutor
from app.risk import maximum_loss
from app.strategy import build_iron_condor, demo_chain


class RecoveryRiskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        self.settings = Settings(_env_file=None, state_file=str(self.path), live_trading=True,
                                 bybit_testnet=False, bybit_api_key="test", bybit_api_secret="test")
        self.engine = TradingEngine(self.settings)
        now = datetime.now(timezone.utc)
        self.engine.chain = demo_chain(now)
        self.preview = build_iron_condor(self.engine.chain, now, qty=0.01)
        self.engine.rfq_state = {"rfq_id": "rfq-1", "status": "Filled", "selected_quote_id": "quote-1",
                                 "legs": [{"symbol": leg.symbol, "side": leg.side, "qty": str(leg.qty)} for leg in self.preview.legs]}

    async def test_rfq_refresh_does_not_restore_partially_closed_quantities(self):
        self.assertTrue(self.engine._track_filled_rfq())
        leg = self.preview.legs[0]
        key = f"{leg.symbol}|{leg.side}"
        self.engine.active_strategy_sizes[key] = 0.004
        self.engine._save_state()
        self.engine.client.rfq_realtime = AsyncMock(return_value=[{"status": "Filled"}])
        self.engine.client.quote_realtime = AsyncMock(return_value=[])
        await self.engine.refresh_rfq()
        self.assertEqual(self.engine.active_strategy_sizes[key], 0.004)
        restored = TradingEngine(self.settings)
        self.assertFalse(restored._track_filled_rfq())
        self.assertEqual(restored.active_strategy_sizes[key], 0.004)

    async def test_rfq_refresh_does_not_recreate_closed_strategy(self):
        self.engine._track_filled_rfq()
        self.engine.active_strategy_sizes.clear()
        self.engine.active_strategy_symbols.clear()
        self.engine.active_strategy_group_id = None
        self.assertFalse(self.engine._track_filled_rfq())
        self.assertEqual(self.engine.active_strategy_sizes, {})

    async def test_legacy_rfq_partial_tracking_is_preserved(self):
        leg = self.preview.legs[0]
        self.engine.active_strategy_group_id = "rfq:rfq-1"
        self.engine.active_strategy_symbols = {leg.symbol}
        self.engine.active_strategy_sizes = {f"{leg.symbol}|{leg.side}": 0.003}
        self.assertFalse(self.engine._track_filled_rfq())
        self.assertEqual(list(self.engine.active_strategy_sizes.values()), [0.003])
        self.assertTrue(self.engine.rfq_state["tracking_applied"])

    async def test_legacy_rfq_close_record_prevents_recovery(self):
        self.engine.execution_groups["closed"] = {"type": "close", "opening_group": "rfq:rfq-1"}
        self.assertFalse(self.engine._track_filled_rfq())
        self.assertFalse(self.engine.active_strategy_symbols)

    async def test_corrupt_state_blocks_trading_and_is_not_overwritten(self):
        for raw in ("{broken", "[]", "{}", '{"schema_version":2}', '{"last_open_week":null,"active_strategy_symbols":[],"active_strategy_sizes":{},"pm_baseline":{"account_im":NaN}}'):
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                engine = TradingEngine(self.settings)
                engine.make_preview = AsyncMock()
                self.assertIsNotNone(engine.state_error)
                with self.assertRaisesRegex(ValueError, "State file"):
                    await engine.open_position(OpenRequest(confirm_live=True, quantity=0.01))
                with self.assertRaises(ValueError):
                    engine._save_state()
                engine.make_preview.assert_not_awaited()
                self.assertEqual(self.path.read_text(), raw)

    async def test_structurally_invalid_state_does_not_partially_load(self):
        self.engine.last_open_week = "2026-W36"
        self.engine._save_state()
        valid = json.loads(self.path.read_text())
        invalid_values = ({"active_strategy_sizes": {"BTC|Buy": -1}}, {"execution_groups": {"bad": []}},
                          {"rfq_state": {"legs": [{"symbol": "BTC", "side": "Buy", "qty": None}]}},
                          {"order_journal": {"bad": {"symbol": "BTC", "side": "Buy", "qty": 1, "reduce_only": False,
                                                    "status": "unknown", "terminal": "false", "filledQty": 0}}})
        for value in invalid_values:
            with self.subTest(value=value):
                self.path.write_text(json.dumps({**valid, **value}))
                restored = TradingEngine(self.settings)
                self.assertIsNotNone(restored.state_error)
                self.assertIsNone(restored.last_open_week)

    async def test_valid_legacy_state_retains_week_marker(self):
        self.engine.last_open_week = "2026-W36"
        self.engine._save_state()
        state = json.loads(self.path.read_text())
        state.pop("schema_version")
        state.pop("order_journal")
        self.path.write_text(json.dumps(state))
        restored = TradingEngine(self.settings)
        self.assertIsNone(restored.state_error)
        self.assertEqual(restored.last_open_week, "2026-W36")

    async def test_save_failure_blocks_further_trading_and_preserves_previous_file(self):
        self.engine._save_state()
        original = self.path.read_bytes()
        with patch("app.engine.os.fsync", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(ValueError, "could not be saved"):
                self.engine._save_state()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIsNotNone(self.engine.state_error)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    async def test_health_reports_state_failure(self):
        with patch("app.main.engine.state_error", "State file could not be loaded"):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                payload = (await client.get("/api/health")).json()
                self.assertEqual(payload["status"], "degraded")
                self.assertIn("State file", payload["trading_blocked_reason"])

    def risk_group(self):
        legs = {
            "LP": {"side": "Buy", "option_type": "Put", "strike": 90., "qty": 1., "price_bound": 1.},
            "SP": {"side": "Sell", "option_type": "Put", "strike": 100., "qty": 1., "price_bound": 3.},
            "SC": {"side": "Sell", "option_type": "Call", "strike": 110., "qty": 1., "price_bound": 3.},
            "LC": {"side": "Buy", "option_type": "Call", "strike": 120., "qty": 1., "price_bound": 1.},
        }
        self.engine.execution_groups["risk"] = {"type": "open", "risk_legs": legs}
        self.engine.execution_group_links["risk-link"] = "risk"
        self.settings.max_risk_usd = 8
        return legs

    async def test_risk_reservations_accumulate_across_legs(self):
        self.risk_group()
        self.engine._reserve_execution_price("risk-link", "LP", 2)
        # Each +1.5 would pass against the initial prices, but the combined
        # worsening must exceed the shared remaining risk budget.
        with self.assertRaisesRegex(ValueError, "maximum loss limit"):
            self.engine._reserve_execution_price("risk-link", "LC", 2.5)
        self.assertTrue(self.engine.execution_groups["risk"]["risk_blocked"])
        with self.assertRaisesRegex(ValueError, "must stop"):
            self.engine._reserve_execution_price("risk-link", "SP", None)

    async def test_price_improvement_does_not_release_unconfirmed_old_limit_budget(self):
        self.risk_group()
        self.engine._reserve_execution_price("risk-link", "LP", 2)
        self.engine._reserve_execution_price("risk-link", "LP", 1)
        self.assertEqual(self.engine.execution_groups["risk"]["risk_legs"]["LP"]["price_bound"], 2)

    async def test_risk_calculation_matches_preview_for_same_prices(self):
        bounds = {leg.symbol: {"side": leg.side, "option_type": leg.option_type, "strike": leg.strike,
                              "qty": leg.qty, "price_bound": leg.mark_price} for leg in self.preview.legs}
        self.assertAlmostEqual(maximum_loss(bounds), self.preview.max_loss_usd, places=2)

    async def test_guard_failure_cancels_live_order_before_adverse_amendment(self):
        instrument = self.engine.chain[0]
        client = Mock()
        client.tickers = AsyncMock(side_effect=[[{"bid1Price": "1"}], [{"bid1Price": "100"}]])
        client.place_limit_order = AsyncMock(return_value={"orderId": "o"})
        client.amend_order = AsyncMock()
        client.cancel_order = AsyncMock()
        client.order = AsyncMock(side_effect=[{"side": "Buy", "qty": "1", "cumExecQty": "0", "orderStatus": "New"},
                                               {"side": "Buy", "qty": "1", "cumExecQty": "0", "orderStatus": "Cancelled"}])
        self.settings.bbo_poll_seconds = 0
        def guard(price):
            if price is not None and price > 2:
                raise ValueError("Risk limit")
        result = await OrderExecutor(client, self.settings, self.engine.log).execute(instrument, "Buy", 1, "link", Mock(), check_price=guard)
        client.amend_order.assert_not_awaited()
        client.cancel_order.assert_awaited_once()
        self.assertEqual(result["status"], "timeout_cancelled")

    async def test_ioc_fallback_uses_checked_limit_price(self):
        client = Mock()
        client.tickers = AsyncMock(return_value=[{"ask1Price": "10.01"}])
        client.place_ioc_order = AsyncMock(return_value={"orderId": "o"})
        client.order = AsyncMock(return_value={"side": "Buy", "qty": "1", "cumExecQty": "1", "orderStatus": "Filled"})
        guard = Mock()
        result = await OrderExecutor(client, self.settings, self.engine.log).execute(self.engine.chain[0], "Buy", 1, "link", Mock(), market=True, check_price=guard)
        guard.assert_called_once_with(10.01)
        self.assertEqual(client.place_ioc_order.await_args.args[3], 10.01)
        self.assertEqual(result["status"], "filled")

    async def test_ioc_risk_failure_does_not_submit(self):
        client = Mock()
        client.tickers = AsyncMock(return_value=[{"ask1Price": "1000"}])
        client.place_ioc_order = AsyncMock()
        result = await OrderExecutor(client, self.settings, self.engine.log).execute(self.engine.chain[0], "Buy", 1, "link", Mock(), market=True, check_price=Mock(side_effect=ValueError("Risk limit")))
        client.place_ioc_order.assert_not_awaited()
        self.assertEqual(result["status"], "not_submitted")


if __name__ == "__main__":
    unittest.main()
