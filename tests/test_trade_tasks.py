import asyncio
import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pydantic import ValidationError

from app.config import Settings
from app.engine import TradingEngine
from app.models import Position, RfqCreateRequest, TradePlanRequest, TradeTaskRequest
from app.orders import OrderExecutor
from app.strategy import demo_chain
from app.trade_tasks import TradeConflict


class TradeTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(_env_file=None, state_file=f"{directory.name}/state.json", trading_mode="testnet",
                                 bybit_api_key="test", bybit_api_secret="test", leg_qty=.03)
        self.settings.bbo_poll_seconds = .001
        self.settings.bbo_order_timeout_seconds = .04
        self.settings.failed_leg_position_checks = 1
        self.settings.failed_leg_retry_delay_seconds = .001
        self.engine = TradingEngine(self.settings)
        self.engine.chain = demo_chain(datetime.now(timezone.utc))
        self.engine.chain_source = "bybit"
        self.engine.chain_updated_at = datetime.now(timezone.utc)
        self.engine.btc_price = 100000.
        self.engine.refresh_chain = AsyncMock(return_value=self.engine.chain)
        self.engine._validate_open_calendar = Mock()
        self.engine._capture_pm_baseline = AsyncMock()
        self.engine.load_recent_executions = AsyncMock(return_value=[])
        self.engine.client = SimpleNamespace(
            tickers=AsyncMock(side_effect=self.quote), place_limit_order=AsyncMock(return_value={"orderId": "placed"}),
            place_ioc_order=AsyncMock(return_value={"orderId": "ioc"}), amend_order=AsyncMock(),
            cancel_order=AsyncMock(), order=AsyncMock(side_effect=self.filled_order), positions=AsyncMock(return_value=[]),
            rfq_config=AsyncMock(return_value={"counterparties": ["DESK"]}),
            create_rfq=AsyncMock(return_value={"rfqId": "rfq-1", "status": "Active"}),
        )
        self.client = self.engine.client

    def quote(self, symbol):
        item = next(item for item in self.engine.chain if item.symbol == symbol)
        return [{"bid1Price": str(item.bid), "ask1Price": str(item.ask)}]

    def filled_order(self, symbol, link):
        entry = self.engine.order_journal[link]
        return {"side": entry["side"], "qty": str(entry["qty"]), "cumExecQty": str(entry["qty"]), "orderStatus": "Filled"}

    async def plan(self, operation="open"):
        return await self.engine.prepare_trade_plan(TradePlanRequest(operation=operation, quantity=.03))

    def request(self, plan, **kwargs):
        return TradeTaskRequest(plan_id=plan["plan_id"], request_id="test-request", confirm_live=True, **kwargs)

    async def wait_task(self, response):
        worker = self.engine.trade_workers.get(response["execution_id"])
        if worker:
            await asyncio.wait_for(worker, timeout=2)
        return self.engine.execution_groups[response["execution_id"]]

    async def stopped_group(self, operation="open"):
        plan = await self.plan(operation)
        response = await self.engine.start_trade_task(self.request(plan))
        self.engine.stop_trade_task(response["execution_id"])
        return await self.wait_task(response)

    async def close_setup(self):
        plan = await self.plan()
        legs = deepcopy(plan["legs"])
        self.engine.execution_groups["opening"] = {"type": "open", "order_tracking": True, "strategy_mode": "iron_condor",
                                                    "legs": {leg["symbol"]: leg for leg in legs}}
        self.engine.active_strategy_group_id = "opening"
        self.engine.active_strategy_symbols = {leg["symbol"] for leg in legs}
        self.engine.active_strategy_sizes = {f"{leg['symbol']}|{leg['side']}": leg["qty"] for leg in legs}
        self.positions = [Position(symbol=leg["symbol"], side=leg["side"], size=leg["qty"], avg_price=leg["reference_price"],
                                   mark_price=leg["reference_price"], unrealised_pnl=0, source="bybit") for leg in legs]
        self.engine._sync_positions = AsyncMock(side_effect=lambda: self.positions)
        self.engine._tracking_needs_recovery = Mock(return_value=False)

    async def test_plan_uses_bbo_and_fees_not_mark_credit(self):
        plan = await self.plan()
        gross = sum((1 if leg["side"] == "Sell" else -1) * leg["reference_price"] * leg["qty"] for leg in plan["legs"])
        fees = sum(min(30, leg["reference_price"] * .07) * leg["qty"] for leg in plan["legs"])
        self.assertAlmostEqual(plan["estimated_gross_usd"], gross)
        self.assertAlmostEqual(plan["estimated_fee_usd"], fees)
        self.assertAlmostEqual(plan["estimated_net_usd"], gross - fees)
        self.assertNotEqual(plan["estimated_net_usd"], self.engine.preview.net_credit_usd)
        self.assertEqual((datetime.fromisoformat(plan["expires_at"]) - datetime.fromisoformat(plan["created_at"])).total_seconds(), 30)

    async def test_plan_uses_same_tick_rounding_as_executor(self):
        for item in self.engine.chain:
            item.bid += .004
            item.ask += .005
        plan = await self.plan()
        response = await self.engine.start_trade_task(self.request(plan))
        group = await self.wait_task(response)
        self.assertEqual(group["execution_status"], "completed")
        self.assertEqual(self.client.place_limit_order.await_count, 4)

    async def test_expired_plan_rejected_before_orders(self):
        plan = await self.plan()
        self.engine.trade_plans[plan["plan_id"]]["plan"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with self.assertRaises(TradeConflict):
            await self.engine.start_trade_task(self.request(plan))
        self.client.place_limit_order.assert_not_awaited()

    async def test_plan_expiry_is_rechecked_after_await(self):
        plan = await self.plan()
        async def expire(context):
            self.engine.trade_plans[plan["plan_id"]]["plan"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.engine._capture_pm_baseline.side_effect = expire
        with self.assertRaises(TradeConflict):
            await self.engine.start_trade_task(self.request(plan))
        self.assertFalse(self.engine.execution_groups)

    async def test_changed_selection_requires_new_confirmation(self):
        plan = await self.plan()
        preview = self.engine.preview.model_copy(deep=True)
        preview.legs[0].symbol = preview.legs[2].symbol
        self.engine.make_preview = AsyncMock(return_value=preview)
        with self.assertRaises(TradeConflict):
            await self.engine.start_trade_task(self.request(plan))
        self.client.place_limit_order.assert_not_awaited()

    async def test_delta_change_during_margin_await_rejects_final_candidate(self):
        plan = await self.plan()
        original_leg = next(leg for leg in plan["legs"] if leg["side"] == "Sell" and leg["option_type"] == "Call")
        original = next(item for item in self.engine.chain if item.symbol == original_leg["symbol"])
        replacement = min((item for item in self.engine.chain if item.option_type == "Call" and item.strike > original.strike),
                          key=lambda item: item.strike)
        async def change_market(context):
            original.delta = .05
            replacement.delta = .45
        self.engine._capture_pm_baseline.side_effect = change_market
        with self.assertRaisesRegex(TradeConflict, "Selected instruments changed"):
            await self.engine.start_trade_task(self.request(plan))
        self.engine._capture_pm_baseline.assert_awaited_once()
        self.assertFalse(self.engine.execution_groups)
        self.client.place_limit_order.assert_not_awaited()

    async def test_changed_metadata_requires_new_confirmation(self):
        plan = await self.plan()
        self.engine.chain[0].expiry += timedelta(days=7)
        changed = next(item for item in self.engine.chain if item.symbol == plan["legs"][0]["symbol"])
        changed.strike += 1
        with self.assertRaises(TradeConflict):
            await self.engine.start_trade_task(self.request(plan))

    async def test_environment_change_rejected(self):
        plan = await self.plan()
        self.settings.trading_mode = "dry-run"
        with self.assertRaises(TradeConflict):
            await self.engine.start_trade_task(self.request(plan))

    async def test_exchange_lot_metadata_change_is_a_conflict(self):
        plan = await self.plan()
        item = next(item for item in self.engine.chain if item.symbol == plan["legs"][0]["symbol"])
        item.qty_step = .02
        with self.assertRaises(TradeConflict):
            await self.engine.start_trade_task(self.request(plan))

    async def test_admission_is_durable_and_idempotent_without_duplicate_orders(self):
        plan = await self.plan()
        request = self.request(plan)
        response = await self.engine.start_trade_task(request)
        persisted = json.loads(Path(self.settings.state_file).read_text())
        self.assertEqual(persisted["execution_groups"][response["execution_id"]]["execution_status"], "accepted")
        self.assertEqual(await self.engine.start_trade_task(request), response)
        with self.assertRaises(TradeConflict):
            await self.engine.start_trade_task(request.model_copy(update={"min_net_income_usd": -100.}))
        self.assertEqual((await self.wait_task(response))["execution_status"], "completed")
        retried = await self.engine.start_trade_task(request)
        self.assertEqual(retried["execution_id"], response["execution_id"])
        self.assertEqual(self.client.place_limit_order.await_count, 4)
        with self.assertRaises(TradeConflict):
            await self.engine.start_trade_task(request.model_copy(update={"request_id": "another-request"}))

    async def test_accepted_task_blocks_new_task_and_rfq_before_journal_exists(self):
        plan = await self.plan()
        response = await self.engine.start_trade_task(self.request(plan))
        self.assertFalse(self.engine.order_journal)
        self.assertTrue(self.engine.trading_operation_active())
        with self.assertRaises(TradeConflict):
            await self.plan()
        with self.assertRaises(TradeConflict):
            await self.engine.create_rfq(RfqCreateRequest())
        self.engine.stop_trade_task(response["execution_id"])
        self.assertEqual((await self.wait_task(response))["execution_status"], "stopped")
        self.client.place_limit_order.assert_not_awaited()

    async def test_starting_requests_do_not_queue_behind_admission(self):
        plan = await self.plan()
        entered, release = asyncio.Event(), asyncio.Event()
        async def capture(context):
            entered.set()
            await release.wait()
        self.engine._capture_pm_baseline.side_effect = capture
        pending = asyncio.create_task(self.engine.start_trade_task(self.request(plan)))
        await entered.wait()
        with self.assertRaises(TradeConflict):
            await asyncio.wait_for(self.engine.create_rfq(RfqCreateRequest()), .1)
        release.set()
        response = await pending
        self.engine.stop_trade_task(response["execution_id"])
        await self.wait_task(response)

    async def test_stop_during_quote_await_prevents_submission(self):
        plan = await self.plan()
        entered, release = asyncio.Event(), asyncio.Event()
        async def tickers(symbol):
            entered.set()
            await release.wait()
            return self.quote(symbol)
        self.client.tickers.side_effect = tickers
        response = await self.engine.start_trade_task(self.request(plan))
        await entered.wait()
        self.assertTrue(self.engine.lock.locked())
        result = self.engine.stop_trade_task(response["execution_id"])
        self.assertEqual(result["execution_status"], "stopping")
        release.set()
        self.assertEqual((await self.wait_task(response))["execution_status"], "stopped")
        self.client.place_limit_order.assert_not_awaited()
        self.client.cancel_order.assert_not_awaited()

    async def test_stop_after_submit_cancels_and_accounts_racing_fill(self):
        plan = await self.plan()
        entered, release = asyncio.Event(), asyncio.Event()
        async def place(*args):
            entered.set()
            await release.wait()
            return {"orderId": "placed"}
        self.client.place_limit_order.side_effect = place
        self.client.order.side_effect = lambda symbol, link: {**self.filled_order(symbol, link), "cumExecQty": ".01", "orderStatus": "Cancelled"}
        response = await self.engine.start_trade_task(self.request(plan))
        await entered.wait()
        self.engine.stop_trade_task(response["execution_id"])
        release.set()
        group = await self.wait_task(response)
        self.assertEqual(group["execution_status"], "partial")
        self.assertEqual(self.client.cancel_order.await_count, 4)
        self.assertEqual(sum(self.engine.active_strategy_sizes.values()), .04)
        self.client.amend_order.assert_not_awaited()

    async def test_stop_wakes_long_bbo_sleep(self):
        self.settings.bbo_poll_seconds = 60
        self.settings.bbo_order_timeout_seconds = 120
        plan = await self.plan()
        entered = asyncio.Event()
        async def place(*args):
            entered.set()
            return {"orderId": "placed"}
        self.client.place_limit_order.side_effect = place
        self.client.order.side_effect = lambda symbol, link: {**self.filled_order(symbol, link), "cumExecQty": "0", "orderStatus": "Cancelled"}
        response = await self.engine.start_trade_task(self.request(plan))
        await entered.wait()
        self.engine.stop_trade_task(response["execution_id"])
        self.assertEqual((await self.wait_task(response))["execution_status"], "stopped")

    async def test_unknown_after_stop_blocks_until_exchange_terminal(self):
        plan = await self.plan()
        self.client.place_limit_order.side_effect = TimeoutError("response lost")
        self.client.order.return_value = None
        self.client.order.side_effect = None
        response = await self.engine.start_trade_task(self.request(plan))
        group = await self.wait_task(response)
        self.assertEqual(group["execution_status"], "recovery_needed")
        self.engine.stop_trade_task(response["execution_id"])
        with self.assertRaises(TradeConflict):
            await self.plan()
        self.client.order.side_effect = lambda symbol, link: {**self.filled_order(symbol, link), "cumExecQty": "0", "orderStatus": "Cancelled"}
        await self.engine._reconcile_pending_orders()
        self.assertEqual(group["execution_status"], "stopped")
        self.assertFalse(self.engine.trading_operation_active())

    async def test_restart_does_not_write_before_lease_or_replay_accepted_task(self):
        plan = await self.plan()
        response = await self.engine.start_trade_task(self.request(plan))
        original = Path(self.settings.state_file).read_bytes()
        restored = TradingEngine(self.settings)
        self.assertEqual(Path(self.settings.state_file).read_bytes(), original)
        group = restored.execution_groups[response["execution_id"]]
        self.assertEqual(group["execution_status"], "recovering")
        self.assertTrue(restored.trading_operation_active())
        self.assertFalse(restored.trade_workers)
        await restored._reconcile_pending_orders()
        self.assertEqual(group["execution_status"], "stopped")
        self.engine.stop_trade_task(response["execution_id"])
        await self.wait_task(response)

    async def test_dry_run_restart_finishes_without_sending_orders(self):
        self.settings.trading_mode = "dry-run"
        plan = await self.plan()
        response = await self.engine.start_trade_task(self.request(plan))
        restored = TradingEngine(self.settings)
        await restored.reconcile_once()
        self.assertEqual(restored.execution_groups[response["execution_id"]]["execution_status"], "stopped")
        self.engine.stop_trade_task(response["execution_id"])
        await self.wait_task(response)
        self.client.place_limit_order.assert_not_awaited()

    async def test_restart_unknown_task_only_reconciles_and_never_replays(self):
        plan = await self.plan()
        self.client.place_limit_order.side_effect = TimeoutError("lost")
        self.client.order.side_effect = None
        self.client.order.return_value = None
        response = await self.engine.start_trade_task(self.request(plan))
        await self.wait_task(response)
        restored = TradingEngine(self.settings)
        restored.client = self.client
        self.client.order.side_effect = lambda symbol, link: {**self.filled_order(symbol, link), "cumExecQty": ".01", "orderStatus": "Cancelled"}
        await restored._reconcile_pending_orders()
        self.assertEqual(restored.execution_groups[response["execution_id"]]["execution_status"], "partial")
        self.assertEqual(self.client.place_limit_order.await_count, 4)
        self.assertFalse(restored.trade_workers)

    async def test_corrupt_task_identity_timestamp_and_journal_are_rejected_on_load(self):
        plan = await self.plan()
        response = await self.engine.start_trade_task(self.request(plan))
        await self.wait_task(response)
        path = Path(self.settings.state_file)
        original = json.loads(path.read_text())
        for mutation in ("fingerprint", "timestamp", "journal"):
            with self.subTest(mutation=mutation):
                payload = deepcopy(original)
                group = payload["execution_groups"][response["execution_id"]]
                if mutation == "fingerprint":
                    group["request_fingerprint"]["plan_id"] = "different-plan"
                elif mutation == "timestamp":
                    group["accepted_at"] = "2026-10-10T00:00:00"
                else:
                    next(iter(payload["order_journal"].values()))["reduce_only"] = True
                path.write_text(json.dumps(payload))
                self.assertIsNotNone(TradingEngine(self.settings).state_error)

    async def test_open_net_guard_includes_fee_and_does_not_relax_old_bound(self):
        group = await self.stopped_group()
        sell = next(leg for leg in group["legs"].values() if leg["side"] == "Sell")
        group["min_net_income_usd"] -= 1
        self.engine._reserve_task_price(group, sell["symbol"], sell["reference_price"] - 5)
        reserved = group["reserved_net_usd"]
        self.engine._reserve_task_price(group, sell["symbol"], sell["reference_price"])
        self.assertLessEqual(group["reserved_net_usd"], reserved)
        group["min_net_income_usd"] = group["reserved_gross_usd"]
        with self.assertRaisesRegex(ValueError, "estimated fees"):
            self.engine._reserve_task_price(group, sell["symbol"], sell["reference_price"])

    async def test_open_price_limit_prevents_exchange_submission(self):
        plan = await self.plan()
        response = await self.engine.start_trade_task(self.request(plan, min_net_income_usd=plan["estimated_gross_usd"]))
        self.assertEqual((await self.wait_task(response))["execution_status"], "failed")
        self.client.place_limit_order.assert_not_awaited()

    async def test_close_rejects_exchange_and_tracked_size_change(self):
        await self.close_setup()
        plan = await self.plan("close")
        self.positions[0].size += .01
        with self.assertRaises(TradeConflict):
            await self.engine.start_trade_task(self.request(plan))
        self.positions[0].size -= .01
        plan = await self.plan("close")
        key = next(iter(self.engine.active_strategy_sizes))
        self.engine.active_strategy_sizes[key] -= .01
        with self.assertRaises(TradeConflict):
            await self.engine.start_trade_task(self.request(plan))

    async def test_close_respects_net_cost_and_reduce_only(self):
        await self.close_setup()
        plan = await self.plan("close")
        response = await self.engine.start_trade_task(self.request(plan, max_net_cost_usd=plan["max_net_cost_usd"] - 1))
        self.assertEqual((await self.wait_task(response))["execution_status"], "failed")
        self.client.place_limit_order.assert_not_awaited()
        plan = await self.plan("close")
        request = self.request(plan).model_copy(update={"request_id": "close-retry"})
        response = await self.engine.start_trade_task(request)
        self.assertEqual((await self.wait_task(response))["execution_status"], "completed")
        self.assertTrue(all(call.args[-1] is True for call in self.client.place_limit_order.await_args_list))
        self.assertEqual(self.engine.execution_groups["opening"]["status"], "closed")
        self.assertFalse(self.engine.active_strategy_sizes)

    async def test_close_plan_accepts_unequal_quantities_and_ignores_open_spread_limit(self):
        await self.close_setup()
        self.positions[0].size = .01
        self.engine.active_strategy_sizes[f"{self.positions[0].symbol}|{self.positions[0].side}"] = .01
        self.settings.max_spread_bps = .01
        plan = await self.plan("close")
        self.assertEqual(sorted(leg["qty"] for leg in plan["legs"]), [.01, .03, .03, .03])

    async def test_legacy_close_mode_does_not_use_new_configured_mode(self):
        await self.close_setup()
        self.engine.execution_groups["opening"].pop("strategy_mode")
        self.settings.strategy_mode = "short_strangle"
        self.assertEqual((await self.plan("close"))["strategy_mode"], "iron_condor")

    async def test_close_empty_position_is_conflict(self):
        await self.close_setup()
        plan = await self.plan("close")
        self.positions.clear()
        with self.assertRaises(TradeConflict):
            await self.engine.start_trade_task(self.request(plan))

    async def test_fallback_only_submits_confirmed_remainder_and_reserves_original_qty(self):
        self.settings.allow_market_fallback = True
        plan = await self.plan()
        self.client.order.side_effect = lambda symbol, link: {**self.filled_order(symbol, link),
            "cumExecQty": str(self.engine.order_journal[link]["qty"]) if link.startswith("ic-mkt-") else ".01",
            "orderStatus": "Filled" if link.startswith("ic-mkt-") else "Cancelled"}
        response = await self.engine.start_trade_task(self.request(plan, min_net_income_usd=-100))
        group = await self.wait_task(response)
        self.assertEqual(group["execution_status"], "completed")
        self.assertEqual(self.client.place_ioc_order.await_count, 4)
        self.assertTrue(all(call.args[2] == .02 for call in self.client.place_ioc_order.await_args_list))
        self.assertTrue(all(leg["qty"] == .03 for leg in group["net_price_legs"].values()))
        self.assertAlmostEqual(sum(self.engine.active_strategy_sizes.values()), .12)

    async def test_stop_wakes_fallback_delay_without_sending_ioc(self):
        self.settings.allow_market_fallback = True
        self.settings.failed_leg_retry_delay_seconds = 60
        plan = await self.plan()
        self.client.order.side_effect = lambda symbol, link: {**self.filled_order(symbol, link), "cumExecQty": ".01", "orderStatus": "Cancelled"}
        entered = asyncio.Event()
        original_delay = self.engine._trade_delay
        async def delay(*args):
            entered.set()
            await original_delay(*args)
        self.engine._trade_delay = delay
        response = await self.engine.start_trade_task(self.request(plan))
        await entered.wait()
        self.engine.stop_trade_task(response["execution_id"])
        self.assertEqual((await self.wait_task(response))["execution_status"], "partial")
        self.client.place_ioc_order.assert_not_awaited()

    async def test_shutdown_cooperatively_stops_without_cancelling_worker(self):
        plan = await self.plan()
        response = await self.engine.start_trade_task(self.request(plan))
        worker = self.engine.trade_workers[response["execution_id"]]
        await self.engine.shutdown_trade_tasks()
        self.assertFalse(worker.cancelled())
        self.assertEqual(self.engine.execution_groups[response["execution_id"]]["execution_status"], "stopped")

    async def test_storage_failure_does_not_prevent_stop_or_shutdown_reconciliation(self):
        plan = await self.plan()
        entered, release = asyncio.Event(), asyncio.Event()
        async def place(*args):
            entered.set()
            await release.wait()
            return {"orderId": "placed"}
        self.client.place_limit_order.side_effect = place
        self.client.order.side_effect = lambda symbol, link: {**self.filled_order(symbol, link), "cumExecQty": "0", "orderStatus": "Cancelled"}
        response = await self.engine.start_trade_task(self.request(plan))
        await entered.wait()
        self.engine.state_error = "Storage unavailable"
        result = self.engine.stop_trade_task(response["execution_id"])
        self.assertEqual(result["execution_status"], "stopping")
        self.assertTrue(self.engine.trade_stop_events[response["execution_id"]].is_set())
        release.set()
        await self.engine.shutdown_trade_tasks()
        self.assertEqual(self.client.cancel_order.await_count, 4)
        self.assertFalse(self.engine.trade_workers)
        self.assertEqual(self.engine.execution_groups[response["execution_id"]]["execution_status"], "recovery_needed")

    async def test_storage_failure_retries_unknown_cancellation_after_network_recovers(self):
        plan = await self.plan()
        entered, release = asyncio.Event(), asyncio.Event()
        async def place(*args):
            entered.set()
            await release.wait()
            return {"orderId": "placed"}
        self.client.place_limit_order.side_effect = place
        self.client.cancel_order.side_effect = TimeoutError("exchange unavailable")
        self.client.order.side_effect = None
        self.client.order.return_value = None
        self.engine._sync_positions = AsyncMock()
        self.engine._refresh_rfq = AsyncMock()
        response = await self.engine.start_trade_task(self.request(plan))
        await entered.wait()
        with patch("app.engine.Path.replace", side_effect=OSError("disk unavailable")):
            self.engine.stop_trade_task(response["execution_id"])
            storage_error = self.engine.state_error
            self.assertIsNotNone(storage_error)
            release.set()
            group = await self.wait_task(response)
            self.assertEqual(group["execution_status"], "recovery_needed")
            self.assertEqual(self.client.cancel_order.await_count, 4)
            sent = self.client.place_limit_order.await_count

            # A cancellation acknowledgement alone is still not a terminal order.
            self.client.cancel_order.side_effect = None
            await self.engine.reconcile_once()
            self.assertEqual(group["execution_status"], "recovery_needed")
            self.assertTrue(all(not entry["terminal"] for entry in self.engine.order_journal.values()))

            self.client.order.side_effect = lambda symbol, link: {
                **self.filled_order(symbol, link), "cumExecQty": ".01", "orderStatus": "Cancelled"}
            await self.engine.reconcile_once()
            self.assertEqual(self.client.cancel_order.await_count, 12)
            self.assertTrue(all(entry["terminal"] for entry in self.engine.order_journal.values()))
            self.assertEqual(group["execution_status"], "partial")
            self.assertAlmostEqual(sum(self.engine.active_strategy_sizes.values()), .04)
            self.assertEqual(self.engine.state_error, storage_error)
            self.assertEqual(self.engine.reconciliation_error, storage_error)
            self.assertIsNone(self.engine.reconciliation_last_success)
            with self.assertRaisesRegex(ValueError, "State file could not be saved"):
                await self.plan()
            with self.assertRaisesRegex(ValueError, "State file could not be saved"):
                await self.engine.start_trade_task(self.request(plan).model_copy(update={"plan_id": "new", "request_id": "new"}))
            await self.engine.reconcile_once()
            self.assertEqual(self.client.cancel_order.await_count, 12)

        self.assertEqual(self.client.place_limit_order.await_count, sent)
        self.client.amend_order.assert_not_awaited()
        self.client.place_ioc_order.assert_not_awaited()
        self.engine._sync_positions.assert_not_awaited()
        self.engine._refresh_rfq.assert_not_awaited()

    async def test_simulated_progress_has_no_real_journal(self):
        self.settings.trading_mode = "dry-run"
        plan = await self.plan()
        response = await self.engine.start_trade_task(self.request(plan))
        group = await self.wait_task(response)
        self.assertFalse(group["live"])
        self.assertEqual(group["execution_status"], "completed")
        self.assertEqual(group["simulated_fills"], {leg["symbol"]: leg["qty"] for leg in plan["legs"]})
        self.assertFalse(self.engine.order_journal)

    async def test_invalid_finite_inputs_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValidationError):
                TradeTaskRequest(plan_id="plan", request_id="id", min_net_income_usd=value)
            with self.assertRaises(ValidationError):
                TradePlanRequest(operation="open", quantity=value)

    async def test_ioc_stop_during_quote_never_submits(self):
        event = asyncio.Event()
        instrument = self.engine.chain[0]
        async def quote(**kwargs):
            event.set()
            return [{"ask1Price": "100"}]
        self.client.tickers.side_effect = quote
        executor = OrderExecutor(self.client, self.settings, self.engine.log)
        result = await executor.execute(instrument, "Buy", .03, "ioc-stop", Mock(), market=True, stop_event=event)
        self.assertEqual(result["status"], "not_submitted")
        self.client.place_ioc_order.assert_not_awaited()

    async def test_stop_after_order_read_prevents_amendment(self):
        event = asyncio.Event()
        instrument = self.engine.chain[0]
        calls = 0
        async def order(*args):
            nonlocal calls
            calls += 1
            event.set()
            return {"side": "Buy", "qty": ".03", "cumExecQty": "0", "orderStatus": "New" if calls == 1 else "Cancelled"}
        self.client.order.side_effect = order
        executor = OrderExecutor(self.client, self.settings, self.engine.log)
        result = await executor.execute(instrument, "Buy", .03, "amend-stop", Mock(), stop_event=event)
        self.assertTrue(result["terminal"])
        self.client.amend_order.assert_not_awaited()
        self.client.tickers.assert_awaited_once()
        self.client.cancel_order.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
