"""Check generated clients and packaged workflows, not just Python functions."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from apk2cli.apk import inspect_apk
from apk2cli.builder import build, initialize
from apk2cli.errors import Apk2CliError

from test_core_review import compiled_manifest, valid_spec


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.apk = self.root / "example app.apk"
        with zipfile.ZipFile(self.apk, "w") as archive:
            archive.writestr("AndroidManifest.xml", compiled_manifest())
        self.spec = valid_spec()
        self.spec_path = self.root / "app.json"
        self.save_spec()

    def save_spec(self):
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")

    def launch(self, folder, *args):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env["APK2CLI_STATE_DIR"] = str(self.root / "private-state")
        return subprocess.run([sys.executable, str(folder / "review"), *args], cwd=self.root,
                              env=env, capture_output=True, text=True, timeout=15)

    def test_relocated_client_runs_without_source_package(self):
        output = self.root / "client"
        result = build(self.apk, "review", self.spec_path, output)
        self.assertEqual(result["auth"], "auth --adb")
        moved = self.root / "moved with spaces" / "client"
        moved.parent.mkdir()
        shutil.move(output, moved)
        help_result = self.launch(moved, "--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("auth", help_result.stdout)
        status = self.launch(moved, "auth", "status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIs(json.loads(status.stdout)["authenticated"], False)
        failure = self.launch(moved, "me")
        self.assertEqual(failure.returncode, 1)
        self.assertEqual(json.loads(failure.stderr)["error"]["code"], "not_authenticated")
        self.assertTrue((moved / "review.cmd").is_file())

    def test_workspace_contains_skills_and_referenced_docs(self):
        output = self.root / "workspace"
        result = initialize(self.apk, "review", output)
        self.assertEqual(result["stage"], "inspect")
        for name in ("inspect-apk", "build-apk-cli", "verify-adb-auth"):
            self.assertTrue((output / ".agents" / "skills" / name / "SKILL.md").is_file())
        for name in ("references.md", "specification.md", "mcp.example.json"):
            self.assertTrue((output / "docs" / name).is_file())
        data = json.loads((output / "workspace.json").read_text())
        self.assertTrue(Path(data["apk"]).samefile(self.apk))
        self.assertEqual(data["apk_sha256"], inspect_apk(self.apk)["apk"]["sha256"])
        self.assertFalse((output / "app.json").exists())

    def test_existing_output_is_preserved(self):
        output = self.root / "existing"
        output.mkdir()
        valuable = output / "notes.txt"
        valuable.write_text("keep me")
        for action in (lambda: build(self.apk, "review", self.spec_path, output),
                       lambda: initialize(self.apk, "review", output)):
            with self.assertRaises(Apk2CliError) as caught:
                action()
            self.assertEqual(caught.exception.code, "output_exists")
            self.assertEqual(valuable.read_text(), "keep me")

    def test_package_name_and_fingerprint_must_match(self):
        for key, value, code in (("package", "dev.other.app", "package_mismatch"),
                                 ("name", "other", "name_mismatch"),
                                 ("apk_sha256", "0" * 64, "apk_mismatch")):
            with self.subTest(field=key):
                self.spec = valid_spec()
                self.spec[key] = value
                self.save_spec()
                output = self.root / "output"
                with self.assertRaises(Apk2CliError) as caught:
                    build(self.apk, "review", self.spec_path, output)
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(output.exists())

    def test_build_binds_accepted_recipe_to_supplied_apk(self):
        expected = inspect_apk(self.apk)["apk"]["sha256"]
        self.spec["apk_sha256"] = expected
        self.save_spec()
        output = self.root / "client"
        build(self.apk, "review", self.spec_path, output)
        built = json.loads((output / "app.json").read_text())
        self.assertEqual(built["apk_sha256"], expected)


if __name__ == "__main__":
    unittest.main()
