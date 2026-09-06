import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from app.bybit import BybitClient
from app.config import Settings
from app.engine import TradingEngine
from app.models import CloseRequest, OpenRequest
from app.orders import OrderExecutor
from app.strategy import build_iron_condor, demo_chain


class OrderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(_env_file=None, state_file=f"{directory.name}/state.json", live_trading=True,
                                 bybit_testnet=False, bybit_api_key="test", bybit_api_secret="test")
        self.settings.bbo_order_timeout_seconds = 0.01
        self.settings.bbo_poll_seconds = 0.001
        self.settings.failed_leg_position_checks = 2
        self.settings.failed_leg_position_check_interval_seconds = 0.001
        self.settings.failed_leg_retry_delay_seconds = 0
        self.engine = TradingEngine(self.settings)
        now = datetime.now(timezone.utc)
        self.engine.chain = demo_chain(now)
        self.engine.chain_source = "bybit"
        self.engine.chain_updated_at = now
        self.preview = build_iron_condor(self.engine.chain, now, qty=0.03)
        self.leg = self.preview.legs[0]
        self.engine.client = SimpleNamespace(
            tickers=AsyncMock(return_value=[{"bid1Price": "10", "ask1Price": "12"}]),
            order=AsyncMock(return_value=None), cancel_order=AsyncMock(return_value={}),
            place_limit_order=AsyncMock(return_value={"orderId": "order-1"}),
            place_market_order=AsyncMock(return_value={"orderId": "market-1"}),
            amend_order=AsyncMock(return_value={}), positions=AsyncMock(side_effect=AssertionError("Account positions must not determine fallback")),
        )
        self.client = self.engine.client
        self.engine._capture_pm_baseline = AsyncMock()
        self.engine._validate_open_calendar = Mock()
        self.engine.make_preview = AsyncMock(return_value=self.preview)
        self.engine.load_recent_executions = AsyncMock(return_value=[])

    def order(self, status="Cancelled", filled="0.01", qty="0.03", side=None):
        return {"orderId": "order-1", "orderStatus": status, "cumExecQty": filled,
                "qty": qty, "side": side or self.leg.side}

    async def run_leg(self):
        return await self.engine.follow_bbo_order(self.leg, 0.03, "ic-order")

    async def test_lost_placement_response_is_cancelled_and_reconciled(self):
        self.client.place_limit_order.side_effect = httpx.ReadTimeout("response lost")
        self.client.order.return_value = self.order()
        result = await self.run_leg()
        self.client.place_limit_order.assert_awaited_once()
        self.client.cancel_order.assert_awaited_once_with(self.leg.symbol, "ic-order")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["filledQty"], 0.01)
        self.assertTrue(result["terminal"])

    async def test_amend_failure_always_runs_cancellation(self):
        self.client.tickers.side_effect = [[{"ask1Price": "12"}], [{"ask1Price": "13"}]]
        self.client.order.side_effect = [self.order("New", "0"), self.order()]
        self.client.amend_order.side_effect = httpx.ReadTimeout("amend lost")
        result = await self.run_leg()
        self.assertEqual(result["status"], "partial")
        self.client.cancel_order.assert_awaited_once()

    async def test_reconciliation_failure_never_means_cancelled(self):
        self.client.place_limit_order.side_effect = httpx.ReadTimeout("response lost")
        self.client.order.side_effect = httpx.ReadTimeout("status unavailable")
        result = await self.run_leg()
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["terminal"])
        restored = TradingEngine(self.settings)
        self.assertFalse(restored.order_journal["ic-order"]["terminal"])

    async def test_cancel_acknowledgement_without_terminal_state_is_unknown(self):
        self.client.place_limit_order.side_effect = httpx.ReadTimeout("response lost")
        self.client.order.return_value = self.order("New", "0")
        result = await self.run_leg()
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["terminal"])

    async def test_fill_racing_cancellation_is_counted(self):
        self.client.place_limit_order.side_effect = httpx.ReadTimeout("response lost")
        self.client.cancel_order.side_effect = RuntimeError("already filled")
        self.client.order.return_value = self.order("Filled", "0.03")
        result = await self.run_leg()
        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["filledQty"], 0.03)

    async def test_timeout_cancels_and_waits_for_terminal_state(self):
        self.client.order.return_value = self.order("New", "0")
        async def cancel(*args):
            self.client.order.return_value = self.order("Cancelled", "0")
            return {}
        self.client.cancel_order.side_effect = cancel
        result = await self.run_leg()
        self.assertEqual(result["status"], "timeout_cancelled")
        self.assertTrue(result["terminal"])
        self.client.cancel_order.assert_awaited_once()

    async def test_task_cancellation_persists_cleanup_before_propagating(self):
        submitted = asyncio.Event()
        async def place(*args):
            submitted.set()
            await asyncio.Future()
        self.client.place_limit_order.side_effect = place
        self.client.order.return_value = self.order()
        task = asyncio.create_task(self.run_leg())
        await submitted.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.client.cancel_order.assert_awaited_once()
        self.assertEqual(self.engine.order_journal["ic-order"]["filledQty"], 0.01)

    async def test_invalid_order_quantities_block_terminal_confirmation(self):
        self.client.place_limit_order.side_effect = httpx.ReadTimeout("response lost")
        self.client.order.return_value = self.order("Filled", "NaN")
        result = await self.run_leg()
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["terminal"])

    async def test_no_quote_does_not_submit_or_cancel(self):
        self.client.tickers.return_value = []
        result = await self.run_leg()
        self.assertEqual(result["status"], "not_submitted")
        self.client.place_limit_order.assert_not_awaited()
        self.client.cancel_order.assert_not_awaited()

    async def test_filled_order_does_not_depend_on_next_quote(self):
        self.client.tickers.side_effect = [[{"ask1Price": "12"}], AssertionError("No quote is needed after fill")]
        self.client.order.return_value = self.order("Filled", "0.03")
        result = await self.run_leg()
        self.assertEqual(result["status"], "filled")
        self.client.tickers.assert_awaited_once()

    async def test_unknown_order_blocks_new_open_after_restart(self):
        self.client.place_limit_order.side_effect = httpx.ReadTimeout("response lost")
        await self.run_leg()
        restored = TradingEngine(self.settings)
        restored.client = self.client
        restored.make_preview = self.engine.make_preview
        with self.assertRaisesRegex(ValueError, "Unresolved orders"):
            await restored.open_position(OpenRequest(confirm_live=True, quantity=0.03))
        self.client.place_limit_order.assert_awaited_once()

    async def test_later_reconciliation_restores_only_own_confirmed_quantity(self):
        self.engine.execution_groups["opening"] = {"type": "open", "order_tracking": True}
        self.engine.execution_group_links["ic-order"] = "opening"
        self.client.place_limit_order.side_effect = httpx.ReadTimeout("response lost")
        await self.run_leg()
        restored = TradingEngine(self.settings)
        restored.client = self.client
        self.client.order.return_value = self.order("Cancelled", "0.01")
        await restored._reconcile_pending_orders()
        await restored._reconcile_pending_orders()
        self.assertEqual(restored.active_strategy_sizes, {f"{self.leg.symbol}|{self.leg.side}": 0.01})
        self.assertEqual(restored.active_strategy_group_id, "opening")
        self.assertTrue(restored.order_journal["ic-order"]["terminal"])

    async def test_partial_limit_fills_only_fallback_the_remaining_quantity(self):
        self.settings.allow_market_fallback = True
        orders = {}
        async def limit(symbol, side, qty, price, link, reduce_only):
            orders[link] = self.order("Cancelled", "0.01", str(qty), side)
            return {"orderId": link}
        async def market(symbol, side, qty, link, reduce_only):
            orders[link] = self.order("Filled", str(qty), str(qty), side)
            return {"orderId": link}
        self.client.place_limit_order.side_effect = limit
        self.client.place_market_order.side_effect = market
        self.client.order.side_effect = lambda symbol, link: orders.get(link)
        results = await self.engine.open_position(OpenRequest(confirm_live=True, quantity=0.03))
        self.assertEqual(self.client.place_market_order.await_count, 4)
        self.assertTrue(all(call.args[2] == 0.02 for call in self.client.place_market_order.await_args_list))
        self.assertTrue(all(result.status == "filled" and result.qty == 0.03 for result in results))
        self.assertTrue(all(abs(qty - 0.03) < 1e-9 for qty in self.engine.active_strategy_sizes.values()))
        self.assertEqual(len(self.engine.active_strategy_sizes), 4)
        self.assertTrue(all(len(result.related_order_link_ids) == 2 for result in results))
        self.client.positions.assert_not_awaited()

    async def test_unknown_legs_never_trigger_market_fallback(self):
        self.settings.allow_market_fallback = True
        self.client.place_limit_order.side_effect = httpx.ReadTimeout("response lost")
        results = await self.engine.open_position(OpenRequest(confirm_live=True, quantity=0.03))
        self.assertTrue(all(result.status == "unknown" for result in results))
        self.client.place_market_order.assert_not_awaited()

    async def test_fallback_requires_fresh_original_order_confirmation(self):
        self.settings.allow_market_fallback = True
        journal = {"symbol": self.leg.symbol, "side": self.leg.side, "qty": 0.03, "reduce_only": False,
                   "status": "partial", "terminal": True, "filledQty": 0.01}
        self.engine.order_journal["ic-order"] = journal
        self.client.order.side_effect = httpx.ReadTimeout("no confirmation")
        from app.models import OrderResult
        result = OrderResult(symbol=self.leg.symbol, side=self.leg.side, qty=0.01, status="partial")
        await self.engine._market_fallback([self.leg], 0.03, ["ic-order"], [result], "group", [])
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.qty, 0.01)
        self.client.place_market_order.assert_not_awaited()

    async def test_replacement_response_loss_never_causes_a_second_submission(self):
        self.client.place_market_order.side_effect = httpx.ReadTimeout("market response lost")
        result = await self.engine._execute_order(self.leg, 0.02, "ic-mkt-unknown", market=True)
        self.assertEqual(result["status"], "unknown")
        with self.assertRaisesRegex(ValueError, "already been used"):
            await self.engine._execute_order(self.leg, 0.02, "ic-mkt-unknown", market=True)
        self.client.place_market_order.assert_awaited_once()
        self.client.cancel_order.assert_awaited_once()

    async def test_remainder_below_lot_minimum_is_not_rounded_up(self):
        self.engine.order_journal["ic-order"] = {"symbol": self.leg.symbol, "side": self.leg.side, "qty": 0.03,
            "reduce_only": False, "status": "partial", "terminal": True, "filledQty": 0.025}
        self.client.order.return_value = self.order("Cancelled", "0.025")
        from app.models import OrderResult
        result = OrderResult(symbol=self.leg.symbol, side=self.leg.side, qty=0.025, status="partial")
        await self.engine._market_fallback([self.leg], 0.03, ["ic-order"], [result], "group", [])
        self.assertEqual(result.qty, 0.025)
        self.assertIn("lot limits", result.message)
        self.client.place_market_order.assert_not_awaited()

    async def test_partial_close_updates_all_confirmed_legs_without_double_deduction(self):
        self.engine.active_strategy_group_id = "opening"
        self.engine.execution_groups["opening"] = {"type": "open", "order_tracking": True}
        self.engine.active_strategy_symbols = {leg.symbol for leg in self.preview.legs}
        self.engine.active_strategy_sizes = {f"{leg.symbol}|{leg.side}": 0.03 for leg in self.preview.legs}
        self.client.positions.side_effect = None
        self.client.positions.return_value = [{"symbol": leg.symbol, "side": leg.side, "size": "0.03"} for leg in self.preview.legs]
        orders = {}
        async def limit(symbol, side, qty, price, link, reduce_only):
            filled = "0.01" if symbol == self.leg.symbol else "0.03"
            orders[link] = self.order("Cancelled" if filled == "0.01" else "Filled", filled, str(qty), side)
            return {"orderId": link}
        self.client.place_limit_order.side_effect = limit
        self.client.order.side_effect = lambda symbol, link: orders[link]
        results, _ = await self.engine.close_position(CloseRequest(confirm_live=True))
        self.assertEqual(sum(result.status == "partial" for result in results), 1)
        self.assertEqual(self.engine.active_strategy_symbols, {self.leg.symbol})
        self.assertAlmostEqual(self.engine.active_strategy_sizes[f"{self.leg.symbol}|{self.leg.side}"], 0.02)
        for link, outcome in list(self.engine.order_journal.items()):
            self.engine._record_order(link, outcome.copy())
        self.assertAlmostEqual(self.engine.active_strategy_sizes[f"{self.leg.symbol}|{self.leg.side}"], 0.02)


class BybitOrderLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_miss_uses_history_and_checks_identity(self):
        client = BybitClient()
        record = {"symbol": "BTC-OPTION", "orderLinkId": "link", "orderStatus": "Filled"}
        client._request = AsyncMock(side_effect=[{"list": [{"symbol": "OTHER", "orderLinkId": "link"}]}, {"list": [record]}])
        self.assertEqual(await client.order("BTC-OPTION", "link"), record)
        self.assertEqual(client._request.await_args_list[1].args[1], "/v5/order/history")

    async def test_missing_order_stays_unknown(self):
        client = BybitClient()
        client._request = AsyncMock(return_value={"list": []})
        self.assertIsNone(await client.order("BTC-OPTION", "link"))


class ExecutionHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_merges_history_preserves_close_identity_and_combines_fallback_fills(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = TradingEngine(Settings(_env_file=None, state_file=f"{directory}/state.json", bybit_api_key="test", bybit_api_secret="test"))
            engine.execution_groups = {"closing": {"type": "close", "opening_group": "opening"}}
            engine.execution_group_links = {"ic-close-closing-0": "closing"}
            def fill(exec_id, link, qty, price, fee, timestamp):
                return {"symbol": "BTC-OPTION", "side": "Buy", "orderId": link, "orderLinkId": link,
                        "execId": exec_id, "execQty": str(qty), "execPrice": str(price), "execFee": str(fee),
                        "feeCurrency": "USDT", "execTime": str(timestamp)}
            initial = fill("a", "ic-opening-0", 0.01, 100, 0.1, 1000)
            replacement = fill("b", "ic-mkt-retry", 0.02, 110, 0.2, 2000)
            closing = fill("c", "ic-close-closing-0", 0.03, 120, 0.3, 3000)
            engine.client.executions = AsyncMock(side_effect=[[initial], [replacement], [closing], []])
            await engine.load_recent_executions()
            await engine.load_recent_executions(["ic-mkt-retry"])
            await engine.load_recent_executions(["ic-close-closing-0"])
            await engine.load_recent_executions()
            self.assertEqual({item.exec_id for item in engine.last_executions}, {"a", "b", "c"})
            close_record = next(item for item in engine.last_executions if item.exec_id == "c")
            self.assertTrue(close_record.reduce_only)
            self.assertEqual(close_record.opening_group, "opening")
            self.assertFalse(next(item for item in engine.last_executions if item.exec_id == "a").reduce_only)
            from app.models import OrderResult
            result = OrderResult(symbol="BTC-OPTION", side="Buy", qty=0.03, status="filled", order_link_id="ic-opening-0", related_order_link_ids=["ic-opening-0", "ic-mkt-retry"])
            engine._attach_execution_details([result])
            self.assertEqual(result.exec_qty, 0.03)
            self.assertEqual(result.exec_fee, 0.3)
            self.assertAlmostEqual(result.exec_price, 106.66666667)
            self.assertEqual(result.execution_id, "b")


if __name__ == "__main__":
    unittest.main()
