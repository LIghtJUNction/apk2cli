"""Import one verified app session without exposing its credential in output."""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path
from typing import Any

from .adb import Adb, MAX_SOURCE_BYTES, validate_source_path
from .errors import Apk2CliError
from .http import request_json
from .session import SessionStore, now_iso, parse_expiry, require_live


def json_pointer(document: Any, pointer: str) -> Any:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise Apk2CliError("invalid_pointer", "A nonempty JSON pointer beginning with / is required.")
    value = document
    try:
        for item in pointer[1:].split("/"):
            if re.search(r"~(?![01])", item):
                raise ValueError("Invalid escape")
            item = item.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", item):
                    raise ValueError("Invalid index")
                value = value[int(item)]
            elif isinstance(value, dict):
                value = value[item]
            else:
                raise ValueError("Non-container")
        return value
    except (KeyError, IndexError, ValueError) as exc:
        raise Apk2CliError("field_missing", "The configured field is missing from the source.") from exc


def _sqlite_value(source: dict[str, Any], data: bytes) -> Any:
    fields = [source.get(key) for key in ("table", "key_column", "value_column")]
    if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) for value in fields):
        raise Apk2CliError("invalid_source", "SQLite sources require simple table, key_column, and value_column identifiers.")
    if not isinstance(source.get("key"), str):
        raise Apk2CliError("invalid_source", "SQLite sources require a string key.")
    if not data.startswith(b"SQLite format 3\0"):
        raise Apk2CliError("invalid_source_data", "The configured source is not a SQLite database.")
    table, key_column, value_column = fields
    with tempfile.TemporaryDirectory(prefix="apk2cli-sqlite-") as directory:
        file = Path(directory) / "session.db"
        file.write_bytes(data)
        file.chmod(0o600)
        try:
            with closing(sqlite3.connect(f"{file.as_uri()}?mode=ro&immutable=1", uri=True)) as connection:
                definition = connection.execute(
                    "SELECT type, sql FROM sqlite_master WHERE name = ?", (table,)
                ).fetchone()
                if not definition or definition[0] != "table" or not definition[1] or "VIRTUAL TABLE" in definition[1].upper():
                    raise Apk2CliError("invalid_source_data", "The configured SQLite source must be an ordinary table.")
                columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
                if key_column not in columns or value_column not in columns:
                    raise Apk2CliError("field_missing", "The configured SQLite credential column is missing.")
                rows = connection.execute(
                    f'SELECT "{value_column}" FROM "{table}" WHERE "{key_column}" = ? LIMIT 2',
                    (source["key"],),
                ).fetchall()
        except sqlite3.Error as exc:
            raise Apk2CliError("invalid_source_data", "The configured credential could not be read from the database.") from exc
    if len(rows) != 1:
        raise Apk2CliError("field_missing", "The SQLite credential key must identify exactly one row.")
    return rows[0][0]


def extract_token(source: dict[str, Any], data: bytes) -> str:
    if len(data) > MAX_SOURCE_BYTES:
        raise Apk2CliError("source_too_large", "Credential source exceeds the 4 MiB limit.")
    format_name = source.get("format")
    try:
        if format_name == "shared_prefs":
            if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                raise Apk2CliError("invalid_source_data", "SharedPreferences must not contain XML declarations of entities.")
            document = ET.fromstring(data)
            if document.tag != "map":
                raise Apk2CliError("invalid_source_data", "The source is not Android SharedPreferences XML.")
            matches = [node for node in document if node.tag == "string" and node.get("name") == source.get("key")]
            if len(matches) != 1:
                raise Apk2CliError("field_missing", "The configured SharedPreferences credential key must identify one string.")
            token = matches[0].text
        elif format_name == "json":
            document = json.loads(data)
            if "pointer" in source:
                token = json_pointer(document, source["pointer"])
            elif isinstance(document, dict) and isinstance(source.get("key"), str) and source["key"] in document:
                token = document[source["key"]]
            else:
                raise Apk2CliError("field_missing", "The configured JSON credential key is missing.")
        elif format_name == "sqlite":
            token = _sqlite_value(source, data)
        else:
            raise Apk2CliError("invalid_source", "Supported credential source formats are shared_prefs, json, and sqlite.")
    except (UnicodeError, ValueError, ET.ParseError) as exc:
        raise Apk2CliError("invalid_source_data", "The configured credential source could not be parsed.") from exc
    if not isinstance(token, str) or not token.strip() or len(token) > 16384 or any(c in token for c in "\r\n\0"):
        raise Apk2CliError("invalid_credential", "The configured source does not contain a valid string credential.")
    return token


