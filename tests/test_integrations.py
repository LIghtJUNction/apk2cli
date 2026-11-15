"""Adapter contract tests using real child processes, not installed providers."""

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from apk2cli.errors import Apk2CliError
from apk2cli import integrations


@unittest.skipUnless(os.name == "posix", "Executable fixture uses a POSIX shebang")
class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="apk2cli-tools-")
        self.root = Path(self.temp.name)
        self.apk = self.root / "app ' $(touch should-not-exist).apk"
        with zipfile.ZipFile(self.apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", '<manifest package="example.demo"/>')
        self.executable = self.root / "analysis-tool"

    def tearDown(self):
        self.temp.cleanup()

    def tool(self, body):
        self.executable.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
        self.executable.chmod(0o700)
        return patch.object(integrations.shutil, "which", return_value=str(self.executable))

    def test_craft_json_is_input_and_output_format_is_explicit(self):
        with self.tool("import json, sys; print(json.dumps(sys.argv[1:]))"):
            result = integrations.run_tool("craft-anything", self.apk, self.root / "evidence")
        self.assertEqual(result["status"], "success")
        args = json.loads(Path(result["stdout_path"]).read_text())
        self.assertEqual(args[0], "android-package-show")
        self.assertEqual(args[-2:], ["--output-format", "json"])
        snapshot = Path(json.loads(args[2])["path"])
        self.assertEqual(snapshot.read_bytes(), self.apk.read_bytes())
        self.assertEqual(result["input_sha256"], hashlib.sha256(snapshot.read_bytes()).hexdigest())
        self.assertFalse((self.root / "should-not-exist").exists())
        self.assertEqual(stat.S_IMODE(snapshot.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(snapshot.parent.stat().st_mode), 0o700)
        for name in ("stdout.log", "stderr.log", "result.json"):
            self.assertEqual(stat.S_IMODE((snapshot.parent / name).stat().st_mode), 0o600)

    def test_failure_keeps_private_evidence_without_inline_output(self):
        secret = "example-sensitive-analysis-string"
        with self.tool(f"import sys; print({secret!r}); print('failed', file=sys.stderr); sys.exit(7)"):
            result = integrations.run_tool("rea", self.apk, self.root / "evidence")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["returncode"], 7)
        self.assertNotIn(secret, json.dumps(result))
        self.assertIn(secret, Path(result["stdout_path"]).read_text())
        self.assertEqual(result["argv"][1], "inspect-android-package")

    def test_timeout_terminates_process(self):
        with self.tool("import time; time.sleep(60)"), patch.object(integrations, "TOOL_TIMEOUT", 0.15):
            result = integrations.run_tool("jadx", self.apk, self.root / "evidence")
        self.assertEqual(result["status"], "timeout")
        self.assertLess(result["elapsed_seconds"], 3)
        self.assertNotEqual(result["returncode"], 0)

    def test_output_is_capped(self):
        with self.tool("import sys; sys.stdout.write('x' * 200000)"), patch.object(integrations, "MAX_LOG_BYTES", 4096):
            result = integrations.run_tool("apktool", self.apk, self.root / "evidence")
        self.assertEqual(result["status"], "output_limit")
        self.assertEqual(Path(result["stdout_path"]).stat().st_size, 4096)
        self.assertTrue(result["truncated"]["stdout"])

    def test_finished_parent_cannot_leave_a_pipe_open_indefinitely(self):
        body = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); print('spawned')"
        with self.tool(body), patch.object(integrations, "TOOL_TIMEOUT", 0.15):
            result = integrations.run_tool("rea", self.apk, self.root / "evidence")
        self.assertEqual(result["status"], "timeout")
        self.assertLess(result["elapsed_seconds"], 3)
        self.assertIn("spawned", Path(result["stdout_path"]).read_text())

    def test_each_run_retains_its_own_evidence(self):
        with self.tool("print('ok')"):
            first = integrations.run_tool("rea", self.apk, self.root / "evidence")
            second = integrations.run_tool("rea", self.apk, self.root / "evidence")
        self.assertNotEqual(first["evidence_dir"], second["evidence_dir"])
        self.assertTrue(Path(first["stdout_path"]).is_file())

    def test_missing_tool_does_not_install_or_create_evidence(self):
        with patch.object(integrations.shutil, "which", return_value=None):
            with self.assertRaises(Apk2CliError) as caught:
                integrations.run_tool("rea", self.apk, self.root / "evidence")
        self.assertEqual(caught.exception.code, "tool_unavailable")
        self.assertFalse((self.root / "evidence").exists())

    def test_skill_is_not_executed(self):
        with self.assertRaises(Apk2CliError) as caught:
            integrations.run_tool("leanrea", self.apk, self.root / "evidence")
        self.assertEqual(caught.exception.code, "skill_not_executable")

    def test_non_apk_zip_is_rejected_before_starting_tool(self):
        with zipfile.ZipFile(self.apk, "w") as archive:
            archive.writestr("not-an-android-manifest.txt", "example")
        with self.tool("raise AssertionError('must not run')"):
            with self.assertRaises(Apk2CliError) as caught:
                integrations.run_tool("rea", self.apk, self.root / "evidence")
        self.assertEqual(caught.exception.code, "invalid_apk")
        self.assertEqual(list((self.root / "evidence").iterdir()), [])

    def test_output_symlink_is_rejected(self):
        output = self.root / "linked-evidence"
        output.symlink_to(self.root, target_is_directory=True)
        with self.tool("print('must not run')"):
            with self.assertRaises(Apk2CliError) as caught:
                integrations.run_tool("rea", self.apk, output)
        self.assertEqual(caught.exception.code, "unsafe_output")


class InventoryTests(unittest.TestCase):
    def test_inventory_does_not_execute_tools_or_report_skill_as_binary(self):
        with patch.object(integrations.shutil, "which", return_value=None), patch.object(integrations.subprocess, "Popen") as spawn:
            inventory = integrations.tool_inventory()
        spawn.assert_not_called()
        self.assertEqual(set(inventory), {"leanrea", "rea", "craft-anything", "jadx", "apktool"})
        self.assertIsNone(inventory["leanrea"]["available"])
        self.assertIsNone(inventory["leanrea"]["executable"])
        self.assertFalse(inventory["rea"]["available"])


if __name__ == "__main__":
    unittest.main()
