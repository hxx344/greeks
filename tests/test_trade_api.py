import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.engine import TradingEngine
from app.main import app
from app.models import TradePlanRequest, TradeTaskRequest
from app.strategy import demo_chain
from app.trade_tasks import TradeConflict


class TradeApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = Settings(_env_file=None, state_file=f"{directory.name}/state.json", leg_qty=.01,
                                 max_risk_usd=100000, dashboard_password="test-dashboard-password")
        self.engine = TradingEngine(self.settings)
        self.addAsyncCleanup(self.engine.client.close)
        self.engine.chain = demo_chain(datetime.now(timezone.utc))
        self.engine.chain_source = "bybit"
        self.engine.chain_updated_at = datetime.now(timezone.utc)
        self.engine.btc_price = 100000
        self.engine.refresh_chain = AsyncMock()
        self.engine._validate_open_calendar = lambda expiry: None
        self.engine._capture_pm_baseline = AsyncMock()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                       auth=("admin", "test-dashboard-password"))
        self.addAsyncCleanup(self.client.aclose)
        for target, value in (("app.main.engine", self.engine), ("app.main.settings", self.settings)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def plan(self):
        response = await self.client.post("/api/trading/plans", json={"operation": "open", "quantity": .01})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def test_confirmed_task_is_queryable_by_request_and_repeat_submission_is_idempotent(self):
        plan = await self.plan()
        payload = {"plan_id": plan["plan_id"], "request_id": "stable-request", "confirm_live": False,
                   "min_net_income_usd": plan["min_net_income_usd"]}
        response = await self.client.post("/api/trading/tasks", json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        execution_id = response.json()["execution_id"]
        if self.engine.trade_workers:
            await asyncio.gather(*list(self.engine.trade_workers.values()))
        repeated = await self.client.post("/api/trading/tasks", json=payload)
        self.assertEqual(repeated.status_code, 202)
        self.assertEqual(repeated.json()["execution_id"], execution_id)
        self.assertEqual(len(self.engine.positions), 4)
        conflict = await self.client.post("/api/trading/tasks", json={**payload, "min_net_income_usd": 1})
        self.assertEqual(conflict.status_code, 409)
        query = await self.client.get("/api/trading/tasks", params={"request_id": "stable-request"})
        self.assertEqual(query.status_code, 200)
        self.assertEqual([item["execution_id"] for item in query.json()["items"]], [execution_id])
        detail = await self.client.get(f"/api/trading/tasks/{execution_id}")
        self.assertEqual(detail.json()["execution_status"], "completed")
        self.assertTrue(detail.json()["simulated"])
        self.assertEqual(detail.json()["progress_ratio"], 1)
        self.assertEqual(detail.headers["cache-control"], "no-store")
        unknown = await self.client.get("/api/trading/tasks", params={"request_id": "missing"})
        self.assertEqual(unknown.json()["items"], [])
        self.assertFalse(unknown.json()["admission_closed"])

    async def test_missing_request_is_released_only_after_server_proves_plan_unavailable(self):
        plan = await self.plan()
        params = {"request_id": "never-arrived", "plan_id": plan["plan_id"]}
        response = (await self.client.get("/api/trading/tasks", params=params)).json()
        self.assertEqual(response["request_id"], params["request_id"])
        self.assertEqual(response["plan_id"], params["plan_id"])
        self.assertIsNotNone(datetime.fromisoformat(response["server_time"]).tzinfo)
        self.assertFalse(response["admission_closed"])
        self.engine.trade_plans[plan["plan_id"]]["plan"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.assertTrue((await self.client.get("/api/trading/tasks", params=params)).json()["admission_closed"])
        self.engine.trade_plans.clear()
        self.assertTrue((await self.client.get("/api/trading/tasks", params=params)).json()["admission_closed"])
        self.engine.state_error = "State could not be loaded"
        self.assertFalse((await self.client.get("/api/trading/tasks", params=params)).json()["admission_closed"])

    async def test_recovery_cannot_release_a_request_while_admission_is_in_flight(self):
        plan = await self.plan()
        params = {"request_id": "slow-admission", "plan_id": plan["plan_id"]}
        entered, release = asyncio.Event(), asyncio.Event()
        original_preview = self.engine.make_preview

        async def slow_preview(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_preview(*args, **kwargs)

        with patch.object(self.engine, "make_preview", side_effect=slow_preview):
            submission = asyncio.create_task(self.client.post("/api/trading/tasks", json=params))
            try:
                await asyncio.wait_for(entered.wait(), .5)
                self.engine.trade_plans[plan["plan_id"]]["plan"]["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
                response = await asyncio.wait_for(self.client.get("/api/trading/tasks", params=params), .5)
                self.assertEqual(response.json()["items"], [])
                self.assertTrue(response.json()["admission_active"])
                self.assertFalse(response.json()["admission_closed"])
            finally:
                release.set()
                result = await submission
        self.assertEqual(result.status_code, 409)
        response = (await self.client.get("/api/trading/tasks", params=params)).json()
        self.assertTrue(response["admission_closed"])
        self.assertFalse(self.engine.trade_workers)

    async def test_stop_and_snapshot_do_not_wait_for_the_execution_lock(self):
        plan = await self.plan()
        started, release = asyncio.Event(), asyncio.Event()

        async def worker(group_id):
            async with self.engine.lock:
                self.engine.execution_groups[group_id]["execution_status"] = "running"
                started.set()
                await release.wait()
                self.engine._finish_trade_task(group_id)
            self.engine.trade_workers.pop(group_id, None)

        with patch.object(self.engine, "_run_trade_task", side_effect=worker), patch.object(self.engine, "load_recent_executions", new=AsyncMock()) as fetch:
            accepted = await self.client.post("/api/trading/tasks", json={"plan_id": plan["plan_id"], "request_id": "stop-test"})
            task_id = accepted.json()["execution_id"]
            await asyncio.wait_for(started.wait(), .5)
            workers = list(self.engine.trade_workers.values())
            try:
                snapshot = await asyncio.wait_for(self.client.get("/api/dashboard/orders"), .5)
                self.assertEqual(snapshot.status_code, 200)
                self.assertTrue(snapshot.json()["execution_active"])
                self.assertEqual(snapshot.json()["active_execution_id"], task_id)
                stop = await asyncio.wait_for(self.client.post(f"/api/trading/tasks/{task_id}/stop"), .5)
                self.assertEqual(stop.status_code, 200, stop.text)
                self.assertEqual(stop.json()["execution_status"], "stopping")
                repeat = await self.client.post(f"/api/trading/tasks/{task_id}/stop")
                self.assertEqual(repeat.json()["stop_requested_at"], stop.json()["stop_requested_at"])
                fetch.assert_not_awaited()
            finally:
                release.set()
                await asyncio.gather(*workers)
            self.assertEqual(self.engine.execution_groups[task_id]["execution_status"], "stopped")

    async def test_old_routes_require_plan_binding_and_never_execute(self):
        with patch.object(self.engine, "open_position", new=AsyncMock()) as opening, patch.object(self.engine, "close_position", new=AsyncMock()) as closing:
            for path in ("open", "close"):
                response = await self.client.post(f"/api/trading/{path}", json={"confirm_live": True})
                self.assertEqual(response.status_code, 409)
                self.assertIn("/api/trading/plans", response.json()["detail"])
            opening.assert_not_awaited()
            closing.assert_not_awaited()

    async def test_new_routes_keep_authentication_and_same_origin_checks(self):
        writes = [("/api/trading/plans", {"operation": "open"}),
                  ("/api/trading/tasks", {"plan_id": "p", "request_id": "r"}),
                  ("/api/trading/tasks/task/stop", {})]
        with patch.object(self.engine, "prepare_trade_plan", new=AsyncMock()) as prepare, patch.object(self.engine, "start_trade_task", new=AsyncMock()) as start, patch.object(self.engine, "stop_trade_task") as stop:
            for path, payload in writes:
                denied = await self.client.post(path, json=payload, auth=None)
                self.assertEqual(denied.status_code, 401)
                cross_site = await self.client.post(path, json=payload, headers={"Origin": "https://other.example"})
                self.assertEqual(cross_site.status_code, 403)
            self.assertEqual((await self.client.get("/api/trading/tasks", auth=None)).status_code, 401)
            prepare.assert_not_awaited()
            start.assert_not_awaited()
            stop.assert_not_called()

    async def test_conflicts_and_invalid_limits_are_client_errors(self):
        with patch.object(self.engine, "prepare_trade_plan", new=AsyncMock(side_effect=TradeConflict("busy"))):
            self.assertEqual((await self.client.post("/api/trading/plans", json={"operation": "open"})).status_code, 409)
        for field in ("min_net_income_usd", "max_net_cost_usd"):
            for value in ("NaN", "Infinity", "-Infinity"):
                response = await self.client.post("/api/trading/tasks", json={"plan_id": "p", "request_id": "r", field: value})
                self.assertEqual(response.status_code, 422)
                self.assertNotIn("input", response.json()["detail"][0])
        self.assertEqual((await self.client.get("/api/trading/tasks/missing")).status_code, 404)
        self.assertEqual((await self.client.post("/api/trading/tasks/missing/stop")).status_code, 404)


if __name__ == "__main__":
    unittest.main()