def _verify(spec: dict[str, Any], token: str) -> tuple[str | int, float | None]:
    verification = spec["auth"]["verify"]
    if verification.get("method", "GET") != "GET":
        raise Apk2CliError("invalid_auth", "Session verification must use a read-only GET endpoint.")
    response = request_json(spec, "GET", verification["path"], token=token)
    identity = json_pointer(response, verification["identity_pointer"])
    if isinstance(identity, bool) or not isinstance(identity, (str, int)) or identity == "" or identity == token:
        raise Apk2CliError("identity_missing", "Session verification did not return a usable account identity.")
    if len(str(identity)) > 512:
        raise Apk2CliError("identity_missing", "The verified account identity exceeds the supported length.")
    expires_at = None
    if verification.get("expires_at_pointer"):
        expires_at = parse_expiry(json_pointer(response, verification["expires_at_pointer"]))
        require_live({"expires_at": expires_at})
    return identity, expires_at


def _public(record: dict[str, Any]) -> dict[str, Any]:
    keys = ("name", "package", "base_url", "identity", "imported_at", "expires_at", "source")
    return {"authenticated": True, **{key: record[key] for key in keys if key in record}}


def authenticate(
    spec: dict[str, Any], *, adb_path: str = "adb", serial: str | None = None,
    root: bool = False, state_dir: str | Path | None = None,
) -> dict[str, Any]:
    sources = spec.get("auth", {}).get("sources")
    if not isinstance(sources, list) or not sources:
        raise Apk2CliError("auth_not_configured", "The app adapter needs an observed credential source and a verification endpoint.")
    store = SessionStore(spec, state_dir)
    adb = Adb(adb_path, serial=serial)
    device = adb.select()
    last_error: Apk2CliError | None = None
    for source in sources:
        try:
            validate_source_path(source["path"])
            token = extract_token(source, adb.read_file(spec["package"], source["path"], root=root))
            identity, expires_at = _verify(spec, token)
        except Apk2CliError as exc:
            last_error = exc
            continue
        record = {
            "schema_version": 1,
            **{key: spec[key] for key in ("name", "package", "base_url")},
            "token": token,
            "identity": identity,
            "imported_at": now_iso(),
            "source": {
                "transport": "adb", "mode": "root" if root else "run-as", "serial": device.serial,
                "path": source["path"], "format": source["format"],
            },
        }
        if expires_at is not None:
            record["expires_at"] = expires_at
        store.save(record)
        return _public(record)
    if last_error is not None:
        raise last_error
    raise Apk2CliError("auth_failed", "No configured application source yielded a verified session.")


def get_token(spec: dict[str, Any], *, state_dir: str | Path | None = None) -> str:
    record = SessionStore(spec, state_dir).load()
    assert record is not None
    return record["token"]


def status(spec: dict[str, Any], *, state_dir: str | Path | None = None, verify: bool = False) -> dict[str, Any]:
    try:
        record = SessionStore(spec, state_dir).load(required=False)
    except Apk2CliError as exc:
        if exc.code == "session_expired":
            return {"authenticated": False, "name": spec["name"], "package": spec["package"], "reason": exc.code}
        raise
    if record is None:
        return {"authenticated": False, "name": spec["name"], "package": spec["package"]}
    if verify:
        identity, expires_at = _verify(spec, record["token"])
        record["identity"] = identity
        if expires_at is not None:
            record["expires_at"] = expires_at
        SessionStore(spec, state_dir).save(record)
    return _public(record)


def logout(spec: dict[str, Any], *, state_dir: str | Path | None = None) -> dict[str, Any]:
    removed = SessionStore(spec, state_dir).delete()
    return {"authenticated": False, "name": spec["name"], "package": spec["package"], "removed": removed}
