import base64
import binascii
import ipaddress
import secrets
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


def origin(url):
    parsed = urlsplit(str(url))
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def authorize_dashboard(request, settings):
    password = settings.dashboard_password.get_secret_value()
    if password:
        try:
            scheme, value = request.headers.get("authorization", "").split(" ", 1)
            if scheme.lower() != "basic":
                raise ValueError("Unsupported authentication scheme")
            username, supplied = base64.b64decode(value, validate=True).decode("utf-8").split(":", 1)
            valid_user = secrets.compare_digest(username.encode(), settings.dashboard_username.encode())
            valid_password = secrets.compare_digest(supplied.encode(), password.encode())
            valid = valid_user and valid_password
        except (ValueError, UnicodeError, binascii.Error):
            valid = False
        if not valid:
            return JSONResponse({"detail": "Dashboard authentication required"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="Trading dashboard", charset="UTF-8"'})
    else:
        try:
            local_client = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
            local_host = request.url.hostname in {"localhost", "127.0.0.1", "::1"}
        except ValueError:
            local_client = local_host = False
        if not local_client or not local_host:
            return JSONResponse({"detail": "Remote access requires a configured dashboard password"}, status_code=403)
    if request.url.path.startswith("/api/") or request.method not in {"GET", "HEAD", "OPTIONS"}:
        site = request.headers.get("sec-fetch-site")
        source = request.headers.get("origin") or request.headers.get("referer")
        try:
            cross_origin = source is not None and origin(source) != origin(request.url)
        except ValueError:
            cross_origin = True
        if site == "cross-site" or cross_origin:
            return JSONResponse({"detail": "Cross-origin API requests are not allowed"}, status_code=403)
    return None
