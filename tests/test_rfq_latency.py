import asyncio
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx

from app.config import Settings
from app.engine import TradingEngine
from app.models import RfqCancelRequest, RfqCreateRequest, RfqExecuteRequest
from app.strategy import build_iron_condor, demo_chain


class RfqLatencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(_env_file=None, state_file=str(Path(directory.name) / "state.json"),
                                 trading_mode="testnet", bybit_api_key="test", bybit_api_secret="test")
        self.engine = TradingEngine(self.settings)
        now = datetime.now(timezone.utc)
        self.engine.chain = demo_chain(now)
        self.engine.chain_source, self.engine.chain_updated_at = "bybit", now
        self.preview = build_iron_condor(self.engine.chain, now, qty=.01)
        self.engine.make_preview = AsyncMock(return_value=self.preview)
        self.engine.refresh_chain = AsyncMock(return_value=self.engine.chain)
        self.engine._validate_open_calendar = Mock()
        self.engine._capture_pm_baseline = AsyncMock()
        self.mock_client(self.engine)

    @staticmethod
    def mock_client(engine):
        # Every exchange path must be mocked; these tests never open a transport.
        engine.client._request = AsyncMock(side_effect=AssertionError("Unexpected exchange request"))
        engine.client.rfq_config = AsyncMock(return_value={"counterparties": ["DESK"], "strategyTypes": ["IronCondor"]})
        engine.client.create_rfq = AsyncMock(return_value={"rfqId": "rfq-new", "status": "Active"})
        engine.client.rfq_realtime = AsyncMock(return_value=[{"rfqId": "rfq-1", "status": "Active"}])
        engine.client.rfq_history = AsyncMock(return_value=[])
        engine.client.quote_realtime = AsyncMock(return_value=[])
        engine.client.cancel_rfq = AsyncMock(return_value={"rfqId": "rfq-1"})
        engine.client.execute_quote = AsyncMock(return_value={"rfqId": "rfq-1", "quoteId": "quote-1", "status": "PendingFill"})
        engine.client.positions = AsyncMock(return_value=[])

    def active_rfq(self):
        self.engine.rfq_state = {"rfq_id": "rfq-1", "rfq_link_id": "link1", "status": "Active",
                                 "legs": [{"symbol": leg.symbol, "side": leg.side, "qty": str(leg.qty)} for leg in self.preview.legs],
                                 "quotes": [{"quoteId": "quote-1", "quoteSellList": [
                                     {"symbol": leg.symbol, "qty": str(leg.qty), "price": "100"} for leg in self.preview.legs]}]}

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)

        async def finish():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        self.addAsyncCleanup(finish)
        return task

    async def test_create_fetches_config_and_preview_in_parallel_before_one_post(self):
        config_started, preview_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def config():
            config_started.set()
            await release.wait()
            return {"counterparties": ["A", "B"], "maxLP": "1", "strategyTypes": ["IronCondor"]}

        async def preview(*args, **kwargs):
            preview_started.set()
            await release.wait()
            return self.preview

        self.engine.client.rfq_config.side_effect = config
        self.engine.make_preview.side_effect = preview
        task = self.start(self.engine.create_rfq(RfqCreateRequest(quantity=.01)))
        await asyncio.wait_for(asyncio.gather(config_started.wait(), preview_started.wait()), 2)
        self.engine.client.create_rfq.assert_not_awaited()
        release.set()
        result = await asyncio.wait_for(task, 2)
        self.engine.client.create_rfq.assert_awaited_once()
        args = self.engine.client.create_rfq.await_args.args
        self.assertEqual((args[0], args[3]), (["A"], "IronCondor"))
        self.assertEqual(len(args[1]), 4)
        result["legs"][0]["qty"] = "999"
        self.assertEqual(self.engine.rfq_state["legs"][0]["qty"], "0.01")

    async def test_create_failure_cancels_and_joins_the_other_read(self):
        for failed in ("config", "preview"):
            with self.subTest(failed=failed):
                started, stopped, never = asyncio.Event(), asyncio.Event(), asyncio.Event()

                async def pending(*args, **kwargs):
                    started.set()
                    try:
                        await never.wait()
                    finally:
                        stopped.set()

                async def fail(*args, **kwargs):
                    await started.wait()
                    raise ValueError("read failed")

                self.engine.client.rfq_config.side_effect = fail if failed == "config" else pending
                self.engine.make_preview.side_effect = fail if failed == "preview" else pending
                with self.assertRaisesRegex(ValueError, "read failed"):
                    await asyncio.wait_for(self.engine.create_rfq(RfqCreateRequest(quantity=.01)), 2)
                self.assertTrue(stopped.is_set())
                self.assertFalse(self.engine.lock.locked())
                self.assertFalse(self.engine._trade_admitting)
                self.assertEqual(self.engine.rfq_state, {})
                self.engine.client.create_rfq.assert_not_awaited()

    async def test_canceling_create_request_joins_both_read_workers(self):
        started = [asyncio.Event(), asyncio.Event()]
        stopped = [asyncio.Event(), asyncio.Event()]
        never = asyncio.Event()

        async def pending(index):
            started[index].set()
            try:
                await never.wait()
            finally:
                stopped[index].set()

        async def config():
            return await pending(0)

        async def preview(*args, **kwargs):
            return await pending(1)

        self.engine.client.rfq_config.side_effect = config
        self.engine.make_preview.side_effect = preview
        task = self.start(self.engine.create_rfq(RfqCreateRequest(quantity=.01)))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(all(event.is_set() for event in stopped))
        self.assertFalse(self.engine.lock.locked())
        self.assertFalse(self.engine._trade_admitting)
        self.engine.client.create_rfq.assert_not_awaited()

    async def test_explicit_counterparties_keep_config_failure_fallback(self):
        self.engine.client.rfq_config.side_effect = httpx.ReadTimeout("config unavailable")
        result = await self.engine.create_rfq(RfqCreateRequest(quantity=.01, counterparties=["EXPLICIT"]))
        self.assertEqual(result["counterparties"], ["EXPLICIT"])
        self.engine.client.create_rfq.assert_awaited_once()
        self.assertTrue(any("using specified counterparties" in entry.message for entry in self.engine.logs))

    async def test_create_rechecks_freshness_after_parallel_reads_finish(self):
        async def config():
            self.engine.chain_updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
            return {"counterparties": ["DESK"]}

        self.engine.client.rfq_config.side_effect = config
        with self.assertRaisesRegex(ValueError, "stale"):
            await self.engine.create_rfq(RfqCreateRequest(quantity=.01))
        self.engine.client.create_rfq.assert_not_awaited()
        self.assertEqual(self.engine.rfq_state, {})

    async def test_create_retains_preview_mode_quantity_and_risk_checks(self):
        cases = [("strategy mode", lambda: self.preview.model_copy(update={"strategy_mode": "short_strangle"})),
                 ("quantity", lambda: build_iron_condor(self.engine.chain, datetime.now(timezone.utc), qty=.02)),
                 ("Risk limit", lambda: self.preview.model_copy(update={"max_loss_usd": self.settings.max_risk_usd + 1}))]
        for message, invalid in cases:
            with self.subTest(message=message):
                self.engine.make_preview.return_value = invalid()
                with self.assertRaisesRegex(ValueError, message):
                    await self.engine.create_rfq(RfqCreateRequest(quantity=.01))
                self.engine.client.create_rfq.assert_not_awaited()
                self.assertEqual(self.engine.rfq_state, {})

    async def test_poll_remote_reads_do_not_hold_lock_or_block_cancel(self):
        for stage in ("rfq_realtime", "rfq_history", "quote_realtime"):
            with self.subTest(stage=stage):
                self.mock_client(self.engine)
                self.active_rfq()
                started, release = asyncio.Event(), asyncio.Event()

                async def blocked(*args, **kwargs):
                    started.set()
                    await release.wait()
                    return [] if stage == "quote_realtime" else [{"rfqId": "rfq-1", "status": "Active"}]

                if stage == "rfq_history":
                    self.engine.client.rfq_realtime.return_value = []
                getattr(self.engine.client, stage).side_effect = blocked
                poll = self.start(self.engine.refresh_rfq())
                await asyncio.wait_for(started.wait(), 2)
                self.assertFalse(self.engine.lock.locked())
                result = await asyncio.wait_for(self.engine.cancel_rfq(RfqCancelRequest(rfq_id="rfq-1")), 2)
                self.assertFalse(poll.done())
                self.assertEqual(result["status"], "CancelUnknown")
                release.set()
                self.assertEqual((await asyncio.wait_for(poll, 2))["status"], "CancelUnknown")
                self.assertEqual(self.engine.rfq_state["status"], "CancelUnknown")
                self.engine.client.cancel_rfq.assert_awaited_once()

    async def test_stale_poll_cannot_overwrite_reconciled_terminal_or_new_creation(self):
        self.active_rfq()
        started, release = asyncio.Event(), asyncio.Event()

        async def quotes(*args):
            started.set()
            await release.wait()
            return [{"quoteId": "stale"}]

        self.engine.client.quote_realtime.side_effect = quotes
        poll = self.start(self.engine.refresh_rfq())
        await asyncio.wait_for(started.wait(), 2)
        await self.engine.cancel_rfq(RfqCancelRequest(rfq_id="rfq-1"))
        self.engine.client.rfq_realtime.return_value = [{"rfqId": "rfq-1", "status": "Canceled"}]
        # Internal refresh must not wait for the public poll's in-flight claim.
        async with self.engine.lock:
            terminal = await asyncio.wait_for(self.engine._refresh_rfq(include_quotes=False), 2)
        self.assertEqual(terminal["status"], "Canceled")
        created = await asyncio.wait_for(self.engine.create_rfq(RfqCreateRequest(quantity=.01)), 2)
        self.assertFalse(poll.done())
        self.assertEqual(created["rfq_id"], "rfq-new")
        release.set()
        self.assertEqual(await asyncio.wait_for(poll, 2), created)
        self.assertEqual(self.engine.rfq_state, created)
        self.engine.client.create_rfq.assert_awaited_once()

    async def test_stale_poll_cannot_overwrite_execution_intent(self):
        self.active_rfq()
        started, release = asyncio.Event(), asyncio.Event()

        async def quotes(*args):
            started.set()
            await release.wait()
            return [{"quoteId": "stale"}]

        self.engine.client.quote_realtime.side_effect = quotes
        poll = self.start(self.engine.refresh_rfq())
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(self.engine.execute_rfq(RfqExecuteRequest(
            confirm_live=True, rfq_id="rfq-1", quote_id="quote-1", quote_side="Sell")), 2)
        self.assertFalse(poll.done())
        expected = deepcopy(self.engine.rfq_state)
        release.set()
        self.assertEqual(await asyncio.wait_for(poll, 2), expected)
        self.assertEqual(expected["status"], "PendingFill")
        self.assertEqual(expected["selected_quote_id"], "quote-1")
        self.engine.client.execute_quote.assert_awaited_once()

    async def test_concurrent_polls_return_detached_snapshot_and_share_one_read(self):
        self.active_rfq()
        started, release = asyncio.Event(), asyncio.Event()

        async def read(*args):
            started.set()
            await release.wait()
            return [{"rfqId": "rfq-1", "status": "Active"}]

        self.engine.client.rfq_realtime.side_effect = read
        poll = self.start(self.engine.refresh_rfq())
        await asyncio.wait_for(started.wait(), 2)
        other = await asyncio.wait_for(self.engine.refresh_rfq(), 2)
        other["quotes"][0]["quoteId"] = "edited"
        self.assertEqual(self.engine.rfq_state["quotes"][0]["quoteId"], "quote-1")
        self.engine.client.rfq_realtime.assert_awaited_once()
        release.set()
        await asyncio.wait_for(poll, 2)

    async def test_cancel_returns_ack_without_waiting_for_realtime_or_history(self):
        self.active_rfq()

        async def cancel(rfq_id):
            persisted = TradingEngine(self.settings).rfq_state
            self.assertEqual((persisted["rfq_id"], persisted["status"]), (rfq_id, "CancelUnknown"))
            return {"rfqId": rfq_id, "status": "Canceled"}

        self.engine.client.cancel_rfq.side_effect = cancel
        self.engine.client.rfq_realtime.side_effect = AssertionError("Cancel must not query realtime")
        self.engine.client.rfq_history.side_effect = AssertionError("Cancel must not query history")
        result = await self.engine.cancel_rfq(RfqCancelRequest(rfq_id="rfq-1"))
        self.assertEqual(result["status"], "CancelUnknown")
        self.assertEqual(result["cancellation"]["status"], "Canceled")
        self.assertTrue(self.engine._rfq_unresolved())
        result["legs"][0]["qty"] = "100"
        self.assertEqual(self.engine.rfq_state["legs"][0]["qty"], "0.01")
        self.engine.client.rfq_realtime.assert_not_awaited()
        self.engine.client.rfq_history.assert_not_awaited()

    async def test_lost_cancel_and_lagging_active_remain_blocked_after_restart(self):
        self.active_rfq()
        self.engine.client.cancel_rfq.side_effect = httpx.ReadTimeout("lost acknowledgement")
        with self.assertRaises(httpx.ReadTimeout):
            await self.engine.cancel_rfq(RfqCancelRequest(rfq_id="rfq-1"))
        restored = TradingEngine(self.settings)
        self.mock_client(restored)
        restored.client.rfq_history.return_value = [{"rfqId": "rfq-1", "status": "Active"}]
        snapshot = deepcopy(restored.rfq_state)
        self.assertEqual(await restored.refresh_rfq(), snapshot)
        self.assertEqual(TradingEngine(self.settings).rfq_state, snapshot)
        restored.client.quote_realtime.assert_not_awaited()
        restored.client.rfq_history.assert_awaited_once()
        actions = [restored.cancel_rfq(RfqCancelRequest(rfq_id="rfq-1")),
                   restored.create_rfq(RfqCreateRequest(quantity=.01)),
                   restored.execute_rfq(RfqExecuteRequest(confirm_live=True, rfq_id="rfq-1", quote_id="quote-1", quote_side="Sell"))]
        for action in actions:
            with self.assertRaisesRegex(ValueError, "Unresolved RFQ"):
                await action
        restored.client.cancel_rfq.assert_not_awaited()
        restored.client.create_rfq.assert_not_awaited()
        restored.client.execute_quote.assert_not_awaited()
        self.assertEqual(restored.rfq_state, snapshot)

    async def test_cancel_reconciles_terminal_history_despite_active_realtime(self):
        self.active_rfq()
        await self.engine.cancel_rfq(RfqCancelRequest(rfq_id="rfq-1"))
        self.engine.client.rfq_history.return_value = [{"rfqId": "rfq-1", "status": "Canceled"}]
        result = await self.engine.refresh_rfq()
        self.assertEqual(result["status"], "Canceled")
        self.assertTrue(result["execution_resolved"])
        self.assertFalse(self.engine._rfq_unresolved())
        self.engine.client.quote_realtime.assert_not_awaited()
        self.engine.client.rfq_history.assert_awaited_once_with("rfq-1", rfq_link_id="link1")

    async def test_cancel_intent_survives_pending_fill_then_lagging_active(self):
        self.active_rfq()
        await self.engine.cancel_rfq(RfqCancelRequest(rfq_id="rfq-1"))
        expected = deepcopy(self.engine.rfq_state)
        for status in ("PendingFill", "Active"):
            with self.subTest(status=status):
                self.engine.client.rfq_realtime.return_value = [{"rfqId": "rfq-1", "status": status}]
                self.assertEqual(await self.engine.refresh_rfq(), expected)
                self.assertTrue(self.engine._rfq_unresolved())
                with self.assertRaisesRegex(ValueError, "Unresolved RFQ"):
                    await self.engine.execute_rfq(RfqExecuteRequest(
                        confirm_live=True, rfq_id="rfq-1", quote_id="quote-1", quote_side="Sell"))
        self.engine.client.quote_realtime.assert_not_awaited()
        self.engine.client.execute_quote.assert_not_awaited()

    async def test_stable_terminal_states_skip_remote_reads(self):
        for status in ("Canceled", "Expired", "Failed", "Filled"):
            with self.subTest(status=status):
                self.active_rfq()
                self.engine.rfq_state.update(status=status, tracking_applied=True, execution_resolved=True)
                expected = deepcopy(self.engine.rfq_state)
                self.assertEqual(await self.engine.refresh_rfq(), expected)
                async with self.engine.lock:
                    self.assertEqual(await self.engine._refresh_rfq(), expected)
                self.engine.client.rfq_realtime.assert_not_awaited()
                self.engine.client.rfq_history.assert_not_awaited()
                self.engine.client.quote_realtime.assert_not_awaited()

    async def test_cancel_never_clears_selected_quote_even_if_marked_resolved(self):
        self.active_rfq()
        self.engine.rfq_state.update(selected_quote_id="quote-1", execution_resolved=True)
        with self.assertRaisesRegex(ValueError, "unexecuted RFQ"):
            await self.engine.cancel_rfq(RfqCancelRequest(rfq_id="rfq-1"))
        self.engine.client.cancel_rfq.assert_not_awaited()
        self.assertEqual(self.engine.rfq_state["selected_quote_id"], "quote-1")


if __name__ == "__main__":
    unittest.main()
