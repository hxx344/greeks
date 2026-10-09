import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from pydantic import ValidationError

from app.config import Settings
from app.engine import TradingEngine
from app.main import app
from app.models import CloseRequest, OpenRequest, OptionInstrument, OrderResult, RfqCreateRequest, RfqExecuteRequest
from app.risk import reserve_short_margin, validate_structure
from app.strategy import build_iron_condor, build_strategy, demo_chain


def option_chain(now):
    expiry = demo_chain(now)[0].expiry
    return [OptionInstrument(symbol=f"BTC-{expiry:%d%b%y}-{strike}-{kind[0]}-USDT", expiry=expiry,
                             strike=strike, option_type=kind, delta=delta, mark_price=mark,
                             bid=mark - 1, ask=mark + 1)
            for kind, strike, delta, mark in (("Call", 105000, .45, 100), ("Put", 95000, -.45, 100),
                                              ("Call", 110000, .10, 20), ("Put", 90000, -.10, 20))]


class StrategyModeTests(unittest.TestCase):
    def test_compatible_defaults_and_invalid_modes_or_budgets(self):
        settings = Settings(_env_file=None)
        self.assertEqual(settings.strategy_mode, "iron_condor")
        self.assertEqual(settings.max_margin_usd, 2500)
        for model in (OpenRequest, RfqCreateRequest):
            self.assertIsNone(model().strategy_mode)
            with self.assertRaises(ValidationError):
                model(strategy_mode="two_legs")
        for values in ({"strategy_mode": "other"}, {"max_margin_usd": 0}, {"max_margin_usd": float("nan")}, {"max_margin_usd": float("inf")}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                Settings(_env_file=None, **values)

    def test_two_shorts_do_not_need_protective_wings_and_serialize_unbounded_loss_as_null(self):
        now = datetime.now(timezone.utc)
        options = option_chain(now)[:2]
        preview = build_strategy(options, now, qty=.1, index_price=100000, strategy_mode="short_strangle")
        self.assertEqual(len(preview.legs), 2)
        self.assertEqual({(leg.option_type, leg.side) for leg in preview.legs}, {("Call", "Sell"), ("Put", "Sell")})
        self.assertEqual({leg.target_delta for leg in preview.legs}, {.45})
        self.assertTrue(preview.unbounded_loss)
        self.assertIsNone(preview.max_loss_usd)
        self.assertIsNone(preview.risk_reward)
        self.assertEqual(preview.max_profit_usd, 20)
        json.dumps(preview.model_dump(mode="json"), allow_nan=False)
        with self.assertRaisesRegex(ValueError, "protective wings"):
            build_iron_condor(options, now, qty=.1)

    def test_pm_short_strangle_uses_regular_im_plus_buffer(self):
        now = datetime.now(timezone.utc)
        options = option_chain(now)
        regular = build_strategy(options, now, qty=.1, index_price=100000, strategy_mode="short_strangle", margin_buffer_pct=.2)
        pm = build_strategy(options, now, qty=.1, index_price=100000, strategy_mode="short_strangle", margin_buffer_pct=.2, margin_mode="PORTFOLIO_MARGIN")
        self.assertEqual(pm.margin_basis, "regular_order_im")
        self.assertEqual(pm.estimated_margin_usd, regular.estimated_margin_usd)
        self.assertEqual(pm.estimated_initial_margin_usd, 1001.41)
        self.assertEqual(pm.estimated_margin_usd, 1201.70)
        condor = build_iron_condor(options, now, qty=.1, index_price=100000, margin_buffer_pct=.2, margin_mode="PORTFOLIO_MARGIN")
        self.assertEqual(condor.margin_basis, "portfolio_loss_estimate")
        self.assertEqual(condor.estimated_margin_usd, round(condor.max_loss_usd * 1.2, 2))

    def test_missing_underlying_or_invalid_price_cannot_make_a_zero_margin_short_preview(self):
        now = datetime.now(timezone.utc)
        for price in (0, float("nan"), float("inf")):
            with self.subTest(price=price), self.assertRaises(ValueError):
                build_strategy(option_chain(now), now, index_price=price, strategy_mode="short_strangle")
        bad = option_chain(now)
        bad[0].ask = float("inf")
        with self.assertRaises(ValueError):
            build_strategy(bad, now, index_price=100000, strategy_mode="short_strangle")

    def test_mode_never_bypasses_side_quantity_expiry_or_strike_structure(self):
        expiry = demo_chain(datetime.now(timezone.utc))[0].expiry.isoformat()
        original = {"C": {"option_type": "Call", "side": "Sell", "qty": .1, "strike": 105000., "expiry": expiry},
                    "P": {"option_type": "Put", "side": "Sell", "qty": .1, "strike": 95000., "expiry": expiry}}
        mutations = [lambda legs: legs["C"].update(side="Buy"), lambda legs: legs["C"].update(qty=.2),
                     lambda legs: legs["P"].update(strike=106000.), lambda legs: legs["P"].update(expiry="2026-10-10T08:00:00+00:00"),
                     lambda legs: legs["P"].update(qty=float("nan")), lambda legs: legs.pop("C")]
        for mutate in mutations:
            legs = copy.deepcopy(original)
            mutate(legs)
            with self.assertRaises(ValueError):
                validate_structure(legs, "short_strangle", require_expiry=True)
        with self.assertRaises(ValueError):
            validate_structure(original, "iron_condor")


class ShortStrangleEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.json"
        self.settings = Settings(_env_file=None, state_file=str(self.path), trading_mode="testnet", bybit_api_key="test", bybit_api_secret="test",
                                 strategy_mode="iron_condor", leg_qty=.1, max_margin_usd=2500)
        self.settings.bbo_poll_seconds = .001
        self.settings.failed_leg_position_check_interval_seconds = .001
        self.settings.failed_leg_retry_delay_seconds = 0
        self.engine = TradingEngine(self.settings)
        self.now = datetime.now(timezone.utc)
        self.engine.chain = option_chain(self.now)
        self.engine.chain_source, self.engine.chain_updated_at, self.engine.btc_price = "bybit", self.now, 100000.
        self.engine.refresh_chain = AsyncMock(return_value=self.engine.chain)
        self.engine._validate_open_calendar = Mock()
        self.engine._capture_pm_baseline = AsyncMock()
        self.engine.load_recent_executions = AsyncMock(return_value=[])
        self.engine.client._request = AsyncMock(side_effect=AssertionError("Unexpected exchange call"))
        self.engine.client.tickers = AsyncMock(return_value=[{"bid1Price": "99", "ask1Price": "101"}])
        self.engine.client.place_limit_order = AsyncMock(return_value={"orderId": "placed"})
        self.engine.client.place_ioc_order = AsyncMock(return_value={"orderId": "ioc"})
        self.engine.client.amend_order = AsyncMock()
        self.engine.client.cancel_order = AsyncMock()
        self.engine.client.positions = AsyncMock(return_value=[])
        self.engine.client.order = AsyncMock(side_effect=lambda symbol, link: {
            "side": self.engine.order_journal[link]["side"], "qty": str(self.engine.order_journal[link]["qty"]),
            "cumExecQty": str(self.engine.order_journal[link]["qty"]), "orderStatus": "Filled"})
        self.engine.client.rfq_config = AsyncMock(return_value={"counterparties": ["DESK"], "strategyTypes": ["IronCondor", "ShortStrangle"]})
        self.engine.client.create_rfq = AsyncMock(return_value={"rfqId": "rfq-1", "status": "Active"})
        self.engine.client.execute_quote = AsyncMock(return_value={"rfqId": "rfq-1", "quoteId": "q1", "status": "PendingFill"})

    async def preview(self, **kwargs):
        return await self.engine.make_preview(.1, strategy_mode="short_strangle", **kwargs)

    async def risk_group(self, room=1000):
        preview = await self.preview()
        risk = self.engine._initial_execution_risk(preview)
        risk["risk_budget_usd"] = risk["risk_reserved_usd"] + room
        self.engine.execution_groups["opening"] = {"type": "open", "created_at": self.now.isoformat(),
                                                    "legs": {leg.symbol: {"side": leg.side, "qty": leg.qty} for leg in preview.legs}, **risk}
        self.engine.execution_group_links["ic-open"] = "opening"
        return preview, self.engine.execution_groups["opening"]

    async def rfq(self):
        await self.engine.create_rfq(RfqCreateRequest(quantity=.1, strategy_mode="short_strangle"))
        self.engine.rfq_state["quotes"] = [{"quoteId": "q1", "quoteSellList": [
            {"symbol": leg["symbol"], "qty": leg["qty"], "price": "101"} for leg in self.engine.rfq_state["legs"]]}]
        return RfqExecuteRequest(confirm_live=True, rfq_id="rfq-1", quote_id="q1", quote_side="Sell")

    async def test_preview_above_budget_stays_available_but_open_is_blocked(self):
        self.settings.max_margin_usd = 1
        preview = await self.preview()
        self.assertGreater(preview.estimated_margin_usd, 1)
        with self.assertRaisesRegex(ValueError, "Margin budget"):
            await self.engine.open_position(OpenRequest(confirm_live=True, quantity=.1, strategy_mode="short_strangle"))
        self.engine.client.place_limit_order.assert_not_awaited()

    async def test_manual_mode_override_is_not_persisted_as_scheduler_default(self):
        preview = await self.preview()
        self.assertEqual(preview.strategy_mode, "short_strangle")
        self.assertEqual(self.settings.strategy_mode, "iron_condor")
        self.assertEqual((await self.engine.make_preview(.1)).strategy_mode, "iron_condor")
        self.settings.auto_open = True
        self.engine.is_open_window = Mock(return_value=True)
        self.engine.open_position = AsyncMock()
        with patch("app.engine.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.engine.scheduler()
        request = self.engine.open_position.await_args.args[0]
        self.assertEqual(request.strategy_mode, "iron_condor")
        self.assertTrue(self.engine.open_position.await_args.kwargs["scheduled"])

    async def test_real_mocked_two_leg_open_close_preserves_mode_and_original_structure(self):
        results = await self.engine.open_position(OpenRequest(confirm_live=True, quantity=.1, strategy_mode="short_strangle"))
        self.assertEqual([item.status for item in results], ["filled", "filled"])
        self.assertEqual(self.engine.client.place_limit_order.await_count, 2)
        group_id = self.engine.active_strategy_group_id
        group = self.engine.execution_groups[group_id]
        self.assertEqual(group["strategy_mode"], "short_strangle")
        self.assertEqual(len(group["legs"]), 2)
        self.assertEqual(group["risk_context"]["index_price"], 100000)
        restored = TradingEngine(self.settings)
        self.assertEqual(restored.execution_groups[group_id]["strategy_mode"], "short_strangle")
        self.assertEqual(len(restored.active_strategy_sizes), 2)
        self.engine.client.positions.return_value = [{"symbol": symbol, "side": "Sell", "size": ".1"} for symbol in self.engine.active_strategy_symbols]
        await self.engine.make_preview(.1, strategy_mode="iron_condor")
        closing, _ = await self.engine.close_position(CloseRequest(confirm_live=True))
        self.assertEqual(len(closing), 2)
        self.assertEqual({item.side for item in closing}, {"Buy"})
        self.assertFalse(self.engine.active_strategy_symbols)
        closes = [value for value in self.engine.execution_groups.values() if value["type"] == "close"]
        self.assertEqual(closes[0]["strategy_mode"], "short_strangle")

    async def test_scheduled_open_uses_configured_mode_even_when_request_contains_an_override(self):
        self.engine.is_open_window = Mock(return_value=True)
        results = await self.engine.open_position(OpenRequest(confirm_live=True, quantity=.1, strategy_mode="short_strangle"), scheduled=True)
        self.assertEqual(len(results), 4)
        self.assertEqual(self.engine.execution_groups[self.engine.active_strategy_group_id]["strategy_mode"], "iron_condor")

    async def test_simulated_close_does_not_depend_on_current_preview_mode(self):
        self.settings.trading_mode = "dry-run"
        await self.engine.open_position(OpenRequest(quantity=.1, strategy_mode="short_strangle"))
        await self.engine.make_preview(.1, strategy_mode="iron_condor")
        closing, _ = await self.engine.close_position(CloseRequest())
        self.assertEqual(len(closing), 2)
        self.assertEqual(self.engine.positions, [])
        self.engine.client.place_limit_order.assert_not_awaited()

    async def test_short_mode_does_not_bypass_four_leg_shape_or_requested_quantity(self):
        preview = await self.engine.make_preview(.1)
        preview.strategy_mode, preview.unbounded_loss, preview.max_loss_usd, preview.risk_reward = "short_strangle", True, None, None
        self.engine.make_preview = AsyncMock(return_value=preview)
        with self.assertRaisesRegex(ValueError, "exactly two"):
            await self.engine.open_position(OpenRequest(confirm_live=True, quantity=.1, strategy_mode="short_strangle"))
        self.engine.client.place_limit_order.assert_not_awaited()
        preview.legs = preview.legs[:2]
        preview.legs[0].qty = .2
        with self.assertRaisesRegex(ValueError, "quantity"):
            await self.engine.open_position(OpenRequest(confirm_live=True, quantity=.1, strategy_mode="short_strangle"))

    async def test_budget_reservations_accumulate_and_price_improvements_never_release_margin(self):
        preview, group = await self.risk_group(room=6)
        first, second = (leg.symbol for leg in preview.legs)
        self.engine._reserve_execution_price("ic-open", first, 50)
        reserve = group["risk_reserved_usd"]
        self.engine._reserve_execution_price("ic-open", first, 101)
        self.assertEqual(group["risk_reserved_usd"], reserve)
        self.assertEqual(group["risk_legs"][first]["price_floor"], 50)
        with self.assertRaisesRegex(ValueError, "margin budget"):
            self.engine._reserve_execution_price("ic-open", second, 50)
        self.assertTrue(group["risk_blocked"])

    async def test_favorable_underlying_move_cannot_release_previous_margin_reserve(self):
        preview, group = await self.risk_group()
        self.engine.btc_price = 102000
        self.engine._reserve_execution_price("ic-open", preview.legs[0].symbol, None)
        reserve = group["risk_reserved_usd"]
        self.engine.btc_price = 100000
        self.engine._reserve_execution_price("ic-open", preview.legs[0].symbol, None)
        self.assertEqual(group["risk_reserved_usd"], reserve)

    async def test_stricter_margin_configuration_applies_to_saved_opening(self):
        preview, group = await self.risk_group()
        old = group["risk_reserved_usd"]
        self.settings.portfolio_margin_buffer_pct = .5
        self.engine._reserve_execution_price("ic-open", preview.legs[0].symbol, None)
        self.assertGreater(group["risk_reserved_usd"], old)
        self.settings.portfolio_margin_buffer_pct = 0
        reserve = group["risk_reserved_usd"]
        self.engine._reserve_execution_price("ic-open", preview.legs[0].symbol, None)
        self.assertEqual(group["risk_reserved_usd"], reserve)

    async def test_initial_live_price_budget_failure_submits_no_order(self):
        preview, group = await self.risk_group(room=.1)
        self.engine.client.tickers.return_value = [{"ask1Price": "1"}]
        result = await self.engine._execute_order(preview.legs[0], .1, "ic-open")
        self.assertEqual(result["status"], "not_submitted")
        self.engine.client.place_limit_order.assert_not_awaited()
        self.assertTrue(group["risk_blocked"])

    async def test_adverse_amendment_is_blocked_and_existing_order_is_reconciled(self):
        preview, _ = await self.risk_group(room=.1)
        self.engine.client.tickers.side_effect = [[{"ask1Price": "101"}], [{"ask1Price": "1"}]]
        self.engine.client.order.side_effect = [{"side": "Sell", "qty": ".1", "cumExecQty": "0", "orderStatus": "New"},
                                               {"side": "Sell", "qty": ".1", "cumExecQty": "0", "orderStatus": "Cancelled"}]
        result = await self.engine._execute_order(preview.legs[0], .1, "ic-open")
        self.engine.client.place_limit_order.assert_awaited_once()
        self.engine.client.amend_order.assert_not_awaited()
        self.engine.client.cancel_order.assert_awaited_once()
        self.assertEqual(result["status"], "timeout_cancelled")

    async def test_ioc_partial_fallback_uses_full_original_budget(self):
        preview, group = await self.risk_group(room=.1)
        leg = preview.legs[0]
        self.engine.order_journal["ic-open"] = {"symbol": leg.symbol, "side": "Sell", "qty": .1, "filledQty": .05,
                                                "status": "partial", "terminal": True, "reduce_only": False}
        self.engine.client.order.side_effect = None
        self.engine.client.order.return_value = {"side": "Sell", "qty": ".1", "cumExecQty": ".05", "orderStatus": "Cancelled"}
        self.engine.client.tickers.return_value = [{"bid1Price": "1"}]
        results = [OrderResult(symbol=leg.symbol, side="Sell", qty=.05, status="partial")]
        await self.engine._market_fallback([leg], .1, ["ic-open"], results, "opening", ["ic-open"])
        self.engine.client.place_ioc_order.assert_not_awaited()
        self.assertEqual(group["risk_legs"][leg.symbol]["qty"], .1)
        self.assertEqual(results[0].qty, .05)

    async def test_stale_or_missing_market_blocks_short_repricing_even_without_a_new_price(self):
        preview, group = await self.risk_group()
        self.engine.chain_updated_at -= timedelta(minutes=5)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.engine._reserve_execution_price("ic-open", preview.legs[0].symbol, None)
        self.assertTrue(group["risk_blocked"])

    async def test_runtime_side_cannot_diverge_from_short_opening(self):
        preview, _ = await self.risk_group()
        leg = preview.legs[0].model_copy(update={"side": "Buy"})
        result = await self.engine._execute_order(leg, .1, "ic-open")
        self.assertEqual(result["status"], "not_submitted")
        self.engine.client.place_limit_order.assert_not_awaited()

    async def test_short_rfq_is_custom_and_mode_is_fixed_at_creation(self):
        request = await self.rfq()
        self.assertEqual(self.engine.client.create_rfq.await_args.args[3], "custom")
        self.assertEqual(len(self.engine.client.create_rfq.await_args.args[1]), 2)
        self.settings.strategy_mode = "iron_condor"
        result = await self.engine.execute_rfq(request)
        self.assertEqual(result["strategy_mode"], "short_strangle")
        self.engine.client.execute_quote.assert_awaited_once()
        restored = TradingEngine(self.settings)
        self.assertEqual(restored.rfq_state["strategy_mode"], "short_strangle")
        self.assertIn("market_timestamp", restored.rfq_state["risk_context"])

    async def test_rfq_quotes_must_match_saved_short_shape_and_complete_quantity(self):
        request = await self.rfq()
        original = copy.deepcopy(self.engine.rfq_state)
        for mutate in (lambda state: state["quotes"][0]["quoteSellList"].pop(),
                       lambda state: state["quotes"][0]["quoteSellList"][0].update(qty=".2"),
                       lambda state: state["quotes"][0]["quoteSellList"][0].update(price="nan"),
                       lambda state: state["legs"][0].update(side="Buy"),
                       lambda state: state.update(strategy_mode="iron_condor"),
                       lambda state: state.pop("risk_context"),
                       lambda state: state.pop("risk_budget_usd"),
                       lambda state: state["risk_legs"][next(iter(state["risk_legs"]))].update(qty=.2)):
            self.engine.rfq_state = copy.deepcopy(original)
            mutate(self.engine.rfq_state)
            with self.assertRaises(ValueError):
                await self.engine.execute_rfq(request)
        self.engine.client.execute_quote.assert_not_awaited()

    async def test_rfq_margin_budget_is_rechecked_after_account_baseline_await(self):
        request = await self.rfq()
        async def market_moved(_):
            self.engine.btc_price = 250000
        self.engine._capture_pm_baseline.side_effect = market_moved
        with self.assertRaisesRegex(ValueError, "margin budget"):
            await self.engine.execute_rfq(request)
        self.engine.client.execute_quote.assert_not_awaited()

    async def test_filled_short_rfq_recovers_after_restart_without_promoting_condor_residuals(self):
        await self.rfq()
        self.engine.rfq_state.update(status="Filled", selected_quote_id="q1", execution_resolved=True)
        self.engine._save_state()
        restored = TradingEngine(self.settings)
        self.assertTrue(restored._track_filled_rfq())
        self.assertEqual(len(restored.active_strategy_symbols), 2)
        self.assertEqual(restored.execution_groups[restored.active_strategy_group_id]["strategy_mode"], "short_strangle")
        restored.active_strategy_symbols.clear()
        restored.active_strategy_sizes.clear()
        restored.active_strategy_group_id = None
        restored.execution_groups.clear()
        restored.rfq_state.pop("strategy_mode")
        restored.rfq_state.pop("tracking_applied")
        restored.settings.strategy_mode = "short_strangle"
        self.assertFalse(restored._track_filled_rfq())
        self.assertFalse(restored.active_strategy_symbols)

    async def test_short_legacy_task_recovery_requires_explicit_mode_and_complete_original_structure(self):
        preview = await self.preview()
        group = {"type": "open", "strategy_mode": "short_strangle", "created_at": self.now.isoformat(),
                 "legs": {leg.symbol: {"side": leg.side, "qty": leg.qty} for leg in preview.legs}}
        positions = self.engine._parse_positions([{"symbol": leg.symbol, "side": leg.side, "size": ".1"} for leg in preview.legs])
        self.engine.execution_groups["legacy"] = group
        self.assertTrue(self.engine._recover_tracked_open_positions(positions))
        self.engine.active_strategy_symbols.clear()
        self.engine.active_strategy_sizes.clear()
        self.engine.active_strategy_group_id = None
        group.pop("strategy_mode")
        self.settings.strategy_mode = "short_strangle"
        self.assertFalse(self.engine._recover_tracked_open_positions(positions))

    async def test_unknown_persisted_mode_blocks_state_loading(self):
        await self.risk_group()
        self.engine._save_state()
        payload = json.loads(self.path.read_text())
        payload["execution_groups"]["opening"]["strategy_mode"] = "unsupported"
        self.path.write_text(json.dumps(payload))
        restored = TradingEngine(self.settings)
        self.assertIsNotNone(restored.state_error)

    async def test_api_exposes_mode_budget_and_nullable_risk_without_changing_configuration(self):
        with patch("app.main.engine", self.engine), patch("app.main.settings", self.settings):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                for route in ("/api/strategy/preview", "/api/dashboard/market"):
                    result = await client.get(route, params={"quantity": .1, "strategy_mode": "short_strangle"})
                    self.assertEqual(result.status_code, 200)
                    payload = result.json().get("preview", result.json())
                    self.assertEqual(payload["strategy_mode"], "short_strangle")
                    self.assertIsNone(payload["max_loss_usd"])
                    self.assertEqual((await client.get(route, params={"strategy_mode": "unknown"})).status_code, 422)
                config = (await client.get("/api/config")).json()
                self.assertEqual(config["strategy_mode"], "iron_condor")
                self.assertEqual(config["max_margin_usd"], 2500)
                for route in ("/api/trading/open", "/api/rfq/create"):
                    self.assertEqual((await client.post(route, json={"strategy_mode": "unknown"})).status_code, 422)

    async def test_market_refresh_accepts_two_valid_contracts_without_requiring_protective_wings(self):
        del self.engine.refresh_chain
        legs = self.engine.chain[:2]
        self.engine.client.instruments = AsyncMock(return_value=[{"symbol": leg.symbol, "deliveryTime": int(leg.expiry.timestamp() * 1000),
                                                                  "strikePrice": str(leg.strike), "optionsType": leg.option_type} for leg in legs])
        self.engine.client.tickers.return_value = [{"symbol": leg.symbol, "delta": str(leg.delta), "markPrice": "100", "bid1Price": "99", "ask1Price": "101"} for leg in legs]
        self.engine.client.underlying_ticker = AsyncMock(return_value={"indexPrice": "100000"})
        await self.engine.refresh_chain(force=True, refresh_instruments=True)
        self.assertEqual(len(self.engine.chain), 2)
        self.assertEqual((await self.preview()).strategy_mode, "short_strangle")
