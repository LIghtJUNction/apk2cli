"""Authentication boundaries; Android emulator coverage lives in the E2E script."""

from __future__ import annotations

import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from apk2cli.adb import Adb, Device, MAX_SOURCE_BYTES, validate_source_path
from apk2cli.auth import authenticate, extract_token, get_token, json_pointer, logout, status
from apk2cli.errors import Apk2CliError
from apk2cli.session import SessionStore, parse_expiry


def spec():
    return {
        "schema_version": 1, "name": "demo", "package": "dev.apk2cli.demo",
        "base_url": "http://127.0.0.1:8765",
        "auth": {
            "sources": [{"path": "shared_prefs/session.xml", "format": "shared_prefs", "key": "access_token"}],
            "header": "Authorization", "prefix": "Bearer ",
            "verify": {"method": "GET", "path": "/me", "identity_pointer": "/username"},
        },
        "commands": [{"name": "me", "method": "GET", "path": "/me", "help": "Show account"}],
    }


class AdbTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def result(self, content, code=0):
        return subprocess.CompletedProcess(["adb"], code, content, b"private stderr must not appear")

    def assertCode(self, code, callback):
        with self.assertRaises(Apk2CliError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_selection_diagnostics(self):
        for output, code in [
            (b"List of devices attached\n", "no_device"),
            (b"List of devices attached\nphone\tunauthorized\n", "device_unauthorized"),
            (b"List of devices attached\nphone\toffline\n", "device_offline"),
            (b"List of devices attached\nfirst\tdevice\nsecond\tdevice\n", "multiple_devices"),
        ]:
            with self.subTest(code=code), patch("apk2cli.adb.subprocess.run", return_value=self.result(output)):
                self.assertCode(code, Adb().select)

    def test_environment_and_explicit_device_selection(self):
        output = b"List of devices attached\nphone-a\tdevice product:example\nphone-b\tdevice\n"
        with patch("apk2cli.adb.subprocess.run", return_value=self.result(output)), patch.dict(os.environ, {"ANDROID_SERIAL": "phone-b"}):
            self.assertEqual(Adb().select().serial, "phone-b")
            self.assertEqual(Adb(serial="phone-a").select().serial, "phone-a")
            self.assertCode("device_not_found", Adb(serial="absent").select)

    def test_run_as_is_exact_and_has_no_root_fallback(self):
        adb = Adb()
        adb.device = Device("phone", "device")
        with patch("apk2cli.adb.subprocess.run", return_value=self.result(b"session")) as run:
            self.assertEqual(adb.read_file("dev.apk2cli.demo", "shared_prefs/session.xml"), b"session")
            command = run.call_args.args[0]
        self.assertEqual(command, ["adb", "-s", "phone", "exec-out", "run-as", "dev.apk2cli.demo", "head", "-c", str(MAX_SOURCE_BYTES + 1), "shared_prefs/session.xml"])
        with patch("apk2cli.adb.subprocess.run", return_value=self.result(b"credential", 1)) as run:
            self.assertCode("source_unavailable", lambda: adb.read_file("dev.apk2cli.demo", "shared_prefs/session.xml"))
            self.assertEqual(run.call_count, 1)
            self.assertNotIn("su", run.call_args.args[0])

    def test_root_is_explicit_and_confined_to_package(self):
        adb = Adb()
        adb.device = Device("phone", "device")
        with patch("apk2cli.adb.subprocess.run", return_value=self.result(b"session")) as run:
            adb.read_file("dev.apk2cli.demo", "files/session.json", root=True)
        remote = shlex.split(" ".join(run.call_args.args[0][4:]))
        self.assertEqual(remote[:2], ["su", "-c"])
        self.assertEqual(len(remote), 3)
        self.assertIn("/data/user/0/dev.apk2cli.demo/files/session.json", remote[2])
        self.assertIn('case "$file" in "$base"/*)', remote[2])

    def test_rejects_shell_and_path_traversal(self):
        for path in ("/data/data/other/session", "shared_prefs/../files/session", "files/a;id", "files/$(id)", "files/a\nb", "files//a", "cache/session", "files/./a"):
            with self.subTest(path=path):
                self.assertCode("invalid_source", lambda: validate_source_path(path))
        adb = Adb()
        self.assertCode("invalid_package", lambda: adb.read_file("dev.app;id", "files/a"))

    def test_large_read_and_timeout_have_safe_errors(self):
        adb = Adb()
        adb.device = Device("phone", "device")
        with patch("apk2cli.adb.subprocess.run", return_value=self.result(b"a" * (MAX_SOURCE_BYTES + 1))):
            self.assertCode("source_too_large", lambda: adb.read_file("dev.apk2cli.demo", "files/a"))
        with patch("apk2cli.adb.subprocess.run", side_effect=subprocess.TimeoutExpired("adb", 20)):
            self.assertCode("adb_timeout", adb.devices)
        with patch("apk2cli.adb.subprocess.run", side_effect=FileNotFoundError):
            self.assertCode("adb_not_found", adb.devices)


class SourceTests(unittest.TestCase):
    def test_shared_prefs_exact_key_and_xml_escaping(self):
        data = b'<map><string name="unrelated">other</string><string name="access_token">a&amp;b</string></map>'
        self.assertEqual(extract_token({"format": "shared_prefs", "key": "access_token"}, data), "a&b")

    def test_json_key_and_pointer(self):
        self.assertEqual(extract_token({"format": "json", "key": "token"}, b'{"token":"secret"}'), "secret")
        self.assertEqual(extract_token({"format": "json", "pointer": "/sessions/0/access~1token"}, b'{"sessions":[{"access/token":"nested"}]}'), "nested")
        for pointer in ("sessions", "/sessions/-1", "/sessions/01", "/bad~escape"):
            with self.subTest(pointer=pointer), self.assertRaises(Apk2CliError):
                json_pointer({"sessions": ["token"]}, pointer)

    def test_sqlite_exact_key(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "store.db"
            with closing(sqlite3.connect(file)) as db:
                db.execute("CREATE TABLE preferences (key TEXT, value TEXT)")
                db.executemany("INSERT INTO preferences VALUES (?, ?)", [("unrelated", "other"), ("access_token", "sqlite-secret")])
                db.commit()
            data = file.read_bytes()
        source = {"format": "sqlite", "table": "preferences", "key_column": "key", "value_column": "value", "key": "access_token"}
        self.assertEqual(extract_token(source, data), "sqlite-secret")
        with self.assertRaises(Apk2CliError):
            extract_token({**source, "value_column": "missing"}, data)
        with self.assertRaises(Apk2CliError):
            extract_token({**source, "table": 'preferences"; DROP TABLE preferences;--'}, data)

    def test_rejects_malformed_ambiguous_or_header_injection_data(self):
        cases = [
            ({"format": "shared_prefs", "key": "t"}, b'<map><string name="t">one</string><string name="t">two</string></map>'),
            ({"format": "shared_prefs", "key": "t"}, b'<!DOCTYPE map [<!ENTITY secret "expanded">]><map><string name="t">&secret;</string></map>'),
            ({"format": "json", "key": "t"}, b'{"t":"secret\\r\\nInjected: yes"}'),
            ({"format": "json", "key": "t"}, b'{"t":false}'),
            ({"format": "json", "key": "t"}, b'{"t":""}'),
            ({"format": "json", "key": "t"}, b'not-json-secret'),
        ]
        for source, data in cases:
            with self.subTest(format=source["format"]), self.assertRaises(Apk2CliError) as caught:
                extract_token(source, data)
            self.assertNotIn("secret", str(caught.exception))


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "private-state"
        self.spec = spec()

    def login(self, token="private-test-token", response=None):
        data = f'<map><string name="access_token">{token}</string></map>'.encode()
        with patch("apk2cli.auth.Adb.select", return_value=Device("phone", "device")), patch("apk2cli.auth.Adb.read_file", return_value=data), patch("apk2cli.auth.request_json", return_value=response or {"username": "demo-user"}) as request:
            result = authenticate(self.spec, state_dir=self.directory)
            request.assert_called_once_with(self.spec, "GET", "/me", token=token)
            return result

    def test_login_verifies_then_saves_private_session(self):
        result = self.login()
        self.assertTrue(result["authenticated"])
        self.assertEqual(result["identity"], "demo-user")
        self.assertNotIn("private-test-token", json.dumps(result))
        self.assertEqual(get_token(self.spec, state_dir=self.directory), "private-test-token")
        store = SessionStore(self.spec, self.directory)
        if os.name != "nt":
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    def test_invalid_remote_session_preserves_existing_session(self):
        self.login(token="valid-token")
        before = SessionStore(self.spec, self.directory).path.read_bytes()
        with patch("apk2cli.auth.Adb.select", return_value=Device("phone", "device")), patch("apk2cli.auth.Adb.read_file", return_value=b'<map><string name="access_token">expired-token</string></map>'), patch("apk2cli.auth.request_json", side_effect=Apk2CliError("http_unauthorized", "Session rejected.")):
            with self.assertRaises(Apk2CliError):
                authenticate(self.spec, state_dir=self.directory)
        self.assertEqual(SessionStore(self.spec, self.directory).path.read_bytes(), before)

    def test_missing_identity_never_creates_session(self):
        for response in ({"username": ""}, {"username": None}, {"username": "private-test-token"}, {"other": "value"}):
            with self.subTest(response=response), self.assertRaises(Apk2CliError):
                self.login(response=response)
            self.assertFalse(SessionStore(self.spec, self.directory).path.exists())

    def test_status_and_logout_are_redacted(self):
        self.assertFalse(status(self.spec, state_dir=self.directory)["authenticated"])
        self.login()
        self.assertNotIn("private-test-token", json.dumps(status(self.spec, state_dir=self.directory)))
        self.assertTrue(logout(self.spec, state_dir=self.directory)["removed"])
        self.assertFalse(status(self.spec, state_dir=self.directory)["authenticated"])
        self.assertFalse(logout(self.spec, state_dir=self.directory)["removed"])
        with self.assertRaises(Apk2CliError) as caught:
            get_token(self.spec, state_dir=self.directory)
        self.assertEqual(caught.exception.code, "not_authenticated")

    def test_origins_and_apps_have_separate_sessions(self):
        self.login()
        for key, value in (("base_url", "https://different.example"), ("package", "dev.other.app"), ("name", "other")):
            alternate = {**self.spec, key: value}
            with self.subTest(key=key), self.assertRaises(Apk2CliError):
                get_token(alternate, state_dir=self.directory)

    def test_expiry_validation_and_stored_expiry(self):
        self.spec["auth"]["verify"]["expires_at_pointer"] = "/expires_at"
        result = self.login(response={"username": "demo-user", "expires_at": time.time() + 100})
        self.assertIn("expires_at", result)
        with patch("apk2cli.session.time.time", return_value=time.time() + 200):
            self.assertFalse(status(self.spec, state_dir=self.directory)["authenticated"])
            with self.assertRaises(Apk2CliError) as caught:
                get_token(self.spec, state_dir=self.directory)
        self.assertEqual(caught.exception.code, "session_expired")
        for invalid in (True, 0, float("nan"), float("inf"), "2025-01-01T00:00:00", "invalid"):
            with self.subTest(invalid=invalid), self.assertRaises(Apk2CliError):
                parse_expiry(invalid)

    @unittest.skipIf(os.name == "nt", "POSIX permission semantics")
    def test_refuses_shared_readable_state(self):
        self.login()
        store = SessionStore(self.spec, self.directory)
        store.path.chmod(0o644)
        with self.assertRaises(Apk2CliError) as caught:
            get_token(self.spec, state_dir=self.directory)
        self.assertEqual(caught.exception.code, "unsafe_session_file")

    def test_state_directory_environment(self):
        with patch.dict(os.environ, {"APK2CLI_STATE_DIR": str(self.directory)}):
            self.assertEqual(SessionStore(self.spec).directory, self.directory)


if __name__ == "__main__":
    unittest.main()
