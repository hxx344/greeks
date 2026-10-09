"""Deployment checks that never load app.main or contact an exchange."""
from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("greeks_runtime", ROOT / "deploy" / "runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class InstallerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.values = {
            "DASHBOARD_USERNAME": "admin",
            "DASHBOARD_PASSWORD": "installer-test-password",
            "STATE_FILE": str(self.root / "state.json"),
            "TRADING_MODE": "dry-run",
            "LIVE_TRADING": "false",
            "AUTO_OPEN": "false",
        }

    def test_dotenv_is_literal_and_existing_mode_and_secrets_are_preserved(self):
        config = self.root / "greeks.env"
        content = "TRADING_MODE=live\nLIVE_TRADING=true\nBYBIT_API_SECRET='$(touch unwanted)${PATH}'\n"
        config.write_text(content, encoding="utf-8")
        values = runtime.read_config(config)
        self.assertEqual(values["TRADING_MODE"], "live")
        self.assertEqual(values["LIVE_TRADING"], "true")
        self.assertEqual(values["BYBIT_API_SECRET"], "$(touch unwanted)${PATH}")
        self.assertEqual(config.read_text(encoding="utf-8"), content)

    def test_validation_ignores_inherited_environment(self):
        with patch.dict(os.environ, {"LIVE_TRADING": "invalid", "PORT": "not-a-port"}):
            host, port, state = runtime.validate(self.values, self.root)
            self.assertEqual((host, port), ("127.0.0.1", 8000))
            self.assertEqual(state, self.root / "state.json")
            self.assertEqual(os.environ["LIVE_TRADING"], "invalid")
        self.assertFalse(state.exists())

    def test_config_cannot_place_state_in_a_release_or_parent_directory(self):
        for state in ["data/state.json", str(self.root.parent / "outside.json"), str(self.root)]:
            with self.subTest(state=state), self.assertRaises(ValueError):
                runtime.validate({**self.values, "STATE_FILE": state}, self.root)

    def test_config_requires_password_and_valid_binding(self):
        for changes in [{"DASHBOARD_PASSWORD": ""}, {"PORT": "65536"}, {"PORT": "0"},
                        {"HOST": "127.0.0.1; echo bad"}, {"DASHBOARD_USERNAME": "user:name"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                runtime.validate({**self.values, **changes}, self.root)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation requires extra privileges")
    def test_symlink_state_is_rejected_without_modifying_target(self):
        target = self.root / "real.json"
        target.write_text("existing-state", encoding="utf-8")
        link = self.root / "state.json"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            runtime.validate(self.values, self.root)
        self.assertEqual(target.read_text(), "existing-state")

    def test_degraded_health_is_valid_but_unrelated_200_responses_are_not(self):
        payload = {"status": "degraded", "environment": "dry-run", "reconciliation": {},
                   "live_enabled": False, "trading_enabled": False}
        self.assertTrue(runtime.health_payload_valid(payload))
        for invalid in [None, "ok", {"status": "ok"}, {**payload, "status": "failed"},
                        {**payload, "trading_enabled": "false"}]:
            self.assertFalse(runtime.health_payload_valid(invalid))

    def test_health_uses_basic_api_endpoint_and_bypasses_http_proxy(self):
        payload = {"status": "degraded", "environment": "dry-run", "reconciliation": {},
                   "live_enabled": False, "trading_enabled": False}
        with patch.object(runtime.urllib.request, "build_opener") as build, \
                patch.object(runtime.urllib.request, "ProxyHandler") as proxy:
            response = build.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.headers.get_content_type.return_value = "application/json"
            response.read.return_value = json.dumps(payload).encode()
            self.assertTrue(runtime.check_health({**self.values, "HOST": "0.0.0.0"}))
            proxy.assert_called_once_with({})
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:8000/api/health")
            expected = base64.b64encode(b"admin:installer-test-password").decode()
            self.assertEqual(request.get_header("Authorization"), f"Basic {expected}")
            response.headers.get_content_type.return_value = "text/html"
            self.assertFalse(runtime.check_health(self.values))

    def test_configuration_error_never_prints_secret_values(self):
        config = self.root / "bad.env"
        config.write_text("DASHBOARD_PASSWORD=SECRET\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(ROOT / "deploy" / "runtime.py"),
                                 "validate", "--env", str(config)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("SECRET", result.stderr)

    def test_redirect_never_receives_panel_credentials(self):
        requested_paths = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requested_paths.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/untrusted")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(runtime.urllib.error.HTTPError):
                runtime.check_health({**self.values, "PORT": str(server.server_port)})
            self.assertEqual(requested_paths, ["/api/health"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
