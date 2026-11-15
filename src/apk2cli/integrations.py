"""Explicit, bounded adapters for the referenced reverse-engineering tools.

These adapters collect static analysis evidence. They neither install tools nor
copy authentication data. Availability means an executable was found on PATH;
each tool still owns its runtime prerequisites.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import zipfile

from .errors import Apk2CliError


TOOL_TIMEOUT = 120.0
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_APK_BYTES = 2 * 1024 * 1024 * 1024

REFERENCES = {
    "leanrea": {
        "repository": "https://github.com/aicodeking/leanrea",
        "revision": "09ba4aa0217f82cd880f855a60b877f59f39b22b",
        "license": "MIT",
        "kind": "skill_reference",
        "description": "Direct CLI inspection workflow; no leanrea executable.",
    },
    "rea": {
        "repository": "https://github.com/morluto/rea",
        "revision": "b3cdbb92ee81d00787dd87aefa43c1cdacbe779c",
        "license": "MIT",
        "binary": "rea",
        "description": "Static APK analysis using REA's configured JADX engine.",
    },
    "craft-anything": {
        "repository": "https://github.com/storytold/craft-anything",
        "revision": "1d8a3b4f60ac8b6cd01b0609cbf2d4d6f65b3da8",
        "license": "MIT OR Apache-2.0",
        "binary": "craft-anything-cli",
        "description": "APK manifest, DEX and package evidence from Craft Anything.",
    },
    "jadx": {
        "repository": "https://github.com/skylot/jadx",
        "binary": "jadx",
        "description": "Direct JADX decompilation, as recommended by leanrea.",
    },
    "apktool": {
        "repository": "https://github.com/iBotPeaches/Apktool",
        "binary": "apktool",
        "description": "Direct resource decoding without source decompilation.",
    },
}


def tool_inventory() -> dict:
    """Discover local executables without launching tools or touching devices."""
    result = {}
    for name, reference in REFERENCES.items():
        item = dict(reference)
        binary = item.get("binary")
        item["executable"] = shutil.which(binary) if binary else None
        item["available"] = bool(item["executable"]) if binary else None
        item["availability_basis"] = "PATH only" if binary else "reference, not an executable"
        if binary:
            item["kind"] = "optional_executable"
        result[name] = item
    return result


def _command(tool: str, executable: str, apk: Path, run: Path) -> list[str]:
    if tool == "rea":
        return [executable, "inspect-android-package", str(apk), "--json"]
    if tool == "craft-anything":
        return [executable, "android-package-show", "--json",
                json.dumps({"path": str(apk)}, ensure_ascii=False), "--output-format", "json"]
    if tool == "jadx":
        return [executable, "-d", str(run / "decompiled"), "--show-bad-code", str(apk)]
    if tool == "apktool":
        return [executable, "d", "--no-src", "-o", str(run / "resources"), str(apk)]
    raise Apk2CliError("unsupported_tool", f"No executable adapter for {tool!r}.")


def _private_file(path: Path):
    """Create a new file without following or overwriting an existing entry."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "wb")


