import asyncio
import hashlib
import hmac
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

from app.bybit import BybitClient
from app.config import Settings
from app.engine import TradingEngine
from app.main import app
from app.models import ExecutionRecord, OptionInstrument, Position
from app.strategy import build_iron_condor, choose_expiry, demo_chain


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeAsyncClient:
    last_request = None
    created = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.is_closed = False
        FakeAsyncClient.created += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def request(self, method, url, **kwargs):
        FakeAsyncClient.last_request = (method, url, kwargs)
        return FakeResponse({"retCode": 0, "result": {"ok": True}})

    async def aclose(self):
        self.is_closed = True


class CoreTests(unittest.TestCase):
    def test_strategy_builds_four_legs(self):
        now = datetime.now(timezone.utc)
        preview = build_iron_condor(demo_chain(now), now, target_dte=2, qty=0.01)
        self.assertEqual(len(preview.legs), 4)
        self.assertEqual({leg.side for leg in preview.legs}, {"Buy", "Sell"})
        self.assertEqual({leg.target_delta for leg in preview.legs if leg.side == "Buy"}, {0.10})
        self.assertEqual(preview.expiry.weekday(), 6)

    def test_expiry_selection_uses_next_sunday_not_rolling_dte(self):
        now = datetime(2026, 9, 2, 21, tzinfo=timezone.utc)
        saturday = datetime(2026, 9, 5, 8, tzinfo=timezone.utc)
        sunday = datetime(2026, 9, 6, 8, tzinfo=timezone.utc)
        options = [OptionInstrument(symbol="SAT", expiry=saturday, strike=100, option_type="Call", delta=0.1), OptionInstrument(symbol="SUN", expiry=sunday, strike=100, option_type="Call", delta=0.1)]
        self.assertEqual(choose_expiry(options, now, 2), sunday)

    def test_live_entry_calendar_requires_friday_and_sunday(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = TradingEngine(Settings(_env_file=None, state_file=f"{directory}/state.json"))
            friday = datetime(2026, 9, 4, 21, tzinfo=timezone.utc)
            sunday = datetime(2026, 9, 6, 8, tzinfo=timezone.utc)
            engine._validate_open_calendar(sunday, friday)
            with self.assertRaisesRegex(ValueError, "Sunday"):
                engine._validate_open_calendar(datetime(2026, 9, 5, 8, tzinfo=timezone.utc), friday)
            with self.assertRaisesRegex(ValueError, "Friday"):
                engine._validate_open_calendar(sunday, datetime(2026, 9, 5, 1, tzinfo=timezone.utc))

    def test_strategy_max_loss_uses_each_wing_width(self):
        now = datetime.now(timezone.utc)
        preview = build_iron_condor(demo_chain(now), now, target_dte=2, qty=0.01)
        short_call = next(leg for leg in preview.legs if leg.option_type == "Call" and leg.side == "Sell")
        long_call = next(leg for leg in preview.legs if leg.option_type == "Call" and leg.side == "Buy")
        short_put = next(leg for leg in preview.legs if leg.option_type == "Put" and leg.side == "Sell")
        long_put = next(leg for leg in preview.legs if leg.option_type == "Put" and leg.side == "Buy")
        width = max(long_call.strike - short_call.strike, short_put.strike - long_put.strike)
        expected = round(max(0.0, width * 0.01 - preview.net_credit_usd), 2)
        self.assertEqual(preview.max_loss_usd, expected)

    def test_market_fallback_is_disabled_by_default(self):
        settings = Settings(_env_file=None)
        self.assertFalse(settings.allow_market_fallback)

    def test_portfolio_margin_metrics_are_parsed(self):
        payload = {"wallet": {"accountIM": "120.5", "accountMM": "90.25"}, "assetPnlRange": [{"baseCoin": "BTC", "asset": {"assetIM": "110", "assetMM": "80"}, "contingency": {"contingencyComponents": "7.5"}, "maxLossPriceMove": "-0.1", "maxLossIvShock": "0.2"}]}
        metrics = TradingEngine._portfolio_margin_metrics(payload)
        self.assertEqual(metrics["account_im"], 120.5)
        self.assertEqual(metrics["asset_mm"], 80.0)
        self.assertEqual(metrics["contingency"], 7.5)
        self.assertEqual(metrics["max_loss_price_move"], -0.1)

    def test_account_health_reports_real_pm_increment(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(_env_file=None, bybit_api_key="key", bybit_api_secret="secret", state_file=f"{directory}/state.json")
            engine = TradingEngine(settings)
            engine.pm_baseline = {"account_im": 100.0, "account_mm": 75.0, "captured_at": "2026-09-05T00:00:00+00:00", "context": "manual_open"}
            engine.client.account_info = AsyncMock(return_value={"marginMode": "PORTFOLIO_MARGIN"})
            engine.client.wallet_balance = AsyncMock(return_value={"totalAvailableBalance": "900", "totalMarginBalance": "1000", "totalEquity": "1100", "totalWalletBalance": "1000", "totalInitialMargin": "120", "totalMaintenanceMargin": "90", "accountIMRate": "0.12", "accountMMRate": "0.09"})
            engine.client.portfolio_margin = AsyncMock(return_value={"wallet": {"accountIM": "125", "accountMM": "92"}, "assetPnlRange": [{"baseCoin": "BTC", "asset": {"assetIM": "115", "assetMM": "82"}, "contingency": {"contingencyComponents": "8"}, "maxLossPriceMove": "-0.1", "maxLossIvShock": "-0.2"}]})
            health = asyncio.run(engine.load_account_health())
            self.assertTrue(health.portfolio_margin_available)
            self.assertEqual(health.pm_incremental_initial_margin_usd, 25.0)
            self.assertEqual(health.pm_incremental_maintenance_margin_usd, 17.0)
            self.assertEqual(health.pm_asset_initial_margin_usd, 115.0)

    def test_executed_rfq_positions_restore_strategy_tracking(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(_env_file=None, state_file=f"{directory}/state.json")
            engine = TradingEngine(settings)
            legs = [
                {"symbol": "CALL-SHORT", "side": "Sell", "qty": "0.01"},
                {"symbol": "PUT-SHORT", "side": "Sell", "qty": "0.01"},
                {"symbol": "CALL-LONG", "side": "Buy", "qty": "0.01"},
                {"symbol": "PUT-LONG", "side": "Buy", "qty": "0.01"},
            ]
            engine.rfq_state = {"status": "PendingFill", "selected_quote_id": "quote-1", "legs": legs}
            positions = [Position(symbol=leg["symbol"], side=leg["side"], size=0.01, avg_price=1, mark_price=1, unrealised_pnl=0) for leg in legs]
            self.assertTrue(engine._track_filled_rfq(positions))
            self.assertEqual(engine.active_strategy_symbols, {leg["symbol"] for leg in legs})
            self.assertEqual(engine.active_strategy_sizes["CALL-SHORT|Sell"], 0.01)

    def test_rfq_tracking_rejects_incomplete_position_set(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(_env_file=None, state_file=f"{directory}/state.json")
            engine = TradingEngine(settings)
            legs = [
                {"symbol": "CALL-SHORT", "side": "Sell", "qty": "0.01"},
                {"symbol": "PUT-SHORT", "side": "Sell", "qty": "0.01"},
                {"symbol": "CALL-LONG", "side": "Buy", "qty": "0.01"},
                {"symbol": "PUT-LONG", "side": "Buy", "qty": "0.01"},
            ]
            engine.rfq_state = {"status": "PendingFill", "selected_quote_id": "quote-1", "legs": legs}
            positions = [Position(symbol=leg["symbol"], side=leg["side"], size=0.01, avg_price=1, mark_price=1, unrealised_pnl=0) for leg in legs[:3]]
            self.assertFalse(engine._track_filled_rfq(positions))
            self.assertFalse(engine.active_strategy_symbols)

    def test_manual_open_task_positions_restore_strategy_tracking(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(_env_file=None, state_file=f"{directory}/state.json")
            engine = TradingEngine(settings)
            legs = {
                "CALL-SHORT": {"side": "Sell", "qty": 0.01},
                "PUT-SHORT": {"side": "Sell", "qty": 0.01},
                "CALL-LONG": {"side": "Buy", "qty": 0.01},
                "PUT-LONG": {"side": "Buy", "qty": 0.01},
            }
            engine.execution_groups = {"group1": {"type": "open", "created_at": "2026-01-01T00:00:00Z", "legs": legs}}
            positions = [Position(symbol=symbol, side=leg["side"], size=0.01, avg_price=1, mark_price=1, unrealised_pnl=0) for symbol, leg in legs.items()]
            self.assertTrue(engine._recover_tracked_open_positions(positions))
            self.assertEqual(engine.active_strategy_group_id, "group1")
            self.assertEqual(len(engine.active_strategy_symbols), 4)

    def test_legacy_manual_executions_restore_strategy_tracking(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(_env_file=None, state_file=f"{directory}/state.json")
            engine = TradingEngine(settings)
            legs = [("CALL-SHORT", "Sell"), ("PUT-SHORT", "Sell"), ("CALL-LONG", "Buy"), ("PUT-LONG", "Buy")]
            now = datetime.now(timezone.utc)
            engine.last_executions = [ExecutionRecord(symbol=symbol, side=side, order_id=str(index), order_link_id=f"ic-legacy-{index}", exec_id=str(index), exec_fee=0, fee_currency="USDT", exec_price=1, exec_qty=0.01, exec_time=now) for index, (symbol, side) in enumerate(legs)]
            positions = [Position(symbol=symbol, side=side, size=0.01, avg_price=1, mark_price=1, unrealised_pnl=0) for symbol, side in legs]
            self.assertTrue(engine._recover_tracked_open_positions(positions))
            self.assertEqual(engine.active_strategy_group_id, "legacy")

    def test_get_signature_matches_wire_query_order(self):
        client = BybitClient("key", "secret", testnet=False, recv_window=5000)
        with patch("app.bybit.httpx.AsyncClient", FakeAsyncClient), patch("app.bybit.time.time", return_value=1700000000):
            asyncio.run(client._request("GET", "/private", {"category": "option", "baseCoin": "BTC"}, private=True))
        _, _, kwargs = FakeAsyncClient.last_request
        query = "category=option&baseCoin=BTC"
        expected = hmac.new(b"secret", ("1700000000000key5000" + query).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(kwargs["params"], {"category": "option", "baseCoin": "BTC"})
        self.assertEqual(kwargs["headers"]["X-BAPI-SIGN"], expected)

    def test_post_signature_uses_exact_compact_body(self):
        client = BybitClient("key", "secret", testnet=False, recv_window=5000)
        body = {"category": "option", "symbol": "BTC-TEST", "qty": "1"}
        compact = json.dumps(body, separators=(",", ":"))
        with patch("app.bybit.httpx.AsyncClient", FakeAsyncClient), patch("app.bybit.time.time", return_value=1700000000):
            asyncio.run(client._request("POST", "/private", body=body, private=True))
        _, _, kwargs = FakeAsyncClient.last_request
        expected = hmac.new(b"secret", ("1700000000000key5000" + compact).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(kwargs["content"], compact)
        self.assertEqual(kwargs["headers"]["X-BAPI-SIGN"], expected)

    def test_bybit_client_reuses_http_connection_pool(self):
        client = BybitClient()
        FakeAsyncClient.created = 0

        async def call_twice():
            await client._request("GET", "/one")
            await client._request("GET", "/two")
            await client.close()

        with patch("app.bybit.httpx.AsyncClient", FakeAsyncClient):
            asyncio.run(call_twice())
        self.assertEqual(FakeAsyncClient.created, 1)

    def test_market_dashboard_returns_selected_expiry_only(self):
        now = datetime.now(timezone.utc)
        options = demo_chain(now)
        preview = build_iron_condor(options, now, target_dte=2, qty=0.01)

        async def call():
            transport = httpx.ASGITransport(app=app)
            with patch("app.main.engine.make_preview", new=AsyncMock(return_value=preview)), patch("app.main.engine.chain", options), patch("app.main.engine.chain_source", "bybit"), patch("app.main.engine.chain_updated_at", now), patch("app.main.engine.btc_price", 100000):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    return await client.get("/api/dashboard/market?quantity=0.01")

        response = asyncio.run(call())
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["chain"]["items"])
        self.assertTrue(all(item["expiry"] == payload["preview"]["expiry"] for item in payload["chain"]["items"]))

    def test_health_endpoint_is_local_system_smoke_test(self):
        async def call():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.get("/api/health")

        response = asyncio.run(call())
        self.assertEqual(response.status_code, 200)
        self.assertIn("live_enabled", response.json())


if __name__ == "__main__":
    unittest.main()
