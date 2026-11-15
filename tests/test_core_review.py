"""Independent compiled-manifest and HTTP/session-boundary regression checks."""

from __future__ import annotations

import json
import struct
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from apk2cli.adb import validate_source_path
from apk2cli.apk import ANDROID, inspect_apk, parse_manifest
from apk2cli.errors import Apk2CliError
from apk2cli.http import request_json
from apk2cli.spec import validate_base_url, validate_path, validate_spec


def valid_spec():
    return {
        "schema_version": 1, "name": "review", "package": "dev.apk2cli.review",
        "base_url": "http://127.0.0.1:8765",
        "auth": {
            "sources": [{"path": "shared_prefs/session.xml", "format": "shared_prefs", "key": "token"}],
            "header": "Authorization", "prefix": "Bearer ",
            "verify": {"method": "GET", "path": "/me", "identity_pointer": "/username"},
        },
        "commands": [{"name": "me", "method": "GET", "path": "/me"}],
    }


def compiled_manifest(*, utf8=True, short_end_header=False):
    """Construct the published ResXMLTree/ResStringPool binary layouts."""
    strings = [
        "manifest", "package", "dev.apk2cli.review", ANDROID, "versionCode", "versionName", "1.0",
        "uses-sdk", "minSdkVersion", "targetSdkVersion", "application", "debuggable", "activity",
        "name", ".MainActivity", "intent-filter", "action", "android.intent.action.MAIN", "category",
        "android.intent.category.LAUNCHER", "uses-permission", "android.permission.INTERNET", "label", "测试应用",
    ]
    index = {value: offset for offset, value in enumerate(strings)}
    offsets, content = [], bytearray()
    for value in strings:
        offsets.append(len(content))
        if utf8:
            raw = value.encode("utf-8")
            content.extend(bytes([len(value), len(raw)]) + raw + b"\0")
        else:
            raw = value.encode("utf-16le")
            content.extend(struct.pack("<H", len(raw) // 2) + raw + b"\0\0")
    content.extend(b"\0" * (-len(content) % 4))
    data_start = 28 + 4 * len(strings)
    pool = struct.pack("<HHIIIIII", 1, 28, data_start + len(content), len(strings), 0, 0x100 if utf8 else 0, data_start, 0)
    pool += struct.pack(f"<{len(offsets)}I", *offsets) + content

    def node_header(kind, size):
        return struct.pack("<HHIII", kind, 16, size, 1, 0xFFFFFFFF)

    def start(tag, attributes=()):
        attrs = bytearray()
        for namespace, name, dtype, value in attributes:
            raw = index[value] if dtype == 3 else 0xFFFFFFFF
            encoded = index[value] if dtype == 3 else value
            attrs.extend(struct.pack("<IIIHBBI", index[namespace] if namespace else 0xFFFFFFFF, index[name], raw, 8, 0, dtype, encoded))
        return node_header(0x102, 36 + len(attrs)) + struct.pack("<IIHHHHHH", 0xFFFFFFFF, index[tag], 20, 20, len(attributes), 0, 0, 0) + attrs

    def end(tag, short=False):
        if short:
            return struct.pack("<HHIII", 0x103, 8, 16, 0xFFFFFFFF, index[tag])
        return node_header(0x103, 24) + struct.pack("<II", 0xFFFFFFFF, index[tag])

    parts = [pool]
    parts.append(start("manifest", [(None, "package", 3, "dev.apk2cli.review"), (ANDROID, "versionCode", 0x10, 42), (ANDROID, "versionName", 3, "1.0")]))
    parts.append(start("uses-sdk", [(ANDROID, "minSdkVersion", 0x10, 23), (ANDROID, "targetSdkVersion", 0x10, 35)]))
    parts.append(end("uses-sdk"))
    parts.append(start("uses-permission", [(ANDROID, "name", 3, "android.permission.INTERNET")]))
    parts.append(end("uses-permission"))
    parts.append(start("application", [(ANDROID, "debuggable", 0x12, 1), (ANDROID, "label", 3, "测试应用")]))
    parts.append(start("activity", [(ANDROID, "name", 3, ".MainActivity")]))
    parts.append(start("intent-filter"))
    parts.append(start("action", [(ANDROID, "name", 3, "android.intent.action.MAIN")]))
    parts.append(end("action"))
    parts.append(start("category", [(ANDROID, "name", 3, "android.intent.category.LAUNCHER")]))
    parts.append(end("category"))
    parts.append(end("intent-filter"))
    parts.append(end("activity"))
    parts.append(end("application"))
    parts.append(end("manifest", short=short_end_header))
    body = b"".join(parts)
    return struct.pack("<HHI", 3, 8, 8 + len(body)) + body


class ManifestReviewTests(unittest.TestCase):
    def test_reads_utf8_and_utf16_compiled_manifests(self):
        for utf8 in (True, False):
            with self.subTest(utf8=utf8):
                root = parse_manifest(compiled_manifest(utf8=utf8))
                self.assertEqual(root.get("package"), "dev.apk2cli.review")
                self.assertEqual(root.get(f"{{{ANDROID}}}versionCode"), "42")
                application = root.find("application")
                self.assertEqual(application.get(f"{{{ANDROID}}}debuggable"), "true")
                self.assertEqual(application.get(f"{{{ANDROID}}}label"), "测试应用")

    def test_inventory_reads_compiled_apk_and_removes_url_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / "review.apk"
            with zipfile.ZipFile(apk, "w") as archive:
                archive.writestr("AndroidManifest.xml", compiled_manifest())
                archive.writestr("classes.dex", b"dex\n035\0https://api.example.test/v1/me?token=private#fragment\0")
                archive.writestr("lib/arm64-v8a/libexample.so", b"fixture")
            report = inspect_apk(apk)
        self.assertEqual(report["manifest"]["launcher_activities"], ["dev.apk2cli.review.MainActivity"])
        self.assertEqual(report["manifest"]["permissions"], ["android.permission.INTERNET"])
        self.assertEqual(report["archive"]["native_abis"], ["arm64-v8a"])
        self.assertEqual(report["url_candidates"], [{"url": "https://api.example.test/v1/me", "source": "classes.dex", "status": "unverified"}])
        self.assertNotIn("private", json.dumps(report["url_candidates"]))

    def test_rejects_truncated_pool_offsets_and_element_bounds(self):
        original = compiled_manifest()
        bad_pool = bytearray(original)
        struct.pack_into("<I", bad_pool, 8 + 28, 0xFFFFFFFE)
        bad_element = bytearray(original)
        element = 8 + struct.unpack_from("<I", original, 12)[0]
        struct.pack_into("<H", bad_element, element + 16 + 10, 8)
        for malformed in (original[:-1], bytes(bad_pool), bytes(bad_element), b"not-a-manifest"):
            with self.subTest(size=len(malformed)), self.assertRaises(Apk2CliError) as caught:
                parse_manifest(malformed)
            self.assertEqual(caught.exception.code, "invalid_manifest")

    def test_rejects_short_end_element_node_header(self):
        with self.assertRaises(Apk2CliError) as caught:
            parse_manifest(compiled_manifest(short_end_header=True))
        self.assertEqual(caught.exception.code, "invalid_manifest")


class ContractReviewTests(unittest.TestCase):
    def test_source_path_contract_matches_adb(self):
        for path in ("files/session file.json", "files//token", "files/./token", "files/token;echo", "files/../token"):
            with self.subTest(path=path):
                contract = valid_spec()
                contract["auth"]["sources"][0]["path"] = path
                with self.assertRaises(Apk2CliError):
                    validate_source_path(path)
                with self.assertRaises(Apk2CliError):
                    validate_spec(contract)

    def test_rejects_empty_query_and_fragment_in_api_base(self):
        for base in ("https://api.example.test?", "https://api.example.test/api#", "https://api.example.test/api/?"):
            with self.subTest(base=base), self.assertRaises(Apk2CliError):
                validate_base_url(base)

    def test_invalid_base_types_have_domain_errors(self):
        for base in (None, 42, [], {}):
            with self.subTest(base=base):
                with self.assertRaises(Apk2CliError) as caught:
                    validate_base_url(base)
                self.assertEqual(caught.exception.code, "invalid_spec")

    def test_same_origin_paths_reject_traversal_or_url_replacement(self):
        for path in ("https://other.example/me", "//other.example/me", "/../me", "/%2e%2e/me", "/a\\b", "/a%5cb", "/me?x=1", "/me#secret"):
            with self.subTest(path=path), self.assertRaises(Apk2CliError):
                validate_path(path)
        self.assertEqual(validate_path("/v1/items/{item-id}"), "/v1/items/{item-id}")

    def test_placeholder_contract_and_reserved_commands(self):
        cases = [
            {"name": "auth", "method": "GET", "path": "/me"},
            {"name": "get", "method": "GET", "path": "/items/{id}"},
            {"name": "get", "method": "GET", "path": "/items", "parameters": [{"name": "id", "in": "path"}]},
            {"name": "get", "method": "GET", "path": "/items", "parameters": [{"name": "payload", "in": "body"}]},
        ]
        for command in cases:
            contract = valid_spec()
            contract["commands"] = [command]
            with self.subTest(command=command), self.assertRaises(Apk2CliError):
                validate_spec(contract)


class HttpReviewTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.destination_calls = []
        calls, destination_calls = self.calls, self.destination_calls

        class Destination(BaseHTTPRequestHandler):
            def do_GET(inner):
                destination_calls.append(dict(inner.headers))
                inner.send_response(200)
                inner.end_headers()
                inner.wfile.write(b'{"username":"redirected"}')

            def log_message(inner, *args):
                pass

        self.destination = self.serve(Destination)
        destination_url = f"http://127.0.0.1:{self.destination.server_port}/me"

        class Origin(BaseHTTPRequestHandler):
            def do_GET(inner):
                calls.append((inner.path, dict(inner.headers)))
                if inner.path == "/redirect":
                    inner.send_response(302)
                    inner.send_header("Location", destination_url)
                    inner.end_headers()
                elif inner.path == "/rejected":
                    inner.send_response(401)
                    inner.end_headers()
                    inner.wfile.write(json.dumps({"echo": inner.headers.get("Authorization")}).encode())
                else:
                    inner.send_response(200)
                    inner.end_headers()
                    inner.wfile.write(b'{"username":"review-user"}')

            def log_message(inner, *args):
                pass

        self.origin = self.serve(Origin)
        self.spec = valid_spec()
        self.spec["base_url"] = f"http://127.0.0.1:{self.origin.server_port}"
        self.proxy = patch.dict("os.environ", {"NO_PROXY": "127.0.0.1,localhost"})
        self.proxy.start()
        self.addCleanup(self.proxy.stop)

    def serve(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_redirect_never_receives_credential(self):
        with self.assertRaises(Apk2CliError) as caught:
            request_json(self.spec, "GET", "/redirect", token="private-review-token")
        self.assertEqual(caught.exception.code, "redirect_rejected")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]["Authorization"], "Bearer private-review-token")
        self.assertEqual(self.destination_calls, [])

    def test_server_error_body_does_not_expose_session(self):
        with self.assertRaises(Apk2CliError) as caught:
            request_json(self.spec, "GET", "/rejected", token="private-review-token")
        self.assertEqual(caught.exception.code, "session_rejected")
        self.assertNotIn("private-review-token", str(caught.exception))

    def test_parameters_are_encoded_and_valid_json_is_returned(self):
        response = request_json(self.spec, "GET", "/me", token="private-review-token", params={"q": "a&b#c"})
        self.assertEqual(response, {"username": "review-user"})
        self.assertEqual(self.calls[0][0], "/me?q=a%26b%23c")


if __name__ == "__main__":
    unittest.main()
