"""Validate an app's observed HTTP and authentication contract before use."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .errors import Apk2CliError
from .adb import validate_source_path

NAME = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
PARAMETER_NAME = re.compile(r"[a-z][a-z0-9_-]{0,47}\Z")
PACKAGE = re.compile(r"[A-Za-z]\w*(?:\.[A-Za-z]\w*)+\Z", re.ASCII)
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}


def fail(message: str) -> None:
    raise Apk2CliError("invalid_spec", message)


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name) or name == "apk2cli":
        fail("CLI name must start with a lowercase letter and contain up to 48 letters, digits or hyphens; apk2cli is reserved.")
    return name


def validate_base_url(url: str) -> str:
    if not isinstance(url, str):
        fail("base_url must be a string.")
    try:
        p = urlsplit(url)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password or p.query or p.fragment:
            fail("base_url must be an HTTP(S) origin or API prefix without credentials, query or fragment.")
        if any(c in url for c in "\\?#") or any(ord(c) < 33 for c in url):
            fail("base_url contains invalid characters.")
        if any(part in {".", ".."} for part in unquote(p.path).split("/")):
            fail("base_url cannot include path traversal.")
        _ = p.port
        if p.scheme == "http":
            try:
                loopback = ipaddress.ip_address(p.hostname).is_loopback
            except ValueError:
                loopback = p.hostname == "localhost"
            if not loopback:
                fail("Use HTTPS for remote APIs. Plain HTTP is allowed only for a loopback test server.")
    except (ValueError, TypeError) as exc:
        raise Apk2CliError("invalid_spec", "base_url is invalid.") from exc
    return url.rstrip("/")


def validate_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        fail("API paths must begin with one slash and remain on base_url.")
    decoded = unquote(path)
    if any(c in path for c in "?#\\") or any(ord(c) < 32 for c in path) or "\\" in decoded or any(x in {".", ".."} for x in decoded.split("/")):
        fail("API paths cannot include a query, fragment or traversal. Use parameters for query values.")
    return path


def validate_spec(spec: dict) -> dict:
    if not isinstance(spec, dict) or type(spec.get("schema_version")) is not int or spec["schema_version"] != 1:
        fail("Expected an object with schema_version: 1.")
    validate_name(spec.get("name"))
    if not isinstance(spec.get("package"), str) or not PACKAGE.fullmatch(spec["package"]):
        fail("package must be a valid Android application ID.")
    spec["base_url"] = validate_base_url(spec.get("base_url", ""))
    auth = spec.get("auth")
    if not isinstance(auth, dict):
        fail("auth must define sources, a header and a verification endpoint.")
    header = auth.get("header", "Authorization")
    if not isinstance(header, str) or not re.fullmatch(r"[A-Za-z0-9-]+", header) or header.lower() in {"host", "content-length", "connection", "transfer-encoding"}:
        fail("auth.header must be a valid authentication header name.")
    prefix = auth.get("prefix", "Bearer ")
    if not isinstance(prefix, str) or any(ord(c) < 32 or ord(c) > 126 for c in prefix):
        fail("auth.prefix must be printable ASCII.")
    sources = auth.get("sources")
    if not isinstance(sources, list) or not 1 <= len(sources) <= 16:
        fail("auth.sources must identify between 1 and 16 observed session sources.")
    for source in sources:
        if not isinstance(source, dict):
            fail("Each auth source must be an object.")
        path = source.get("path", "")
        if not isinstance(path, str):
            fail("Session source path must be a string.")
        try:
            validate_source_path(path)
        except Apk2CliError as exc:
            fail(exc.message)
        fmt = source.get("format")
        if fmt not in {"shared_prefs", "json", "sqlite"}:
            fail("Session source format must be shared_prefs, json or sqlite.")
        if fmt == "json" and "pointer" in source:
            if not isinstance(source["pointer"], str) or not source["pointer"].startswith("/"):
                fail("JSON source pointer must be an RFC 6901 JSON pointer.")
        elif not isinstance(source.get("key"), str) or not source["key"]:
            fail("Session source must name an exact key.")
        if fmt == "sqlite" and any(not isinstance(source.get(k), str) or not IDENTIFIER.fullmatch(source[k]) for k in ("table", "key_column", "value_column")):
            fail("SQLite source must name a table, key_column and value_column using simple identifiers.")
    verify = auth.get("verify")
    if not isinstance(verify, dict) or verify.get("method", "GET") != "GET":
        fail("auth.verify must define a read-only GET identity endpoint.")
    validate_path(verify.get("path"))
    if "{" in verify["path"] or "}" in verify["path"]:
        fail("Authentication verification cannot depend on command parameters.")
    if not isinstance(verify.get("identity_pointer"), str) or not verify["identity_pointer"].startswith("/"):
        fail("auth.verify.identity_pointer must identify the signed-in account in the response.")
    commands = spec.get("commands")
    if not isinstance(commands, list) or not commands:
        fail("commands must contain at least one observed API command.")
    used = set()
    for command in commands:
        if not isinstance(command, dict):
            fail("Each command must be an object.")
        name = command.get("name")
        validate_name(name)
        if name in used or name in {"auth", "help", "doctor"}:
            fail("Command names must be unique and cannot use auth, help or doctor.")
        used.add(name)
        if command.get("method") not in METHODS:
            fail("Command method is not supported.")
        validate_path(command.get("path"))
        params = command.get("parameters", [])
        if not isinstance(params, list):
            fail("parameters must be an array.")
        names = set()
        path_params = set()
        for p in params:
            if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not PARAMETER_NAME.fullmatch(p["name"]):
                fail("Parameter names must use lowercase letters, digits, underscores and hyphens.")
            if p["name"] in names or p["name"] in {"state-dir", "help", "version"}:
                fail("Parameter names must be unique and cannot shadow runtime options.")
            names.add(p["name"])
            if p.get("in") not in {"path", "query", "body"} or p.get("type", "string") not in {"string", "integer", "number", "boolean"}:
                fail("Parameter location/type is not supported.")
            if "required" in p and type(p["required"]) is not bool:
                fail("Parameter required must be a boolean.")
            if "key" in p and (not isinstance(p["key"], str) or not p["key"] or len(p["key"]) > 128 or any(ord(c) < 32 for c in p["key"]) or p["in"] == "path"):
                fail("Parameter key must be a nonempty API field name for a query or body parameter.")
            if "default" in p:
                value, kind = p["default"], p.get("type", "string")
                allowed = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,)}[kind]
                if type(value) not in allowed or isinstance(value, float) and not math.isfinite(value):
                    fail("Parameter default must match its declared type and be finite.")
            if p["in"] == "path":
                path_params.add(p["name"])
            if p["in"] == "body" and command["method"] in {"GET", "HEAD"}:
                fail("GET and HEAD commands cannot have body parameters.")
        placeholders = set(re.findall(r"\{([^{}]+)\}", command["path"]))
        if placeholders != path_params or re.sub(r"\{[^{}]+\}", "", command["path"]).count("{") or re.sub(r"\{[^{}]+\}", "", command["path"]).count("}"):
            fail("Every path placeholder must have an exact matching path parameter.")
    return spec


def load_spec(path: Path | str) -> dict:
    try:
        path = Path(path)
        if path.stat().st_size > 1024 * 1024:
            fail("Specification exceeds the 1 MiB limit.")
        return validate_spec(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Apk2CliError("invalid_spec", "Cannot read a valid JSON app specification.") from exc
