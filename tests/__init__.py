"""Run with `python -m unittest discover -s tests -t .` for network isolation."""

from unittest.mock import patch

# Exchange calls must use MockTransport or fake clients. Even a missing mock
# cannot reach an account when these tests run on a developer machine.
_network_guard = patch("httpx.AsyncHTTPTransport.handle_async_request", side_effect=AssertionError("Real network access is forbidden in tests"))
_network_guard.start()


def tearDownModule():
    _network_guard.stop()
