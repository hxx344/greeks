import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.bybit import BybitClient, BybitError
from app.config import Settings
from app.engine import TradingEngine
from app.models import CloseRequest, OpenRequest, RfqCancelRequest, RfqCreateRequest, RfqExecuteRequest
from app.strategy import build_iron_condor, demo_chain


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(_env_file=None, state_file=str(Path(directory.name) / "state.json"),
                                 live_trading=True, bybit_testnet=False, bybit_api_key="test", bybit_api_secret="test")
        self.engine = TradingEngine(self.settings)
        self.now = datetime.now(timezone.utc)
        self.started = self.now - timedelta(days=3)
        self.engine.chain = demo_chain(self.now)
        self.preview = build_iron_condor(self.engine.chain, self.now, qty=0.01)
        self.engine.chain_source = "bybit"
        self.engine.chain_updated_at = self.now
        self.engine.make_preview = AsyncMock(return_value=self.preview)
        self.engine._validate_open_calendar = Mock()
        self.engine._capture_pm_baseline = AsyncMock()
        self.engine.load_recent_executions = AsyncMock(return_value=[])
        self.engine.follow_bbo_order = AsyncMock(return_value={"status": "filled", "filledQty": 0.01})
        # Unmocked exchange operations fail before creating a transport.
        self.engine.client._request = AsyncMock(side_effect=AssertionError("Unexpected exchange request"))
        self.engine.client.positions = AsyncMock(return_value=[])
        self.engine.client.closed_option_positions = AsyncMock(return_value=[])
        self.engine.client.rfq_realtime = AsyncMock(return_value=[])
        self.engine.client.rfq_history = AsyncMock(return_value=[])
        self.engine.client.quote_realtime = AsyncMock(return_value=[])
        self.engine.client.cancel_rfq = AsyncMock(return_value={})

    def track(self):
        e = self.engine
        e.active_strategy_group_id = "opening"
        e.active_strategy_symbols = {leg.symbol for leg in self.preview.legs}
        e.active_strategy_sizes = {f"{leg.symbol}|{leg.side}": leg.qty for leg in self.preview.legs}
        e.execution_groups["opening"] = {"type": "open", "order_tracking": True, "created_at": self.started.isoformat()}
        e._save_state()

    def rfq(self, status="ExecutionUnknown"):
        self.engine.rfq_state = {"rfq_id": "rfq-1", "status": status, "selected_quote_id": "quote-1",
                                 "execution_started_at": self.started.isoformat(),
                                 "legs": [{"symbol": leg.symbol, "side": leg.side, "qty": str(leg.qty)} for leg in self.preview.legs]}

    def closure(self, symbol, side, **extra):
        return {"symbol": symbol, "side": side, "qty": "0.01",
                "openTime": int((self.started + timedelta(seconds=1)).timestamp() * 1000),
                "closeTime": int((self.now - timedelta(seconds=1)).timestamp() * 1000), **extra}

    def supply_closures(self):
        async def history(symbol, *_):
            return [self.closure(symbol, next(leg.side for leg in self.preview.legs if leg.symbol == symbol))]
        self.engine.client.closed_option_positions.side_effect = history

    async def test_every_unresolved_rfq_blocks_open_and_replacement_after_restart(self):
        for status in ("ExecutionUnknown", "PendingFill", "Filled", "Canceled", "Expired", "Failed"):
            self.rfq(status)
            self.engine._save_state()
            restored = TradingEngine(self.settings)
            restored.make_preview = self.engine.make_preview
            with self.subTest(status=status):
                with self.assertRaisesRegex(ValueError, "Unresolved RFQ"):
                    await restored.open_position(OpenRequest(confirm_live=True, quantity=0.01))
                with self.assertRaisesRegex(ValueError, "Unresolved RFQ"):
                    await restored.create_rfq(RfqCreateRequest(quantity=0.01))
        self.engine.follow_bbo_order.assert_not_awaited()

    async def test_uncertain_execution_cannot_be_erased_by_cancel(self):
        self.rfq()
        with self.assertRaisesRegex(ValueError, "Unresolved RFQ"):
            await self.engine.cancel_rfq(RfqCancelRequest(rfq_id="rfq-1"))
        self.assertEqual(self.engine.rfq_state["selected_quote_id"], "quote-1")
        self.engine.client.cancel_rfq.assert_not_awaited()

    async def test_cancel_response_loss_retains_identity_and_recovers_from_history(self):
        self.engine.rfq_state = {"rfq_id": "rfq-1", "status": "Active"}
        self.engine.client.cancel_rfq.side_effect = httpx.ReadTimeout("lost")
        with self.assertRaises(httpx.ReadTimeout):
            await self.engine.cancel_rfq(RfqCancelRequest(rfq_id="rfq-1"))
        self.assertEqual(TradingEngine(self.settings).rfq_state["status"], "CancelUnknown")
        self.engine.client.rfq_history.return_value = [{"rfqId": "rfq-1", "status": "Canceled"}]
        await self.engine.reconcile_once()
        self.assertFalse(self.engine._rfq_unresolved())
        self.assertEqual(self.engine.rfq_state["rfq_id"], "rfq-1")

    async def test_lost_creation_response_recovers_by_persisted_link(self):
        self.engine.client.rfq_config = AsyncMock(return_value={"counterparties": ["desk"]})
        self.engine.client.create_rfq = AsyncMock(side_effect=httpx.ReadTimeout("lost"))
        with self.assertRaises(httpx.ReadTimeout):
            await self.engine.create_rfq(RfqCreateRequest(quantity=0.01))
        state = TradingEngine(self.settings).rfq_state
        self.assertEqual(state["status"], "CreationUnknown")
        self.engine.client.rfq_realtime.return_value = [{"rfqId": "recovered", "rfqLinkId": state["rfq_link_id"], "status": "Active"}]
        await self.engine.reconcile_once()
        self.assertEqual(self.engine.rfq_state["rfq_id"], "recovered")
        self.engine.client.rfq_realtime.assert_awaited_once_with(rfq_link_id=state["rfq_link_id"])
        self.engine.client.create_rfq.assert_awaited_once()

    async def test_wrong_identity_or_missing_history_never_releases_rfq(self):
        self.rfq()
        self.engine.client.rfq_realtime.return_value = [{"rfqId": "other", "status": "Failed"}]
        await self.engine.reconcile_once()
        self.assertTrue(self.engine._rfq_unresolved())
        self.assertIsNotNone(self.engine.reconciliation_health()["error"])
        self.assertGreater(self.engine.reconciliation_health()["oldest_pending_seconds"], 0)

    async def test_filled_rfq_background_recovery_works_without_quotes_or_browser(self):
        self.rfq()
        self.engine.client.rfq_realtime.return_value = [{"rfqId": "rfq-1", "status": "Filled"}]
        self.engine.client.positions.return_value = [{"symbol": leg.symbol, "side": leg.side, "size": "0.01"} for leg in self.preview.legs]
        self.engine.client.quote_realtime.side_effect = RuntimeError("quotes unavailable")
        await self.engine.reconcile_once()
        self.assertFalse(self.engine._rfq_unresolved())
        self.assertEqual(len(self.engine.active_strategy_symbols), 4)
        self.assertIsNotNone(self.engine.reconciliation_last_success)
        self.engine.client.quote_realtime.assert_not_awaited()

    async def test_pending_rfq_cannot_be_confirmed_from_account_positions(self):
        self.rfq("PendingFill")
        positions = self.engine._parse_positions([{"symbol": leg.symbol, "side": leg.side, "size": "0.01"} for leg in self.preview.legs])
        self.assertFalse(self.engine._track_filled_rfq(positions))
        self.assertTrue(self.engine._rfq_unresolved())

    async def test_empty_positions_without_evidence_preserve_tracking(self):
        self.track()
        await self.engine.reconcile_once()
        self.assertEqual(len(self.engine.active_strategy_symbols), 4)
        self.assertIn("closure evidence", self.engine.reconciliation_error)
        with self.assertRaisesRegex(ValueError, "Close the tracked"):
            await self.engine.open_position(OpenRequest(confirm_live=True, quantity=0.01))

    async def test_confirmed_external_closure_archives_and_allows_next_open(self):
        self.track()
        self.supply_closures()
        await self.engine.reconcile_once()
        self.assertFalse(self.engine.active_strategy_symbols)
        restored = TradingEngine(self.settings)
        group = restored.execution_groups["opening"]
        self.assertEqual(group["status"], "closed")
        self.assertEqual(len(group["closure_evidence"]), 4)
        await self.engine.open_position(OpenRequest(confirm_live=True, quantity=0.01))
        self.assertEqual(self.engine.follow_bbo_order.await_count, 4)

    async def test_reappearing_position_is_not_retired(self):
        self.track()
        self.supply_closures()
        positions = [{"symbol": leg.symbol, "side": leg.side, "size": "0.01"} for leg in self.preview.legs]
        self.engine.client.positions.side_effect = [[], positions]
        await self.engine.reconcile_once()
        self.assertEqual(len(self.engine.active_strategy_symbols), 4)

    async def test_prior_position_evidence_and_duplicate_rows_cannot_clear_tracking(self):
        self.track()
        async def old_history(symbol, *_):
            side = next(leg.side for leg in self.preview.legs if leg.symbol == symbol)
            old = self.closure(symbol, side, openTime=int((self.started - timedelta(days=1)).timestamp() * 1000))
            small = self.closure(symbol, side, qty="0.004")
            return [old, small, small]
        self.engine.client.closed_option_positions.side_effect = old_history
        await self.engine.reconcile_once()
        self.assertEqual(len(self.engine.active_strategy_symbols), 4)

    async def test_malformed_positions_do_not_clear_tracking(self):
        self.track()
        for raw in ([{"symbol": "A", "side": "Sell", "size": "nan"}], [{}], [None]):
            self.engine.client.positions.return_value = raw
            await self.engine.reconcile_once()
            self.assertEqual(len(self.engine.active_strategy_symbols), 4)
            self.assertTrue(self.engine.reconciliation_error)
        self.engine.client.closed_option_positions.assert_not_awaited()

    async def test_background_cancels_and_reconciles_pending_order_after_restart(self):
        self.track()
        e = self.engine
        leg = self.preview.legs[0]
        e.order_journal["ic-pending"] = {"symbol": leg.symbol, "side": leg.side, "qty": 0.01,
                                        "reduce_only": False, "terminal": False, "status": "unknown", "filledQty": 0.0}
        e._save_state()
        e.client.cancel_order = AsyncMock(return_value={})
        e.client.order = AsyncMock(return_value={"side": leg.side, "qty": "0.01", "cumExecQty": "0", "orderStatus": "Cancelled"})
        e.client.positions.return_value = [{"symbol": leg.symbol, "side": leg.side, "size": "0.01"} for leg in self.preview.legs]
        e._load_state()
        await e.reconcile_once()
        e.client.cancel_order.assert_awaited_once()
        self.assertTrue(e.order_journal["ic-pending"]["terminal"])
        self.assertEqual(e.reconciliation_health()["pending_orders"], 0)

    async def test_background_loop_recovers_after_error_and_propagates_cancellation(self):
        e = self.engine
        e.reconcile_once = AsyncMock(side_effect=[RuntimeError("temporary"), None])
        with patch("app.reconciliation.asyncio.sleep", new=AsyncMock(side_effect=[None, asyncio.CancelledError])):
            with self.assertRaises(asyncio.CancelledError):
                await e.reconciliation_loop()
        self.assertEqual(e.reconcile_once.await_count, 2)

    async def test_position_refresh_does_not_mutate_during_order_execution(self):
        self.track()
        async with self.engine.lock:
            await self.engine.load_positions()
        self.engine.client.positions.assert_not_awaited()

    async def test_partial_external_closure_remains_degraded_until_all_absent_legs_resolve(self):
        self.track()
        first = self.preview.legs[0]
        self.engine.client.closed_option_positions.side_effect = lambda symbol, *_: [self.closure(symbol, first.side)] if symbol == first.symbol else []
        await self.engine.reconcile_once()
        self.assertEqual(len(self.engine.active_strategy_symbols), 3)
        self.assertIn("still lack", self.engine.reconciliation_error)
        self.supply_closures()
        await self.engine.reconcile_once()
        self.assertFalse(self.engine.active_strategy_symbols)
        self.assertIsNone(self.engine.reconciliation_error)

    async def test_health_exposes_unresolved_rfq_and_last_recovery(self):
        from app.main import app
        self.rfq()
        with patch("app.main.engine", self.engine), patch("app.main.settings", self.settings):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                payload = (await client.get("/api/health")).json()
        self.assertEqual(payload["status"], "degraded")
        self.assertTrue(payload["reconciliation"]["pending_rfq"])
        self.assertIsNone(payload["reconciliation"]["last_success_at"])


class ModeAndHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_testnet_open_close_and_rfq_use_real_executor_with_fake_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(_env_file=None, state_file=str(Path(directory) / "testnet.json"), trading_mode="testnet",
                                bybit_api_key="test", bybit_api_secret="test")
            settings.bbo_poll_seconds = 0.001
            e = TradingEngine(settings)
            now = datetime.now(timezone.utc)
            e.chain = demo_chain(now)
            e.chain_source, e.chain_updated_at = "bybit", now
            preview = build_iron_condor(e.chain, now, qty=0.01)
            e.make_preview = AsyncMock(return_value=preview)
            e.refresh_chain = AsyncMock(return_value=e.chain)
            e._validate_open_calendar = Mock()
            e._capture_pm_baseline = AsyncMock()
            e.load_recent_executions = AsyncMock(return_value=[])
            orders, positions, hosts = {}, {}, []
            def respond(request):
                import json
                hosts.append(request.url.host)
                path, params = request.url.path, request.url.params
                if path == "/v5/market/tickers":
                    instrument = next(item for item in e.chain if item.symbol == params["symbol"])
                    result = {"list": [{"bid1Price": str(instrument.bid), "ask1Price": str(instrument.ask)}]}
                elif path == "/v5/order/create":
                    order = json.loads(request.content)
                    orders[order["orderLinkId"]] = order
                    result = {"orderId": order["orderLinkId"]}
                elif path == "/v5/order/realtime":
                    order = orders[params["orderLinkId"]]
                    key = order["symbol"]
                    if order["reduceOnly"]:
                        positions.pop(key, None)
                    else:
                        positions[key] = {"symbol": key, "side": order["side"], "size": order["qty"]}
                    result = {"list": [{**order, "orderStatus": "Filled", "cumExecQty": order["qty"]}]}
                elif path == "/v5/position/list":
                    result = {"list": list(positions.values())}
                elif path == "/v5/rfq/execute-quote":
                    result = {"rfqId": "rfq-1", "quoteId": "quote-1", "status": "PendingFill"}
                else:
                    raise AssertionError(f"Unexpected endpoint {path}")
                return httpx.Response(200, json={"retCode": 0, "result": result})
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
                e.client._client = transport
                with self.assertRaisesRegex(ValueError, "confirmation"):
                    await e.open_position(OpenRequest(quantity=0.01))
                results = await e.open_position(OpenRequest(confirm_live=True, quantity=0.01))
                self.assertTrue(all(item.status == "filled" for item in results))
                self.assertEqual(len(e.active_strategy_symbols), 4)
                results, _ = await e.close_position(CloseRequest(confirm_live=True))
                self.assertTrue(all(item.status == "filled" for item in results))
                self.assertFalse(e.active_strategy_symbols)
                e.rfq_state = {"rfq_id": "rfq-1", "status": "Active", "legs": [
                    {"symbol": leg.symbol, "side": leg.side, "qty": "0.01"} for leg in preview.legs],
                    "quotes": [{"quoteId": "quote-1", "quoteSellList": [
                        {"symbol": leg.symbol, "qty": "0.01", "price": str(leg.mark_price)} for leg in preview.legs]}]}
                await e.execute_rfq(RfqExecuteRequest(confirm_live=True, rfq_id="rfq-1", quote_id="quote-1", quote_side="Sell"))
                self.assertTrue(e._rfq_unresolved())
            self.assertEqual(len(orders), 8)
            self.assertEqual(set(hosts), {"api-testnet.bybit.com"})

    async def test_modes_and_testnet_wire_routes(self):
        defaults = Settings(_env_file=None)
        self.assertEqual(defaults.environment, "dry-run")
        self.assertFalse(defaults.can_send_orders)
        legacy_testnet = Settings(_env_file=None, live_trading=True, bybit_testnet=True, bybit_api_key="key", bybit_api_secret="secret")
        self.assertFalse(legacy_testnet.can_send_orders)
        testnet = Settings(_env_file=None, trading_mode="testnet", bybit_api_key="key", bybit_api_secret="secret")
        self.assertTrue(testnet.can_send_orders)
        self.assertFalse(testnet.can_trade_live)
        self.assertFalse(Settings(_env_file=None, trading_mode="live", bybit_api_key="key", bybit_api_secret="secret").can_send_orders)
        urls = []
        def respond(request):
            urls.append(str(request.url))
            return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})
        with tempfile.TemporaryDirectory() as directory:
            testnet.state_file = str(Path(directory) / "state.json")
            e = TradingEngine(testnet)
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                e.client._client = client
                await e.client.tickers()
                await e.client.place_limit_order("BTC-TEST", "Buy", 0.01, 10, "ic-test")
                await e.client.cancel_order("BTC-TEST", "ic-test")
                await e.client.execute_quote("rfq-1", "quote-1", "Sell")
            self.assertTrue(all(url.startswith("https://api-testnet.bybit.com/") for url in urls))
            e._save_state()
            mainnet = testnet.model_copy(update={"trading_mode": "live", "live_trading": True})
            self.assertIsNotNone(TradingEngine(mainnet).state_error)

    async def test_closed_position_queries_cover_windows_and_pages(self):
        client = BybitClient()
        client._request = AsyncMock(side_effect=[
            {"list": [{"symbol": "A"}], "nextPageCursor": "page2"},
            {"list": [{"symbol": "B"}]}, {"list": [{"symbol": "C"}]}])
        rows = await client.closed_option_positions("BTC-TEST", 0, 8 * 86400000)
        self.assertEqual(len(rows), 3)
        calls = client._request.await_args_list
        self.assertEqual(calls[1].args[2]["cursor"], "page2")
        self.assertEqual(calls[2].args[2]["startTime"], calls[0].args[2]["endTime"] + 1)
        client._request = AsyncMock(return_value={"list": [], "nextPageCursor": "same"})
        with self.assertRaises(BybitError):
            await client.closed_option_positions("BTC-TEST", 0, 1)


if __name__ == "__main__":
    unittest.main()
