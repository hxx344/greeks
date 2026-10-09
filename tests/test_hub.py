import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.hub import build_summary
from app.main import app
from app.models import OptionInstrument


NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def snapshot(**overrides):
    sunday = datetime(2026, 10, 11, 8, tzinfo=timezone.utc)
    chain = [OptionInstrument(symbol=f"BTC-{kind}", expiry=sunday, strike=100000,
                              option_type=kind, delta=0.4, mark_price=100)
             for kind in ("Call", "Put")]
    values = dict(settings=Settings(_env_file=None, trading_mode="dry-run", bybit_api_key="", bybit_api_secret=""),
                  chain=chain, chain_source="bybit", chain_updated_at=NOW - timedelta(seconds=5),
                  reconciliation_last_success=None, reconciliation_error=None,
                  btc_price=100000, active_strategy_symbols=set(), order_journal={}, rfq_state={}, state_error=None)
    values.update(overrides)
    return SimpleNamespace(**values)


def live_settings(**kwargs):
    return Settings(_env_file=None, trading_mode="live", live_trading=True,
                    bybit_api_key="private-key", bybit_api_secret="private-secret", **kwargs)


class HubSummaryTests(unittest.TestCase):
    def test_reads_only_existing_memory_and_uses_source_time(self):
        engine = snapshot()
        before = copy.deepcopy(vars(engine))
        for name in ("make_preview", "refresh_chain", "load_positions", "_save_state", "performance_report",
                     "sync_performance", "sample_performance", "refresh_rfq", "reconciliation_health"):
            setattr(engine, name, Mock(side_effect=AssertionError("Summary must not call engine methods")))
        payload = build_summary(engine, NOW)
        self.assertEqual(payload["schemaVersion"], 2)
        self.assertEqual(payload["data"]["updatedAt"], engine.chain_updated_at.isoformat())
        self.assertEqual(payload["data"]["health"]["state"], "online")
        self.assertEqual({key: vars(engine)[key] for key in before}, before)
        for name in set(vars(engine)) - set(before):
            getattr(engine, name).assert_not_called()
        keys = {item["key"] for item in payload["data"]["metrics"]}
        self.assertNotIn("balance", keys)
        self.assertNotIn("pnl", keys)

    def test_live_uses_earliest_market_and_recovery_time_and_shortest_ttl(self):
        engine = snapshot(settings=live_settings(quote_stale_seconds=120, reconciliation_seconds=15),
                          reconciliation_last_success=NOW - timedelta(seconds=20))
        data = build_summary(engine, NOW)["data"]
        self.assertEqual(data["updatedAt"], engine.reconciliation_last_success.isoformat())
        self.assertEqual(data["health"]["staleAfterSeconds"], 60)
        self.assertEqual(data["health"]["state"], "online")
        engine.chain_updated_at = NOW - timedelta(seconds=25)
        self.assertEqual(build_summary(engine, NOW)["data"]["updatedAt"], engine.chain_updated_at.isoformat())

    def test_missing_or_invalid_recovery_time_never_uses_startup_or_request_time(self):
        for value in (None, NOW.replace(tzinfo=None), NOW + timedelta(seconds=1)):
            with self.subTest(value=value):
                engine = snapshot(settings=live_settings(), reconciliation_last_success=value)
                data = build_summary(engine, NOW)["data"]
                self.assertIsNone(data["updatedAt"])
                self.assertEqual(data["health"]["state"], "partial")
                self.assertIn("tracking:unknown", {item["id"] for item in data["diagnostics"]})

    def test_restored_tracking_without_credentials_is_not_current(self):
        data = build_summary(snapshot(active_strategy_symbols={"BTC-old"}), NOW)["data"]
        self.assertIsNone(data["updatedAt"])
        self.assertIn({"id": "tracking:paused", "kind": "action",
                       "message": "已保存交易跟踪状态，但当前未启用交易对账；请检查原项目配置"}, data["diagnostics"])

    def test_normal_listing_wait_is_notice_but_missing_sunday_quotes_are_fault(self):
        engine = snapshot()
        for item in engine.chain:
            item.expiry -= timedelta(days=1)
        data = build_summary(engine, NOW)["data"]
        self.assertEqual(data["health"]["state"], "online")
        self.assertEqual(data["diagnostics"][0]["kind"], "notice")
        engine.chain[0].expiry += timedelta(days=1)
        data = build_summary(engine, NOW)["data"]
        self.assertEqual(data["health"]["state"], "partial")
        self.assertEqual(data["diagnostics"][0]["id"], "market:quotes")

    def test_stale_missing_and_future_market_snapshots_do_not_show_a_price(self):
        for value in (None, NOW - timedelta(seconds=31), NOW + timedelta(seconds=1)):
            with self.subTest(value=value):
                data = build_summary(snapshot(chain_updated_at=value), NOW)["data"]
                self.assertEqual(data["health"]["state"], "stale")
                self.assertIsNone(next(item["value"] for item in data["metrics"] if item["key"] == "btc_price"))
                self.assertFalse(any(item["kind"] == "notice" for item in data["diagnostics"]))

    def test_unavailable_market_and_nonfinite_values_remain_json_safe(self):
        for price in (float("nan"), float("inf"), -5, True, None):
            with self.subTest(price=price):
                data = build_summary(snapshot(btc_price=price), NOW)["data"]
                self.assertIsNone(next(item["value"] for item in data["metrics"] if item["key"] == "btc_price"))
                json.dumps(data, allow_nan=False)
        data = build_summary(snapshot(chain_source="unavailable", chain=[]), NOW)["data"]
        self.assertEqual(data["health"]["state"], "offline")
        self.assertIsNone(data["updatedAt"])

    def test_pending_orders_and_rfq_are_faults_and_never_expose_private_state(self):
        engine = snapshot(settings=live_settings(), reconciliation_last_success=NOW,
                          reconciliation_error="private-secret failure", order_journal={"private-order": {"terminal": False}},
                          rfq_state={"status": "Filled", "tracking_applied": False, "secret": "private-key"})
        data = build_summary(engine, NOW)["data"]
        metrics = {item["key"]: item["value"] for item in data["metrics"]}
        self.assertEqual(metrics["pending_orders"], 1)
        self.assertEqual(metrics["pending_rfq"], 1)
        self.assertTrue({"orders:pending", "rfq:pending", "tracking:error"} <= {item["id"] for item in data["diagnostics"]})
        self.assertNotIn("private-", json.dumps(data))
        engine.state_error = "private-secret cannot load"
        data = build_summary(engine, NOW)["data"]
        self.assertTrue(any(item["kind"] == "action" for item in data["diagnostics"]))
        self.assertIsNone(next(item["value"] for item in data["metrics"] if item["key"] == "tracked_legs"))

    def test_old_successful_recovery_is_not_masked_by_recent_market(self):
        engine = snapshot(settings=live_settings(), reconciliation_last_success=NOW - timedelta(seconds=90))
        data = build_summary(engine, NOW)["data"]
        self.assertEqual(data["updatedAt"], engine.reconciliation_last_success.isoformat())
        self.assertEqual(data["health"]["state"], "partial")
        self.assertTrue(any(item["id"] == "tracking:stale" for item in data["diagnostics"]))


class HubAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_retains_basic_auth_origin_checks_and_no_store(self):
        engine = snapshot()
        engine.settings.dashboard_password = SecretStr("dashboard-test-password")
        with patch("app.main.engine", engine), patch("app.main.settings", engine.settings):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                self.assertEqual((await client.get("/api/hub/summary")).status_code, 401)
                auth = ("admin", "dashboard-test-password")
                first = await client.get("/api/hub/summary", auth=auth)
                second = await client.get("/api/hub/summary?schemaVersion=2", auth=auth)
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.json(), second.json())
                self.assertEqual(second.headers["cache-control"], "no-store")
                self.assertEqual(second.headers["x-frame-options"], "DENY")
                denied = await client.get("/api/hub/summary", auth=auth, headers={"Origin": "http://foreign.test"})
                self.assertEqual(denied.status_code, 403)

    async def test_no_password_still_rejects_remote_access(self):
        engine = snapshot()
        engine.settings.dashboard_password = SecretStr("")
        with patch("app.main.engine", engine), patch("app.main.settings", engine.settings):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.0.2.1", 1)), base_url="http://localhost") as client:
                self.assertEqual((await client.get("/api/hub/summary")).status_code, 403)