def _snapshot(apk: Path, destination: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with apk.open("rb") as source, _private_file(destination) as target:
        while block := source.read(1024 * 1024):
            total += len(block)
            if total > MAX_APK_BYTES:
                raise Apk2CliError("apk_too_large", "APK exceeds the 2 GiB analysis limit.")
            target.write(block)
            digest.update(block)
    try:
        with zipfile.ZipFile(destination) as archive:
            archive.getinfo("AndroidManifest.xml")
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        raise Apk2CliError("invalid_apk", "Input must be a ZIP APK containing AndroidManifest.xml.") from exc
    return digest.hexdigest(), total


def _stop(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _capture(argv: list[str], run: Path) -> dict:
    """Stream capped output to private files and stop a timed-out process tree."""
    limit = threading.Event()
    sizes = {"stdout": 0, "stderr": 0}
    truncated = {"stdout": False, "stderr": False}
    stream_errors: list[Exception] = []

    def consume(stream, name: str, target) -> None:
        try:
            with stream, target:
                while data := stream.read(65536):
                    remaining = MAX_LOG_BYTES - sizes[name]
                    kept = data[:max(0, remaining)]
                    target.write(kept)
                    sizes[name] += len(kept)
                    if len(data) > remaining:
                        truncated[name] = True
                        limit.set()
        except Exception as exc:  # Propagate disk/read failures after stopping the child.
            stream_errors.append(exc)
            limit.set()

    started = time.monotonic()
    with _private_file(run / "stdout.log") as stdout_file, _private_file(run / "stderr.log") as stderr_file:
        try:
            process = subprocess.Popen(
                argv, cwd=run, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, shell=False, start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            return {"status": "spawn_failed", "returncode": None,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "error": "Executable could not be started; check its runtime prerequisites.",
                    "errno": exc.errno, "log_bytes": sizes, "truncated": truncated}
        readers = [
            threading.Thread(target=consume, args=(process.stdout, "stdout", stdout_file), daemon=True),
            threading.Thread(target=consume, args=(process.stderr, "stderr", stderr_file), daemon=True),
        ]
        for reader in readers:
            reader.start()
        status = None
        try:
            while process.poll() is None:
                if limit.is_set():
                    status = "output_limit"
                    _stop(process)
                    break
                if time.monotonic() - started >= TOOL_TIMEOUT:
                    status = "timeout"
                    _stop(process)
                    break
                try:
                    process.wait(timeout=min(0.05, max(0.001, TOOL_TIMEOUT - (time.monotonic() - started))))
                except subprocess.TimeoutExpired:
                    pass
            process.wait(timeout=5)
            # A parent can finish while a child still owns its pipe. Bound that wait too.
            for reader in readers:
                reader.join(timeout=max(0.01, TOOL_TIMEOUT - (time.monotonic() - started)))
            if any(reader.is_alive() for reader in readers):
                status = "timeout"
        finally:
            # Also reap analysis processes when the user interrupts the CLI.
            if process.poll() is None or any(reader.is_alive() for reader in readers):
                _stop(process)
            process.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=1)
        if stream_errors:
            raise Apk2CliError("evidence_write_failed", "Could not save analysis output.") from stream_errors[0]
        if limit.is_set() and status is None:
            status = "output_limit"
        return {"status": status or ("success" if process.returncode == 0 else "failed"),
                "returncode": process.returncode,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "log_bytes": sizes, "truncated": truncated}


def run_tool(tool: str, apk: Path, output_dir: Path) -> dict:
    """Run one explicitly selected local tool; retain evidence but no inline logs.

    A successful exit records process success, not correctness of the analysis.
    Every run gets a fresh private directory. The input hash identifies the exact
    private APK snapshot passed to the tool, even if the original file changes.
    """
    if tool == "leanrea":
        raise Apk2CliError("skill_not_executable", "leanrea is a skill reference. Select jadx or apktool for its direct-tool workflow.")
    reference = REFERENCES.get(tool)
    if not reference or "binary" not in reference:
        raise Apk2CliError("unsupported_tool", "Select rea, craft-anything, jadx, or apktool.")
    executable = shutil.which(reference["binary"])
    if not executable:
        raise Apk2CliError("tool_unavailable", f"{reference['binary']} is not on PATH. Install/configure it explicitly, then retry.")
    apk = Path(apk).expanduser().absolute()
    if not apk.is_file() or apk.suffix.lower() != ".apk":
        raise Apk2CliError("invalid_apk", "Select an existing .apk file.")
    output_dir = Path(output_dir).expanduser().absolute()
    if output_dir.is_symlink():
        raise Apk2CliError("unsafe_output", "The evidence output directory must not be a symbolic link.")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    run = Path(tempfile.mkdtemp(prefix=f"{tool}-", dir=output_dir))
    try:
        digest, size = _snapshot(apk, run / "input.apk")
    except Exception:
        shutil.rmtree(run)
        raise
    argv = _command(tool, str(Path(executable).absolute()), run / "input.apk", run)
    result = {
        "tool": tool, "reference": reference, "argv": argv,
        "input_sha256": digest, "input_bytes": size,
        "evidence_dir": str(run), "stdout_path": str(run / "stdout.log"),
        "stderr_path": str(run / "stderr.log"), "timeout_seconds": TOOL_TIMEOUT,
        "max_bytes_per_log": MAX_LOG_BYTES,
        "scope": "static analysis; process success does not prove runtime behavior or authentication",
    }
    result.update(_capture(argv, run))
    with _private_file(run / "result.json") as target:
        target.write((json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return result
