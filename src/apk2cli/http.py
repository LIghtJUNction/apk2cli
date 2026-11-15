"""A same-origin JSON transport that never puts server errors or tokens in logs."""

from __future__ import annotations

import json
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .errors import Apk2CliError
from .spec import validate_base_url, validate_path

MAX_RESPONSE = 8 * 1024 * 1024


def _reject_constant(value: str):
    raise ValueError("Nonfinite JSON number")


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(spec: dict, method: str, path: str, token: str | None = None,
                 params: dict | None = None, body: dict | None = None):
    base = validate_base_url(spec["base_url"])
    validate_path(path)
    url = base + path
    if params:
        url += "?" + urlencode(params, doseq=True)
    headers = {"Accept": "application/json", "User-Agent": "apk2cli/0.1"}
    if token is not None:
        if not isinstance(token, str) or not token or any(ord(c) < 32 or ord(c) > 126 for c in token):
            raise Apk2CliError("invalid_session", "Session value cannot be used as an HTTP credential.")
        auth = spec["auth"]
        headers[auth.get("header", "Authorization")] = auth.get("prefix", "Bearer ") + token
    data = None
    if body is not None:
        try:
            data = json.dumps(body, allow_nan=False).encode("utf-8")
        except (ValueError, TypeError) as exc:
            raise Apk2CliError("invalid_parameters", "Request body must contain valid finite JSON values.") from exc
        headers["Content-Type"] = "application/json"
    try:
        request = Request(url, data=data, headers=headers, method=method)
        with build_opener(_NoRedirects()).open(request, timeout=20) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise Apk2CliError("response_too_large", "API response exceeds the 8 MiB limit.")
            if not raw and (method == "HEAD" or response.status == 204):
                return {"status": response.status}
            try:
                return json.loads(raw, parse_constant=_reject_constant)
            except (UnicodeError, ValueError) as exc:
                raise Apk2CliError("invalid_response", "API did not return a valid JSON response.") from exc
    except HTTPError as exc:
        # Do not print a response body: login services may echo submitted secrets.
        exc.close()
        if exc.code in {401, 403}:
            raise Apk2CliError("session_rejected", "API rejected this session. Sign in on the phone, then run auth --adb again.") from None
        if 300 <= exc.code < 400:
            raise Apk2CliError("redirect_rejected", "API redirected the request. Verify base_url; credentials were not forwarded.") from None
        raise Apk2CliError("http_error", f"API returned HTTP {exc.code}.") from None
    except (URLError, socket.timeout, TimeoutError, OSError, ValueError) as exc:
        raise Apk2CliError("network_error", "Cannot reach the configured API. Check connectivity, TLS and base_url.") from exc
