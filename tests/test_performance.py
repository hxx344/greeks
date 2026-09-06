import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.engine import TradingEngine
from app.models import ExecutionRecord, PerformanceSample, Position
from app.performance import build_performance


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
SYMBOL = "BTC-6SEP26-100000-C-USDT"


def fill(identity, side, qty, price, fee, day, group="a", opening=None, symbol=SYMBOL):
    return ExecutionRecord(symbol=symbol, side=side, exec_qty=qty, exec_price=price, exec_fee=fee,
        exec_id=identity, order_id=identity, order_link_id=f"ic-{group}-{identity}", fee_currency="USDT" if symbol.endswith("USDT") else "USDC",
        exec_time=NOW + timedelta(days=day), execution_group=group, opening_group=opening, reduce_only=opening is not None)


class PerformanceTests(unittest.TestCase):
    def expiry_group(self, total=97, early_fee=0, close_time=None):
        row = {"symbol":SYMBOL,"side":"Sell","qty":"1","openTime":int(NOW.timestamp()*1000),
            "closeTime":int((close_time or NOW+timedelta(days=5,hours=8)).timestamp()*1000),"avgEntryPrice":"100",
            "totalPnl":str(total),"totalOpenFee":"1","totalCloseFee":str(early_fee),"deliveryFee":"2"}
        return {"type":"open","created_at":NOW.isoformat(),"status":"closed", "legs":{SYMBOL:{"side":"Sell","qty":1}},
                "closure_evidence":{f"{SYMBOL}|Sell":[row]}}

    def test_only_expiry_combinations_contribute_sampled_returns(self):
        groups = {"held":self.expiry_group(), "early":self.expiry_group(999,close_time=NOW+timedelta(days=2)),
                  "partial":self.expiry_group(333,early_fee=1)}
        # Distinct position history identities avoid ambiguous evidence reuse.
        partial_evidence = groups["partial"]["closure_evidence"][f"{SYMBOL}|Sell"][0]
        partial_evidence["openTime"] += 1000
        samples = {"held":[PerformanceSample(time=NOW+timedelta(hours=1),pnl=-1),PerformanceSample(time=NOW+timedelta(hours=2),pnl=120),
                           PerformanceSample(time=NOW+timedelta(hours=3),pnl=40)],
                   "early":[PerformanceSample(time=NOW+timedelta(hours=2),pnl=999)]}
        report = build_performance(groups, [], {}, {}, samples=samples)
        self.assertEqual(len(report["series"]), 1)
        series = report["series"][0]
        self.assertEqual(series["closed_count"], 1)
        self.assertEqual(series["total_pnl"], 97)
        self.assertEqual([point["cumulative"] for point in series["points"]], [-1,120,40,97])
        self.assertEqual(series["max_drawdown"], 80)
        self.assertEqual(series["sampled_count"], 3)
        self.assertEqual({row["id"] for row in report["groups"] if row["eligible"]}, {"held"})

    def test_zero_fee_partial_early_close_excludes_whole_group_after_expiry(self):
        groups = {"a":self.expiry_group()}
        records = [fill("open","Sell",1,100,1,0),fill("partial","Buy",.2,50,0,1,"c","a")]
        report = build_performance(groups, records, {}, {})
        self.assertEqual(report["groups"][0]["status"], "closed")
        self.assertEqual(report["groups"][0]["closure_kind"], "early")
        self.assertFalse(report["groups"][0]["eligible"])
        self.assertEqual(report["series"], [])

    def test_late_manual_close_without_settlement_evidence_does_not_count_as_expiry(self):
        records = [fill("open","Sell",1,100,1,0),fill("late","Buy",1,50,1,6,"c","a")]
        row = build_performance({}, records, {}, {})["groups"][0]
        self.assertEqual(row["closure_kind"], "unknown")
        self.assertFalse(row["eligible"])

    def test_nonfinite_exchange_values_are_rejected_before_archival(self):
        with self.assertRaises(ValueError):
            fill("invalid", "Buy", 1, float("nan"), 1, 0)

    def test_partial_closes_fees_rebates_and_unique_fills(self):
        records = [fill("open", "Sell", 2, 100, 2, 0), fill("close1", "Buy", .5, 80, -.1, 1, "close", "a")]
        report = build_performance({}, records + records, {}, {})
        row = report["groups"][0]
        self.assertEqual(row["net_pnl"], 9.6)
        self.assertEqual(row["remaining_qty"], 1.5)
        self.assertEqual(row["status"], "open")
        self.assertEqual(report["series"], [])
        records.append(fill("close2", "Buy", 1.5, 120, .3, 2, "close", "a"))
        report = build_performance({}, records, {}, {})
        self.assertEqual(report["groups"][0]["net_pnl"], -22.2)
        self.assertEqual(report["series"], [])
        self.assertEqual(report["groups"][0]["closure_kind"], "early")

    def test_groups_and_currencies_never_mix_and_curve_uses_closing_time(self):
        records = [fill("a-open", "Buy", 1, 100, 1, 0), fill("b-open", "Buy", 1, 100, 1, 1, "b"),
            fill("b-close", "Sell", 1, 120, 1, 2, "bc", "b"), fill("a-close", "Sell", 1, 90, 1, 3, "ac", "a"),
            fill("c-open", "Buy", 1, 100, 1, 0, "c", symbol=SYMBOL[:-5]),
            fill("c-close", "Sell", 1, 130, 1, 4, "cc", "c", symbol=SYMBOL[:-5])]
        report = build_performance({}, records, {}, {})
        self.assertEqual(report["series"], [])
        self.assertEqual({row["currency"] for row in report["groups"]}, {"USDT", "USDC"})
        self.assertTrue(all(row["closure_kind"] == "early" for row in report["groups"]))

    def test_missing_history_and_ambiguous_manual_close_are_not_profit(self):
        records = [fill("open", "Buy", 1, 100, 1, 0), fill("close", "Sell", 1, 120, 1, 1, "c", "a")]
        groups = {"a": {"type": "open"}}
        journal = {"link": dict(symbol=SYMBOL,side="Buy",filledQty=2,reduce_only=False,terminal=True)}
        report = build_performance(groups, records, {"link":"a"}, journal)
        self.assertEqual(report["groups"][0]["status"], "pending")
        self.assertIsNone(report["groups"][0]["net_pnl"])
        self.assertEqual(report["series"], [])
        records.insert(1, fill("other", "Buy", 1, 100, 1, 0, "b"))
        records[-1] = records[-1].model_copy(update={"opening_group":None,"closed_size":1})
        report = build_performance({}, records, {}, {})
        self.assertEqual(report["unassigned_closes"], 1)
        self.assertEqual(report["series"], [])

    def test_verified_delivery_is_net_of_fees_and_not_double_counted(self):
        evidence = {"symbol":SYMBOL,"side":"Sell","qty":"1","openTime":int(NOW.timestamp()*1000),
            "closeTime":int((NOW+timedelta(days=5,hours=8)).timestamp()*1000),"avgEntryPrice":"100",
            "totalPnl":"97","totalOpenFee":"1","totalCloseFee":"0","deliveryFee":"2"}
        groups = {"a": {"type":"open","created_at":NOW.isoformat(),"status":"closed",
            "legs":{SYMBOL:{"side":"Sell","qty":1}},"closure_evidence":{f"{SYMBOL}|Sell":[evidence,evidence]}}}
        report = build_performance(groups, [fill("open","Sell",1,100,1,0),fill("close","Buy",1,0,0,5,"c","a").model_copy(update={"exec_time":NOW+timedelta(days=5,hours=8)})], {}, {})
        self.assertEqual(report["groups"][0]["net_pnl"], 97)
        self.assertEqual(report["groups"][0]["delivery_fee"], 2)
        self.assertEqual(report["series"][0]["total_pnl"], 97)
        # RFQ without individual fill identities can use complete, verified closure records.
        self.assertEqual(build_performance(groups, [], {}, {})["series"][0]["total_pnl"], 97)
        evidence["qty"] = "2"
        self.assertEqual(build_performance(groups, [], {}, {})["series"], [])

    def test_floating_requires_matching_live_position_and_complete_ownership(self):
        position = Position(symbol=SYMBOL,side="Buy",size=1,avg_price=100,mark_price=130,unrealised_pnl=30,source="bybit")
        records = [fill("open","Buy",1,100,1,0)]
        self.assertEqual(build_performance({}, records, {}, {}, [position], True)["groups"][0]["floating_pnl"], 30)
        self.assertIsNone(build_performance({}, records, {}, {}, [position], False)["groups"][0]["floating_pnl"])
        self.assertIsNone(build_performance({}, records, {}, {}, [position.model_copy(update={"size":2})], True)["groups"][0]["floating_pnl"])

    def test_missing_leg_from_known_combination_cannot_be_reported_as_complete(self):
        records = [fill("open", "Buy", 1, 100, 1, 0), fill("close", "Sell", 1, 120, 1, 1, "c", "a")]
        groups = {"a": {"type":"open","legs": {SYMBOL: {"side":"Buy","qty":1}, "BTC-6SEP26-95000-P-USDT": {"side":"Sell","qty":1}}}}
        report = build_performance(groups, records, {}, {})
        self.assertEqual(report["groups"][0]["status"], "pending")
        self.assertEqual(report["series"], [])


class PerformancePersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = Settings(_env_file=None,state_file=f"{self.directory.name}/state.json",bybit_api_key="test",bybit_api_secret="test",trading_mode="live")
        self.engine = TradingEngine(self.settings)

    async def test_archive_survives_restart_and_retains_more_than_100_fills(self):
        self.engine._archive_performance([fill(str(i),"Buy",1,100,1,0) for i in range(150)])
        self.engine._archive_performance([fill("1","Buy",1,100,1,0)])
        restored = TradingEngine(self.settings)
        self.assertIsNone(restored.state_error)
        self.assertEqual(len(restored.performance_executions), 150)
        self.assertEqual(restored.performance_executions["1"].exec_time, NOW)

    async def test_sampling_is_real_fee_adjusted_throttled_and_persistent(self):
        self.engine._archive_performance([fill("open","Buy",1,100,1,0)])
        self.engine.load_recent_executions = AsyncMock(return_value=[])
        self.engine.client.positions = AsyncMock(return_value=[dict(symbol=SYMBOL,side="Buy",size="1",avgPrice="100",markPrice="110",unrealisedPnl="10")])
        with patch("app.performance.datetime", wraps=datetime) as clock:
            clock.now.return_value = NOW+timedelta(days=1)
            await self.engine.sample_performance()
            await self.engine.sample_performance()
            self.assertEqual(len(self.engine.performance_samples["a"]), 1)
            self.assertEqual(self.engine.performance_samples["a"][0].pnl, 9)
            clock.now.return_value += timedelta(seconds=60)
            self.engine.client.positions.return_value[0]["markPrice"] = "125"
            await self.engine.sample_performance()
        self.assertEqual([p.pnl for p in self.engine.performance_samples["a"]], [9,24])
        self.assertEqual(self.engine.performance_report()["series"], [])
        restored = TradingEngine(self.settings)
        self.assertEqual([p.pnl for p in restored.performance_samples["a"]], [9,24])
        self.assertIsNone(restored.state_error)

    async def test_failed_position_sampling_does_not_invent_points(self):
        self.engine._archive_performance([fill("open","Buy",1,100,1,0)])
        self.engine.client.positions = AsyncMock(side_effect=RuntimeError("offline"))
        await self.engine.sample_performance()
        self.assertEqual(self.engine.performance_samples, {})
        self.assertIsNotNone(self.engine.performance_sample_error)

    async def test_history_cursor_only_advances_after_complete_success(self):
        now = datetime.now(timezone.utc)
        self.engine.execution_groups["a"] = {"type":"open","created_at":(now-timedelta(days=20)).isoformat()}
        self.engine.client.execution_history = AsyncMock(side_effect=RuntimeError("offline"))
        await self.engine.sync_performance()
        cursor = self.engine.performance_cursor_ms
        self.assertIsNotNone(self.engine.performance_error)
        self.engine.client.execution_history = AsyncMock(return_value=[])
        await self.engine.sync_performance()
        self.assertGreater(self.engine.performance_cursor_ms, cursor)
        self.assertIsNone(self.engine.performance_error)
        restored = TradingEngine(self.settings)
        self.assertEqual(restored.performance_cursor_ms, self.engine.performance_cursor_ms)
        self.assertTrue(restored.performance_report()["syncing"])

    async def test_history_import_deduplicates_and_preserves_close_identity(self):
        now = datetime.now(timezone.utc)
        raw = dict(symbol=SYMBOL,side="Buy",execQty="1",execPrice="100",execFee="1",feeCurrency="USDT",
                   execId="fill-a",orderId="order",orderLinkId="ic-a-open",execTime=str(int(now.timestamp()*1000)),execType="Trade",closedSize="0")
        self.engine.client.execution_history = AsyncMock(return_value=[raw,raw])
        await self.engine.sync_performance()
        self.assertEqual(len(self.engine.performance_executions), 1)
        await self.engine.sync_performance()
        self.assertEqual(len(self.engine.performance_executions), 1)
        self.assertIsNone(self.engine.performance_error)
        self.assertEqual(self.engine.performance_report()["network"], "mainnet")
        self.assertEqual(self.engine.performance_executions["fill-a"].closed_size, 0)

    async def test_history_fetch_follows_all_pages_beyond_recent_execution_limit(self):
        self.engine.client._request = AsyncMock(side_effect=[
            {"list":[{"execId":str(i)} for i in range(100)],"nextPageCursor":"page2"},
            {"list":[{"execId":str(i)} for i in range(100,150)],"nextPageCursor":""}])
        result = await self.engine.client.execution_history(1000, 2000)
        self.assertEqual(len(result), 150)
        self.assertEqual(self.engine.client._request.await_count, 2)
        self.assertEqual(self.engine.client._request.await_args.args[2]["cursor"], "page2")
