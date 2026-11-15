"""The emulator's HTTP fixture must reject missing, invalid, and revoked sessions."""

import importlib.util
import json
from pathlib import Path
import threading
import unittest
import urllib.error
import urllib.request


_spec = importlib.util.spec_from_file_location(
    "android_e2e", Path(__file__).resolve().parents[1] / "scripts/android_e2e.py"
)
assert _spec is not None and _spec.loader is not None
android_e2e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(android_e2e)


class E2EServerTests(unittest.TestCase):
    def setUp(self):
        self.server = android_e2e.DemoServer()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, *, credentials=None, token=None):
        data = json.dumps(credentials).encode() if credentials is not None else None
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def rejected(self, path, *, credentials=None, token=None):
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.request(path, credentials=credentials, token=token)
        self.assertEqual(failure.exception.code, 401)

    def test_fixture_requires_login_and_recognizes_revocation(self):
        self.rejected("/me")
        self.rejected("/login", credentials={"username": "demo", "password": "wrong"})
        self.assertEqual(self.server.login_count, 0)
        session = self.request("/login", credentials={"username": "demo", "password": "fixture-only"})
        self.assertEqual(self.server.login_count, 1)
        self.rejected("/me", token="wrong-session")
        token = session["access_token"]
        self.assertEqual(self.request("/me", token=token)["username"], "demo")
        self.assertEqual(len(self.request("/items", token=token)["items"]), 1)
        self.server.token_active = False
        self.rejected("/items", token=token)
        self.assertEqual(self.server.authenticated_requests, 2)


if __name__ == "__main__":
    unittest.main()
