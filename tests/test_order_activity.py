import asyncio
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.config import Settings
from app.engine import TradingEngine
from app.main import app
from app.models import OrderResult
from app.order_activity import order_dashboard
from app.orders import OrderExecutor
from app.strategy import build_iron_condor, demo_chain


ITEM_KEYS = {"order_link_id", "order_id", "symbol", "side", "qty", "filled_qty", "remaining_qty", "terminal",
             "status", "phase", "exchange_status", "execution_type", "operation", "requested_price", "confirmed_price",
             "created_at", "updated_at", "last_confirmed_at", "stale"}


class OrderActivityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(_env_file=None, state_file=f"{directory.name}/state.json", live_trading=True,
                                 bybit_testnet=False, bybit_api_key="test", bybit_api_secret="test",
                                 dashboard_password="test-dashboard-password")
        self.settings.bbo_order_timeout_seconds = 2
        self.settings.bbo_poll_seconds = 0.001
        self.settings.failed_leg_position_checks = 2
        self.settings.failed_leg_position_check_interval_seconds = 0.001
        self.settings.failed_leg_retry_delay_seconds = 0
        self.engine = TradingEngine(self.settings)
        now = datetime.now(timezone.utc)
        self.engine.chain = demo_chain(now)
        self.leg = build_iron_condor(self.engine.chain, now, qty=0.03).legs[0]
        self.client = SimpleNamespace(
            tickers=AsyncMock(return_value=[{"bid1Price": "10", "ask1Price": "12"}]),
            order=AsyncMock(), cancel_order=AsyncMock(return_value={}),
            place_limit_order=AsyncMock(return_value={"orderId": "order-1"}),
            place_ioc_order=AsyncMock(return_value={"orderId": "ioc-1"}),
            amend_order=AsyncMock(return_value={}),
        )
        self.engine.client = self.client

    def exchange_order(self, status="Filled", filled="0.03", qty="0.03", price="12"):
        return {"orderId": "order-1", "orderStatus": status, "cumExecQty": filled,
                "qty": qty, "side": self.leg.side, "price": price}

    def pending(self, link="ic-order", **extra):
        self.engine.order_journal[link] = {"symbol": self.leg.symbol, "side": self.leg.side, "qty": 0.03,
                                          "reduce_only": False, "status": "unknown", "terminal": False, "filledQty": 0.0, **extra}

    def item(self, link="ic-order"):
        return next(item for item in self.engine.order_snapshot()["items"] if item["order_link_id"] == link)

    async def start(self, coro):
        task = asyncio.create_task(coro)
        async def cleanup():
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.addAsyncCleanup(cleanup)
        return task

    async def wait(self, event):
        await asyncio.wait_for(event.wait(), timeout=1)

    async def test_submission_ack_is_not_a_confirmed_price_and_heartbeats_do_not_write_state(self):
        submitting, allow_submit, querying, allow_query, working, allow_finish = [asyncio.Event() for _ in range(6)]
        async def place(*args):
            submitting.set()
            await allow_submit.wait()
            return {"orderId": "order-1", "price": "999"}
        order_reads = 0
        async def order(*args):
            nonlocal order_reads
            order_reads += 1
            if order_reads == 1:
                querying.set()
                await allow_query.wait()
            return self.exchange_order("New", "0", price="11.5") if order_reads <= 3 else self.exchange_order()
        ticker_reads = 0
        async def tickers(**kwargs):
            nonlocal ticker_reads
            ticker_reads += 1
            if ticker_reads == 4:
                working.set()
                await allow_finish.wait()
            return [{"ask1Price": "12"}]
        self.client.place_limit_order.side_effect = place
        self.client.order.side_effect = order
        self.client.tickers.side_effect = tickers
        with patch.object(self.engine, "_save_state", wraps=self.engine._save_state) as save:
            task = await self.start(self.engine.follow_bbo_order(self.leg, 0.03, "ic-order"))
            await self.wait(submitting)
            self.assertEqual(self.item()["phase"], "submitting")
            self.assertEqual(self.item()["requested_price"], 12)
            self.assertIsNone(self.item()["confirmed_price"])
            self.assertIsNone(self.item()["last_confirmed_at"])
            self.assertEqual(save.call_count, 1)
            allow_submit.set()
            await self.wait(querying)
            self.assertEqual(self.item()["phase"], "reconciling")
            self.assertEqual(self.item()["order_id"], "order-1")
            self.assertIsNone(self.item()["confirmed_price"])
            self.assertIsNone(self.item()["last_confirmed_at"])
            allow_query.set()
            await self.wait(working)
            self.assertEqual(self.item()["phase"], "working")
            self.assertEqual(self.item()["confirmed_price"], 11.5)
            self.assertIsNotNone(self.item()["last_confirmed_at"])
            self.assertEqual(save.call_count, 2)  # New identity, then unchanged observations stay in memory.
            allow_finish.set()
            outcome = await asyncio.wait_for(task, 1)
            self.assertEqual(outcome["status"], "filled")
            self.assertEqual(save.call_count, 3)
        self.client.place_limit_order.assert_awaited_once()
        self.client.cancel_order.assert_not_awaited()

    async def test_amendment_exposes_requested_price_without_confirming_ack(self):
        amending, allow_amend, querying, allow_query = [asyncio.Event() for _ in range(4)]
        async def amend(*args):
            amending.set()
            await allow_amend.wait()
            return {"price": "999"}
        reads = 0
        async def order(*args):
            nonlocal reads
            reads += 1
            if reads == 1:
                return self.exchange_order("PartiallyFilled", "0.01", price="12")
            querying.set()
            await allow_query.wait()
            return self.exchange_order(price="13")
        self.client.order.side_effect = order
        self.client.amend_order.side_effect = amend
        self.client.tickers.side_effect = [[{"ask1Price": "12"}], [{"ask1Price": "13"}]]
        task = await self.start(self.engine.follow_bbo_order(self.leg, 0.03, "ic-order"))
        await self.wait(amending)
        item = self.item()
        self.assertEqual((item["phase"], item["requested_price"], item["confirmed_price"]), ("amending", 13, 12))
        self.assertEqual((item["filled_qty"], item["remaining_qty"]), (0.01, 0.02))
        confirmed_at = item["last_confirmed_at"]
        allow_amend.set()
        await self.wait(querying)
        self.assertEqual(self.item()["phase"], "reconciling")
        self.assertEqual(self.item()["confirmed_price"], 12)
        self.assertEqual(self.item()["last_confirmed_at"], confirmed_at)
        allow_query.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(self.item()["phase"], "terminal")
        self.assertEqual(self.item()["confirmed_price"], 13)
        self.assertEqual(self.item()["remaining_qty"], 0)
        self.client.place_limit_order.assert_awaited_once()
        self.client.amend_order.assert_awaited_once()
        self.client.cancel_order.assert_not_awaited()

    async def test_cancel_ack_remains_reconciling_until_rejection_is_confirmed(self):
        cancelling, allow_cancel, querying, allow_query = [asyncio.Event() for _ in range(4)]
        async def cancel(*args):
            cancelling.set()
            await allow_cancel.wait()
            return {}
        async def order(*args):
            querying.set()
            await allow_query.wait()
            return self.exchange_order("Rejected", "0")
        self.client.place_limit_order.side_effect = httpx.ReadTimeout("lost response")
        self.client.cancel_order.side_effect = cancel
        self.client.order.side_effect = order
        task = await self.start(self.engine.follow_bbo_order(self.leg, 0.03, "ic-order"))
        await self.wait(cancelling)
        self.assertEqual(self.item()["phase"], "cancelling")
        self.assertIsNone(self.item()["last_confirmed_at"])
        allow_cancel.set()
        await self.wait(querying)
        self.assertEqual(self.item()["phase"], "reconciling")
        self.assertFalse(self.item()["terminal"])
        allow_query.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual((self.item()["phase"], self.item()["exchange_status"]), ("terminal", "Rejected"))
        self.assertEqual(self.item()["filled_qty"], 0)
        self.assertFalse(self.engine.order_snapshot()["execution_active"])
        self.client.place_limit_order.assert_awaited_once()
        self.client.cancel_order.assert_awaited_once()

    async def test_reconciliation_persists_partial_fills_before_next_query_finishes(self):
        querying, allow_query = asyncio.Event(), asyncio.Event()
        self.pending()
        reads = 0
        async def order(*args):
            nonlocal reads
            reads += 1
            if reads == 1:
                return self.exchange_order("PartiallyFilled", "0.01")
            querying.set()
            await allow_query.wait()
            return self.exchange_order("PartiallyFilledCanceled", "0.02")
        self.client.order.side_effect = order
        task = await self.start(self.engine._reconcile_pending_orders())
        await self.wait(querying)
        self.assertEqual(self.item()["filled_qty"], 0.01)
        restored = TradingEngine(self.settings)
        self.assertEqual(restored.order_journal["ic-order"]["filledQty"], 0.01)
        self.assertIsNone(restored.order_snapshot()["items"][0]["last_confirmed_at"])
        allow_query.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(self.item()["filled_qty"], 0.02)
        self.assertEqual(self.item()["phase"], "terminal")
        self.client.cancel_order.assert_awaited_once()
        self.assertEqual(self.client.order.await_count, 2)

    async def test_missing_failed_and_regressing_observations_do_not_refresh_confirmation(self):
        self.settings.failed_leg_position_checks = 1
        self.pending(filledQty=0.01)
        self.engine._observe_order("ic-order", {"phase": "working", "confirmed": True, "exchange_status": "PartiallyFilled", "confirmed_price": 12})
        confirmed_at = self.item()["last_confirmed_at"]
        executor = OrderExecutor(self.client, self.settings, self.engine.log)
        for response in (None, httpx.ReadTimeout("unavailable"), self.exchange_order("New", "0")):
            self.client.order.side_effect = response if isinstance(response, Exception) else None
            self.client.order.return_value = response
            result = await executor.reconcile(self.leg.symbol, self.leg.side, 0.03, "ic-order", self.engine.order_journal["ic-order"],
                                             cancel=False, observer=lambda event: self.engine._observe_order("ic-order", event),
                                             record=lambda outcome: self.engine._record_order("ic-order", outcome))
            self.assertFalse(result["terminal"])
            self.assertEqual(self.item()["filled_qty"], 0.01)
            self.assertEqual(self.item()["last_confirmed_at"], confirmed_at)
            self.assertEqual(self.item()["phase"], "unknown")
        self.client.cancel_order.assert_not_awaited()

    async def test_restored_reconciliation_ignores_old_update_time_on_unchanged_observations(self):
        self.settings.failed_leg_position_checks = 3
        self.pending(updated_at="2020-01-01T00:00:00+00:00")
        querying, allow_query = asyncio.Event(), asyncio.Event()
        reads = 0
        async def order(*args):
            nonlocal reads
            reads += 1
            if reads < 3:
                return self.exchange_order("PartiallyFilled", "0.01")
            querying.set()
            await allow_query.wait()
            return self.exchange_order("PartiallyFilledCanceled", "0.01")
        self.client.order.side_effect = order
        with patch.object(self.engine, "_save_state", wraps=self.engine._save_state) as save:
            task = await self.start(self.engine._reconcile_pending_orders())
            await self.wait(querying)
            self.assertEqual(save.call_count, 1)
            self.assertEqual(self.item()["filled_qty"], 0.01)
            allow_query.set()
            await asyncio.wait_for(task, 1)
            self.assertEqual(save.call_count, 2)
        self.assertEqual(self.item()["phase"], "terminal")

    async def test_observer_failure_and_invalid_display_price_cannot_change_order_execution(self):
        self.client.order.return_value = self.exchange_order(price={"secret": "not-a-price"})
        with patch.object(self.engine, "_observe_order", side_effect=RuntimeError("display failed")):
            result = await self.engine.follow_bbo_order(self.leg, 0.03, "ic-order")
        self.assertEqual(result["status"], "filled")
        self.client.cancel_order.assert_not_awaited()
        self.client.place_limit_order.assert_awaited_once()
        result = await self.engine.follow_bbo_order(self.leg, 0.03, "ic-order-2")
        self.assertEqual(result["status"], "filled")
        self.assertIsNone(self.item("ic-order-2")["confirmed_price"])
        self.assertIsNotNone(self.item("ic-order-2")["last_confirmed_at"])
        self.client.cancel_order.assert_not_awaited()

    async def test_ioc_remainder_is_displayed_as_a_separate_order(self):
        submitting, allow_submit = asyncio.Event(), asyncio.Event()
        self.pending(status="partial", terminal=True, filledQty=0.01, execution_type="BBO")
        async def place(symbol, side, qty, price, link, reduce_only):
            submitting.set()
            await allow_submit.wait()
            return {"orderId": "ioc-1"}
        async def order(symbol, link):
            return self.exchange_order("Cancelled", "0.01") if link == "ic-order" else self.exchange_order("Filled", "0.02", "0.02", "10")
        self.client.place_ioc_order.side_effect = place
        self.client.order.side_effect = order
        result = OrderResult(symbol=self.leg.symbol, side=self.leg.side, qty=0.01, status="partial")
        task = await self.start(self.engine._market_fallback([self.leg], 0.03, ["ic-order"], [result], "group", []))
        await self.wait(submitting)
        replacement = next(item for item in self.engine.order_snapshot()["items"] if item["execution_type"] == "IOC")
        self.assertEqual((replacement["phase"], replacement["qty"], replacement["remaining_qty"]), ("submitting", 0.02, 0.02))
        self.assertEqual(replacement["requested_price"], 10)
        self.assertEqual(self.item()["filled_qty"], 0.01)
        self.assertTrue(self.item()["terminal"])
        allow_submit.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual((result.status, result.qty), ("filled", 0.03))
        self.client.place_ioc_order.assert_awaited_once()
        self.assertEqual(self.client.place_ioc_order.await_args.args[2], 0.02)
        self.assertTrue(all(item["terminal"] for item in self.engine.order_snapshot()["items"]))

    async def test_api_is_authenticated_whitelisted_and_nonblocking_inside_transaction(self):
        self.pending(message="private-error-secret", raw={"api_secret": "private-error-secret"}, execution_type="BBO")
        self.engine.execution_groups["closing"] = {"type": "close"}
        self.engine.execution_group_links["ic-order"] = "closing"
        self.engine._observe_order("ic-order", {"phase": "submitting", "requested_price": 12})
        with patch("app.main.engine", self.engine), patch("app.main.settings", self.settings), \
                patch.object(self.engine, "_save_state", side_effect=AssertionError("Dashboard cannot write state")):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                self.assertEqual((await client.get("/api/dashboard/orders")).status_code, 401)
                auth = httpx.BasicAuth("admin", "test-dashboard-password")
                self.assertEqual((await client.get("/api/dashboard/orders", auth=auth, headers={"Origin": "https://other.invalid"})).status_code, 403)
                async with self.engine.lock:
                    response = await asyncio.wait_for(client.get("/api/dashboard/orders", auth=auth), timeout=0.5)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), {"generated_at", "execution_active", "active_count", "terminal_count", "items", "groups", "active_execution_id"})
        self.assertEqual(payload["groups"][0]["execution_id"], "closing")
        self.assertEqual(set(payload["items"][0]), ITEM_KEYS)
        self.assertEqual(payload["items"][0]["operation"], "close")
        self.assertIsNone(payload["items"][0]["last_confirmed_at"])
        self.assertNotIn("private-error-secret", response.text)
        for method in vars(self.client).values():
            method.assert_not_awaited()

    async def test_restart_never_revives_saved_display_actions_or_fabricates_times(self):
        self.pending(phase="amending", requested_price=999, confirmed_price=999, last_confirmed_at="2020-01-01T00:00:00+00:00")
        self.engine._save_state()
        restored = TradingEngine(self.settings)
        item = restored.order_snapshot()["items"][0]
        self.assertEqual(item["phase"], "unknown")
        self.assertTrue(item["stale"])
        self.assertFalse(restored.order_snapshot()["execution_active"])
        for field in ("created_at", "updated_at", "last_confirmed_at", "requested_price", "confirmed_price", "execution_type", "exchange_status"):
            self.assertIsNone(item[field])


