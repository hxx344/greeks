"""Installer runtime: read dotenv as data, launch one worker, check authenticated health."""
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import sys
import urllib.request

from dotenv import dotenv_values


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward Basic credentials to a redirect target.
        return None


def read_config(path: str | Path) -> dict[str, str]:
    # No shell expansion or ${...} interpolation, including inside credentials.
    values = dotenv_values(path, interpolate=False)
    return {key: value for key, value in values.items() if value is not None}


def validate(values: dict[str, str], data_root: Path = Path("/var/lib/greeks")) -> tuple[str, int, Path]:
    from app.config import Settings

    # Do not allow inherited installer environment to change the configuration.
    original = os.environ.copy()
    try:
        os.environ.clear()
        settings = Settings(_env_file=None, **{key.lower(): value for key, value in values.items()})
    finally:
        os.environ.update(original)
    if len(settings.dashboard_password.get_secret_value()) < 12:
        raise ValueError("DASHBOARD_PASSWORD must contain at least 12 characters")
    if ":" in settings.dashboard_username or not settings.dashboard_username.isascii():
        raise ValueError("DASHBOARD_USERNAME must be ASCII and cannot contain ':'")
    host = values.get("HOST", "127.0.0.1")
    ipaddress.ip_address(host)
    port = int(values.get("PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("PORT must be between 1 and 65535")
    state = Path(settings.state_file)
    root = data_root.resolve()
    if not state.is_absolute() or state.resolve() == root or not state.resolve().is_relative_to(root):
        raise ValueError("STATE_FILE must be an absolute file under /var/lib/greeks")
    if any(part.is_symlink() for part in [state, *state.parents]):
        raise ValueError("STATE_FILE and its parents cannot be symbolic links")
    return host, port, state


def health_payload_valid(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("status") in {"ok", "degraded"}
        and payload.get("environment") in {"dry-run", "testnet", "live"}
        and isinstance(payload.get("reconciliation"), dict)
        and isinstance(payload.get("live_enabled"), bool)
        and isinstance(payload.get("trading_enabled"), bool)
    )


def check_health(values: dict[str, str]) -> bool:
    host = values.get("HOST", "127.0.0.1")
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if ":" in host:
        host = f"[{host}]"
    port = int(values.get("PORT", "8000"))
    token = base64.b64encode(
        f"{values.get('DASHBOARD_USERNAME', 'admin')}:{values.get('DASHBOARD_PASSWORD', '')}".encode()
    ).decode()
    request = urllib.request.Request(
        f"http://{host}:{port}/api/health", headers={"Authorization": f"Basic {token}"}
    )
    # Local checks must not send panel credentials through environment HTTP proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=3) as response:
        if response.status != 200 or response.headers.get_content_type() != "application/json":
            return False
        return health_payload_valid(json.loads(response.read(65537)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["validate", "serve", "health"])
    parser.add_argument("--env", required=True)
    args = parser.parse_args()
    # The script is invoked by absolute path; make the adjacent app importable.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    values = read_config(args.env)
    if args.action == "health":
        try:
            healthy = check_health(values)
        except Exception:
            healthy = False
        raise SystemExit(0 if healthy else 1)
    host, port, state = validate(values)
    if args.action == "validate":
        print(state.parent)
        return
    state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/var/lib/greeks",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        **values,
    }
    # Environment keys in dotenv are application settings, not executable controls.
    for key in list(environment):
        if key.startswith(("PYTHON", "UVICORN", "LD_")):
            del environment[key]
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    os.execve(sys.executable, [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host,
                            "--port", str(port), "--workers", "1", "--proxy-headers",
                            "--forwarded-allow-ips", "127.0.0.1,::1"], environment)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Pydantic errors can include secrets; never print their input values.
        print(f"Greeks configuration/runtime check failed ({type(exc).__name__}); check the configuration file.", file=sys.stderr)
        raise SystemExit(1) from None
