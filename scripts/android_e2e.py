#!/usr/bin/env python3
"""Exercise a real installed Android app, ADB session import, and generated CLI."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET


PACKAGE = "dev.apk2cli.demo"
REPO = Path(__file__).resolve().parents[1]


class DemoServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), DemoHandler)
        self.token = secrets.token_urlsafe(32)
        self.token_active = True
        self.login_count = 0
        self.authenticated_requests = 0


class DemoHandler(BaseHTTPRequestHandler):
    server: DemoServer

    def log_message(self, *_args: object) -> None:
        pass

    def respond(self, status: int, body: dict) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(min(length, 4096))
        if self.path != "/login":
            self.respond(404, {"error": "not_found"})
            return
        try:
            credentials = json.loads(body)
        except ValueError:
            self.respond(400, {"error": "invalid_json"})
            return
        if credentials != {"username": "demo", "password": "fixture-only"}:
            self.respond(401, {"error": "invalid_credentials"})
            return
        self.server.login_count += 1
        self.respond(200, {"access_token": self.server.token, "username": "demo"})

    def do_GET(self) -> None:
        expected = "Bearer " + self.server.token
        supplied = self.headers.get("Authorization", "")
        if not self.server.token_active or not secrets.compare_digest(supplied, expected):
            self.respond(401, {"error": "unauthorized"})
            return
        self.server.authenticated_requests += 1
        if self.path == "/me":
            self.respond(200, {"username": "demo", "id": "demo-account"})
        elif self.path == "/items":
            self.respond(200, {"items": [{"id": "one", "title": "Read from the signed-in account"}]})
        else:
            self.respond(404, {"error": "not_found"})


def run(command: list[str], *, env: dict[str, str] | None = None,
        expected: int = 0, private_value: str | None = None,
        timeout: int = 45) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=REPO, env=env, capture_output=True,
                            text=True, timeout=timeout)
    if private_value and private_value in result.stdout + result.stderr:
        raise AssertionError("A CLI output exposed the private session value")
    if result.returncode != expected:
        # Commands and outputs here contain only fixture metadata; still redact first.
        detail = (result.stderr or result.stdout).strip()
        if private_value:
            detail = detail.replace(private_value, "[redacted]")
        raise AssertionError(f"Command {command[:3]} exited {result.returncode}: {detail[:2000]}")
    return result


def adb_command(adb: str, serial: str, *args: str) -> list[str]:
    return [adb, "-s", serial, *args]


def dump_ui(adb: str, serial: str) -> ET.Element:
    run(adb_command(adb, serial, "shell", "uiautomator", "dump", "/sdcard/apk2cli-e2e-ui.xml"))
    output = run(adb_command(adb, serial, "shell", "cat", "/sdcard/apk2cli-e2e-ui.xml"))
    return ET.fromstring(output.stdout)


def wait_ui(adb: str, serial: str, *, text: str | None = None,
            content_description: str | None = None) -> ET.Element:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        root = dump_ui(adb, serial)
        for node in root.iter("node"):
            if text is not None and node.get("text") == text:
                return node
            if content_description is not None and node.get("content-desc") == content_description:
                return node
        time.sleep(0.5)
    raise AssertionError("Expected demo screen did not appear")


def tap(adb: str, serial: str, node: ET.Element) -> None:
    numbers = [int(value) for value in re.findall(r"\d+", node.get("bounds", ""))]
    if len(numbers) != 4:
        raise AssertionError("Sign-in button has no usable bounds")
    x = (numbers[0] + numbers[2]) // 2
    y = (numbers[1] + numbers[3]) // 2
    run(adb_command(adb, serial, "shell", "input", "tap", str(x), str(y)))


def select_device(adb: str, serial: str | None) -> str:
    result = run([adb, "devices"])
    devices = [line.split()[0] for line in result.stdout.splitlines()[1:]
               if len(line.split()) >= 2 and line.split()[1] == "device"]
    if serial:
        if serial not in devices:
            raise AssertionError("Requested ADB device is not online")
        return serial
    if len(devices) != 1:
        raise AssertionError("E2E requires exactly one online ADB device or --serial")
    return devices[0]


def assert_failure(command: list[str], *, env: dict[str, str], secret: str) -> None:
    result = subprocess.run(command, cwd=REPO, env=env, capture_output=True,
                            text=True, timeout=45)
    if secret in result.stdout + result.stderr:
        raise AssertionError("A failed command exposed the private session value")
    if result.returncode == 0:
        raise AssertionError("An unauthenticated operation unexpectedly succeeded")


def exercise(args: argparse.Namespace, report: dict) -> None:
    serial = select_device(args.adb, args.serial)
    # A shell UID of 2000 demonstrates run-as, rather than root ADB, is sufficient.
    run(adb_command(args.adb, serial, "unroot"))
    run(adb_command(args.adb, serial, "wait-for-device"))
    shell_uid = run(adb_command(args.adb, serial, "shell", "id", "-u")).stdout.strip()
    if shell_uid != "2000":
        raise AssertionError("E2E requires an unprivileged ADB shell (UID 2000)")
    report["device"] = {
        "kind": "android_emulator" if serial.startswith("emulator-") else "physical_android_device",
        "api_level": run(adb_command(args.adb, serial, "shell", "getprop", "ro.build.version.sdk")).stdout.strip(),
        "adb_shell_uid": shell_uid,
    }
    server = DemoServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    checks = report["checks"]

    def passed(name: str) -> None:
        checks.append({"name": name, "status": "passed"})
        print(json.dumps({"check": name, "status": "passed"}), flush=True)

    try:
        with tempfile.TemporaryDirectory(prefix="apk2cli-e2e-") as temporary:
            work = Path(temporary)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(REPO / "src")
            env["APK2CLI_STATE_DIR"] = str(work / "state")
            spec = {
                "schema_version": 1,
                "name": "demo",
                "package": PACKAGE,
                "base_url": base_url,
                "auth": {
                    "sources": [{"path": "shared_prefs/session.xml", "format": "shared_prefs", "key": "access_token"}],
                    "header": "Authorization",
                    "prefix": "Bearer ",
                    "verify": {"method": "GET", "path": "/me", "identity_pointer": "/username"},
                },
                "commands": [
                    {"name": "me", "method": "GET", "path": "/me", "help": "Show account"},
                    {"name": "items", "method": "GET", "path": "/items", "help": "List items"},
                ],
            }
            spec_path = work / "spec.json"
            spec_path.write_text(json.dumps(spec), encoding="utf-8")
            run([sys.executable, "-m", "apk2cli", "inspect", str(args.apk), "--json"], env=env)
            passed("inspect_compiled_binary_manifest_apk")
            run([sys.executable, "-m", "apk2cli", "build", str(args.apk), "--name", "demo",
                 "--spec", str(spec_path), "--out", str(work / "cli")], env=env)
            launcher = [sys.executable, str(work / "cli" / "demo")]
            # The generated CLI must run independently of the source package.
            cli_env = env.copy()
            cli_env.pop("PYTHONPATH", None)
            run(launcher + ["--help"], env=cli_env)
            passed("build_standalone_cli")
            assert_failure(launcher + ["me"], env=cli_env, secret=server.token)
            passed("api_requires_imported_session")

            run(adb_command(args.adb, serial, "install", "-r", str(args.apk)), timeout=90)
            run(adb_command(args.adb, serial, "shell", "pm", "clear", PACKAGE))
            run(adb_command(args.adb, serial, "reverse", f"tcp:{port}", f"tcp:{port}"))
            run(adb_command(args.adb, serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP"))
            run(adb_command(args.adb, serial, "shell", "wm", "dismiss-keyguard"))
            run(adb_command(args.adb, serial, "shell", "am", "start", "-W", "-n",
                            f"{PACKAGE}/.MainActivity", "--es", "base_url", base_url))
            button = wait_ui(args.adb, serial, content_description="apk2cli-sign-in")
            tap(args.adb, serial, button)
            wait_ui(args.adb, serial, text="Signed in as demo")
            if server.login_count != 1:
                raise AssertionError("App UI did not perform exactly one server login")
            passed("sign_in_through_android_app_ui_and_http")

            if args.screenshot:
                screenshot = subprocess.run(adb_command(args.adb, serial, "exec-out", "screencap", "-p"),
                                            capture_output=True, timeout=30, check=True)
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                args.screenshot.write_bytes(screenshot.stdout)

            login_args = ["auth", "--adb", "--serial", serial]
            if args.adb != "adb":
                login_args.extend(["--adb-path", args.adb])
            server.token_active = False
            assert_failure(launcher + login_args, env=cli_env, secret=server.token)
            failed_status = json.loads(run(launcher + ["auth", "status"], env=cli_env,
                                           private_value=server.token).stdout)
            if failed_status.get("authenticated") is not False:
                raise AssertionError("Failed verification left a saved CLI session")
            server.token_active = True
            passed("auth_verifies_app_session_before_saving")
            result = run(launcher + login_args, env=cli_env, private_value=server.token)
            if "demo" not in result.stdout:
                raise AssertionError("Auth result omitted the verified demo identity")
            passed("auth_adb_imports_existing_app_session_without_root")
            me = json.loads(run(launcher + ["me"], env=cli_env, private_value=server.token).stdout)
            if me.get("username") != "demo":
                raise AssertionError("Generated me command returned the wrong identity")
            items = json.loads(run(launcher + ["items"], env=cli_env, private_value=server.token).stdout)
            if items.get("items", [{}])[0].get("id") != "one":
                raise AssertionError("Generated items command did not return the protected resource")
            passed("generated_commands_use_authenticated_real_http")
            run(launcher + ["auth", "status"], env=cli_env, private_value=server.token)
            passed("status_keeps_private_session_value_redacted")

            server.token_active = False
            assert_failure(launcher + ["me"], env=cli_env, secret=server.token)
            passed("revoked_session_fails_authenticated_request")
            server.token_active = True
            run(launcher + ["auth", "logout"], env=cli_env, private_value=server.token)
            assert_failure(launcher + ["me"], env=cli_env, secret=server.token)
            passed("logout_removes_local_session")
            run(launcher + login_args, env=cli_env, private_value=server.token)
            if server.login_count != 1:
                raise AssertionError("Session import caused an extra app login")
            passed("app_remains_logged_in_after_cli_logout")
            run(launcher + ["auth", "logout"], env=cli_env, private_value=server.token)

            if args.release_apk:
                run(adb_command(args.adb, serial, "install", "-r", str(args.release_apk)), timeout=90)
                assert_failure(launcher + login_args, env=cli_env, secret=server.token)
                assert_failure(launcher + ["me"], env=cli_env, secret=server.token)
                passed("non_debuggable_app_rejects_unprivileged_session_import")

            report["server"] = {"app_login_count": server.login_count,
                                "authenticated_requests": server.authenticated_requests}
            report["status"] = "passed"
    finally:
        subprocess.run(adb_command(args.adb, serial, "reverse", "--remove", f"tcp:{port}"),
                       capture_output=True, timeout=15)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apk", type=Path, required=True)
    parser.add_argument("--release-apk", type=Path)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--serial")
    parser.add_argument("--report", type=Path, default=REPO / "build/android-e2e.json")
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    args.apk = args.apk.resolve()
    if args.release_apk:
        args.release_apk = args.release_apk.resolve()
    report = {"schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
              "status": "failed", "checks": []}
    exit_code = 0
    try:
        exercise(args, report)
    except Exception as error:
        # No private session material is included in command diagnostics.
        report["error"] = str(error)
        print(f"Android E2E failed: {error}", file=sys.stderr)
        exit_code = 1
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "passed_checks": len(report["checks"]),
                      "report": str(args.report)}))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
