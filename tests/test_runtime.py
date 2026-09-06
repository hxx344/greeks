import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.lease import StateLease
from app.main import app, lifespan


class LeaseTests(unittest.TestCase):
    def test_second_process_is_excluded_until_owner_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = str(Path(directory) / "state.json")
            code = "from app.lease import StateLease; import sys\nwith StateLease(sys.argv[1]): pass"
            with StateLease(state_file):
                child = subprocess.run([sys.executable, "-c", code, state_file], capture_output=True, text=True, timeout=10)
                self.assertNotEqual(child.returncode, 0)
                self.assertIn("already in use", child.stderr)
            child = subprocess.run([sys.executable, "-c", code, state_file], capture_output=True, text=True, timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertTrue(Path(state_file + ".lock").exists())

    def test_path_alias_cannot_bypass_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = str(Path(directory) / "state.json")
            with StateLease(state_file):
                with self.assertRaises(RuntimeError):
                    with StateLease(str(Path(directory) / "." / "state.json")):
                        self.fail("Second lease acquired")


class LifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_failure_closes_client_and_releases_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = str(Path(directory) / "state.json")
            with patch("app.main.settings.state_file", state_file), patch("app.main.engine._load_state"), patch("app.main.engine.refresh_chain", new=AsyncMock(side_effect=RuntimeError("startup failed"))), patch("app.main.engine.client.close", new=AsyncMock()) as close:
                with self.assertRaisesRegex(RuntimeError, "startup failed"):
                    async with lifespan(app):
                        self.fail("Startup unexpectedly succeeded")
                close.assert_awaited_once()
            with StateLease(state_file):
                pass


if __name__ == "__main__":
    unittest.main()
