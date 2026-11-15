"""Exercise generated-command runtime arguments against a real local HTTP server."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from apk2cli.runtime import emit, run
from apk2cli.session import SessionStore, now_iso
from apk2cli.spec import validate_spec
from apk2cli.errors import Apk2CliError


class RuntimeReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.state = self.directory / "private-state"
        self.calls = []
        calls = self.calls

        class Handler(BaseHTTPRequestHandler):
            def reply(inner):
                raw = inner.rfile.read(int(inner.headers.get("Content-Length", "0")))
                body = json.loads(raw) if raw else None
                calls.append({"method": inner.command, "path": inner.path, "body": body})
                response = {"username": "test-account", "echo": inner.headers.get("Authorization", "")}
                payload = json.dumps(response).encode()
                inner.send_response(200)
                inner.send_header("Content-Type", "application/json")
                inner.send_header("Content-Length", str(len(payload)))
                inner.end_headers()
                inner.wfile.write(payload)

            do_GET = reply
            do_POST = reply

            def log_message(inner, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.proxy = patch.dict("os.environ", {"NO_PROXY": "127.0.0.1,localhost"})
        self.proxy.start()
        self.addCleanup(self.proxy.stop)
        self.spec = {
            "schema_version": 1, "name": "runtime-review", "package": "dev.apk2cli.review",
            "base_url": f"http://127.0.0.1:{self.server.server_port}",
            "auth": {
                "sources": [{"path": "shared_prefs/session.xml", "format": "shared_prefs", "key": "token"}],
                "header": "Authorization", "prefix": "Bearer ",
                "verify": {"method": "GET", "path": "/me", "identity_pointer": "/username"},
            },
            "commands": [{"name": "me", "method": "GET", "path": "/me"}],
        }
        self.spec_file = self.directory / "app.json"
        self.token = "review-token"
        self.save_session()

    def save_session(self):
        SessionStore(self.spec, self.state).save({
            "schema_version": 1, **{key: self.spec[key] for key in ("name", "package", "base_url")},
            "token": self.token, "identity": "test-account", "imported_at": now_iso(),
            "source": {"transport": "fixture"},
        })

    def invoke(self, arguments):
        self.spec_file.write_text(json.dumps(self.spec), encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = run(self.spec_file, arguments)
            except SystemExit as exc:
                code = exc.code
        return code, stdout.getvalue(), stderr.getvalue()

    def test_state_directory_before_and_after_subcommand(self):
        for arguments in (["--state-dir", str(self.state), "me"], ["me", "--state-dir", str(self.state)]):
            with self.subTest(arguments=arguments):
                code, output, error = self.invoke(arguments)
                self.assertEqual(code, 0, error)
                self.assertEqual(json.loads(output)["username"], "test-account")

    def test_state_directory_for_nested_auth_commands(self):
        for arguments in (
            ["--state-dir", str(self.state), "auth", "status", "--verify"],
            ["auth", "--state-dir", str(self.state), "status", "--verify"],
            ["auth", "status", "--state-dir", str(self.state), "--verify"],
        ):
            with self.subTest(arguments=arguments):
                code, output, error = self.invoke(arguments)
                self.assertEqual(code, 0, error)
                self.assertTrue(json.loads(output)["authenticated"])

    def test_api_parameter_named_command_does_not_override_subcommand(self):
        self.spec["commands"] = [{
            "name": "invoke", "method": "POST", "path": "/actions",
            "parameters": [{"name": "command", "in": "body", "required": True}],
        }]
        code, output, error = self.invoke(["invoke", "--state-dir", str(self.state), "--command", "refresh"])
        self.assertEqual(code, 0, error)
        self.assertEqual(self.calls[-1]["body"], {"command": "refresh"})

    def test_query_types_and_path_escaping(self):
        self.spec["commands"] = [{
            "name": "get", "method": "GET", "path": "/items/{id}",
            "parameters": [
                {"name": "id", "in": "path"},
                {"name": "active", "in": "query", "type": "boolean"},
                {"name": "count", "in": "query", "type": "integer", "default": 10},
                {"name": "q", "in": "query"},
            ],
        }]
        code, output, error = self.invoke(["get", "--state-dir", str(self.state), "--id", "a/b", "--active", "false", "--q", "a&b#c"])
        self.assertEqual(code, 0, error)
        self.assertEqual(self.calls[-1]["path"], "/items/a%2Fb?active=false&count=10&q=a%26b%23c")

    def test_typed_body_arguments(self):
        self.spec["commands"] = [{
            "name": "create", "method": "POST", "path": "/items",
            "parameters": [
                {"name": "active", "in": "body", "type": "boolean"},
                {"name": "count", "in": "body", "type": "integer"},
                {"name": "ratio", "in": "body", "type": "number"},
            ],
        }]
        code, output, error = self.invoke(["create", "--state-dir", str(self.state), "--active", "true", "--count", "7", "--ratio", "0.5"])
        self.assertEqual(code, 0, error)
        self.assertEqual(self.calls[-1]["body"], {"active": True, "count": 7, "ratio": 0.5})

    def test_nonfinite_numeric_input_fails_before_http(self):
        self.spec["commands"] = [{"name": "create", "method": "POST", "path": "/items", "parameters": [{"name": "ratio", "in": "body", "type": "number"}]}]
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                code, output, error = self.invoke(["create", "--state-dir", str(self.state), "--ratio=" + value])
                self.assertNotEqual(code, 0)
                self.assertNotIn("Traceback", error)
                self.assertEqual(self.calls, [])

    def test_nonfinite_and_mistyped_defaults_fail_validation(self):
        for kind, value in (("number", float("nan")), ("number", float("inf")), ("integer", "many"), ("boolean", "false")):
            with self.subTest(kind=kind, value=value):
                self.spec["commands"] = [{"name": "create", "method": "POST", "path": "/items", "parameters": [{"name": "value", "in": "body", "type": kind, "default": value}]}]
                with self.assertRaises(Apk2CliError):
                    validate_spec(self.spec)

    def test_echoed_quoted_credential_is_redacted(self):
        self.token = 'review"\\token'
        self.save_session()
        code, output, error = self.invoke(["me", "--state-dir", str(self.state)])
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["echo"], "Bearer [redacted]")

    def test_structured_redaction_preserves_json_and_handles_escaped_strings(self):
        token = 'secret"\\value'
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            emit({"data": [token, {"message": "prefix " + token}], token: "key"}, token=token)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["data"], ["[redacted]", {"message": "prefix [redacted]"}])
        self.assertIn("[redacted]", result)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            emit({"number": 123, "string": "123"}, token="123")
        json.loads(stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