class OrderDashboardTests(unittest.TestCase):
    def entry(self, terminal=False, **extra):
        return {"symbol": "BTC-option", "side": "Sell", "qty": 0.03, "filledQty": 0.01,
                "terminal": terminal, "status": "partial" if terminal else "unknown", "reduce_only": False, **extra}

    def test_returns_every_unresolved_order_and_only_the_latest_thirty_terminal_orders(self):
        now = datetime.now(timezone.utc)
        journal = {f"active-{i}": self.entry() for i in range(35)}
        journal.update({f"done-{i}": self.entry(True, updated_at=(now - timedelta(seconds=i)).isoformat()) for i in range(35)})
        activity = {"done-0": {"phase": "working", "last_confirmed_at": "2000-01-01T00:00:00+00:00"}}
        result = order_dashboard(journal, activity, stale_seconds=30, now=now)
        self.assertEqual((result["active_count"], result["terminal_count"], len(result["items"])), (35, 30, 65))
        self.assertEqual({item["order_link_id"] for item in result["items"] if not item["terminal"]}, {f"active-{i}" for i in range(35)})
        self.assertEqual([item["order_link_id"] for item in result["items"] if item["terminal"]], [f"done-{i}" for i in range(30)])
        self.assertTrue(all(item["phase"] == "terminal" and not item["stale"] for item in result["items"] if item["terminal"]))

    def test_staleness_uses_exchange_confirmation_not_dashboard_or_action_heartbeat(self):
        now = datetime.now(timezone.utc)
        journal = {"old": self.entry(), "recent": self.entry(), "missing": self.entry()}
        activity = {
            "old": {"phase": "amending", "updated_at": now.isoformat(), "last_confirmed_at": (now - timedelta(seconds=31)).isoformat()},
            "recent": {"phase": "reconciling", "last_confirmed_at": (now - timedelta(seconds=16)).isoformat()},
        }
        result = order_dashboard(journal, activity, stale_seconds=30, now=now)
        by_id = {item["order_link_id"]: item for item in result["items"]}
        self.assertTrue(by_id["old"]["stale"])
        self.assertFalse(by_id["recent"]["stale"])
        self.assertTrue(by_id["missing"]["stale"])
        self.assertIsNone(by_id["missing"]["last_confirmed_at"])


if __name__ == "__main__":
    unittest.main()
