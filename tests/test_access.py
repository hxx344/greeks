import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import SecretStr, ValidationError

from app.cache import SnapshotCache
from app.config import Settings
from app.main import app


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, path="/api/health", *, host="localhost", peer="127.0.0.1", method="GET", **kwargs):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 40000)), base_url=f"http://{host}") as client:
            return await client.request(method, path, **kwargs)

    async def test_local_access_works_and_remote_without_password_is_blocked(self):
        with patch("app.main.settings.dashboard_password", SecretStr("")):
            self.assertEqual((await self.request()).status_code, 200)
            self.assertEqual((await self.request(peer="198.51.100.1")).status_code, 403)
            self.assertEqual((await self.request(peer="198.51.100.1", headers={"X-Forwarded-For": "127.0.0.1"})).status_code, 403)

    async def test_untrusted_host_cannot_rebind_local_dashboard(self):
        with patch("app.main.settings.dashboard_password", SecretStr("")):
            self.assertEqual((await self.request("/", host="evil.example")).status_code, 403)

    async def test_ipv6_loopback(self):
        with patch("app.main.settings.dashboard_password", SecretStr("")):
            self.assertEqual((await self.request(host="[::1]", peer="::1")).status_code, 200)

    async def test_password_protects_reads_and_writes(self):
        password = "test-password-123"
        with patch("app.main.settings.dashboard_password", SecretStr(password)), patch("app.main.engine.open_position", new=AsyncMock()) as open_order:
            self.assertEqual((await self.request()).status_code, 401)
            response = await self.request("/api/trading/open", method="POST", json={"confirm_live": True})
            self.assertEqual(response.status_code, 401)
            open_order.assert_not_awaited()
            authenticated = await self.request("/api/config", host="dashboard.example", peer="198.51.100.1", auth=("admin", password))
            self.assertEqual(authenticated.status_code, 200)
            self.assertNotIn(password, authenticated.text)
            self.assertEqual(authenticated.headers["cache-control"], "no-store")

    async def test_malformed_or_wrong_credentials_get_a_challenge(self):
        with patch("app.main.settings.dashboard_password", SecretStr("test-password-123")):
            for header in ("Basic !!!", "Basic /w==", "Bearer value", "Basic YWRtaW46d3Jvbmc="):
                response = await self.request(headers={"Authorization": header})
                self.assertEqual(response.status_code, 401)
                self.assertIn("Basic", response.headers["www-authenticate"])

    async def test_cross_site_requests_cannot_trigger_api_side_effects(self):
        with patch("app.main.engine.open_position", new=AsyncMock()) as open_order, patch("app.main.engine.load_positions", new=AsyncMock()) as positions:
            for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}, {"Referer": "https://evil.example/page"}):
                self.assertEqual((await self.request("/api/trading/open", method="POST", json={}, headers=headers)).status_code, 403)
                self.assertEqual((await self.request("/api/positions", headers=headers)).status_code, 403)
            open_order.assert_not_awaited()
            positions.assert_not_awaited()
            self.assertEqual((await self.request(headers={"Origin": "http://localhost:80"})).status_code, 200)

    async def test_raw_nonfinite_json_returns_validation_error_without_echo(self):
        for constant in ("NaN", "Infinity", "-Infinity"):
            response = await self.request("/api/trading/open", method="POST", content='{"quantity":' + constant + '}', headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("input", response.json()["detail"][0])

    async def test_upstream_failure_returns_502_without_internal_details(self):
        with patch("app.main.engine.client.rfq_config", new=AsyncMock(side_effect=httpx.ReadTimeout("sensitive-internal-detail"))):
            response = await self.request("/api/rfq/config")
            self.assertEqual(response.status_code, 502)
            self.assertNotIn("sensitive-internal-detail", response.text)

    async def test_bad_rfq_state_is_a_client_error(self):
        with patch("app.main.engine.refresh_rfq", new=AsyncMock(side_effect=ValueError("State file unavailable"))):
            self.assertEqual((await self.request("/api/rfq/status")).status_code, 400)

    async def test_dashboard_coalesces_reads_and_invalidates_after_write(self):
        async def build():
            await asyncio.sleep(0.01)
            return {"health": {"available": True}}
        with patch("app.main.account_cache", SnapshotCache(2)), patch("app.main._build_account_dashboard", new=AsyncMock(side_effect=build)) as builder, patch("app.main.engine.refresh_chain", new=AsyncMock(return_value=[])):
            responses = await asyncio.gather(*(self.request("/api/dashboard/account") for _ in range(12)))
            self.assertTrue(all(response.status_code == 200 for response in responses))
            builder.assert_awaited_once()
            await self.request("/api/market/refresh", method="POST")
            await self.request("/api/dashboard/account")
            self.assertEqual(builder.await_count, 2)

    async def test_short_password_is_rejected(self):
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, dashboard_password="short")

    async def test_dashboard_marks_failed_position_refresh_unavailable(self):
        from app.main import _build_account_dashboard
        from app.models import AccountHealth

        with patch("app.main.engine.load_positions", new=AsyncMock(side_effect=RuntimeError("offline"))), patch("app.main.engine.load_account_health", new=AsyncMock(return_value=AccountHealth())), patch("app.main.engine.load_recent_executions", new=AsyncMock(return_value=[])), patch("app.main.engine.positions", []):
            payload = await _build_account_dashboard()
            self.assertFalse(payload["positions"]["available"])
        with patch("app.main.engine.load_positions", new=AsyncMock(return_value=[])), patch("app.main.engine.load_account_health", new=AsyncMock(return_value=AccountHealth())), patch("app.main.engine.load_recent_executions", new=AsyncMock(return_value=[])):
            payload = await _build_account_dashboard()
            self.assertTrue(payload["positions"]["available"])


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_payloads_are_isolated_and_expire(self):
        cache = SnapshotCache(2)
        build = AsyncMock(return_value={"items": []})
        with patch("app.cache.monotonic", return_value=10):
            value = await cache.get(build)
            value["items"].append("mutated")
            self.assertEqual(await cache.get(build), {"items": []})
            build.assert_awaited_once()
        with patch("app.cache.monotonic", return_value=13):
            await cache.get(build)
            self.assertEqual(build.await_count, 2)

    async def test_failed_builder_does_not_poison_cache(self):
        cache = SnapshotCache(2)
        build = AsyncMock(side_effect=[RuntimeError("offline"), {"ok": True}])
        with self.assertRaises(RuntimeError):
            await cache.get(build)
        self.assertEqual(await cache.get(build), {"ok": True})

    async def test_invalidation_during_refresh_prevents_stale_cache(self):
        cache = SnapshotCache(2)
        started, resume = asyncio.Event(), asyncio.Event()
        async def old_snapshot():
            started.set()
            await resume.wait()
            return {"version": 1}
        task = asyncio.create_task(cache.get(old_snapshot))
        await started.wait()
        cache.invalidate()
        resume.set()
        await task
        self.assertEqual(await cache.get(AsyncMock(return_value={"version": 2})), {"version": 2})


if __name__ == "__main__":
    unittest.main()
