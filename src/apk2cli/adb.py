"""Read explicitly configured application files through an authorized ADB device."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import PurePosixPath

from .errors import Apk2CliError

MAX_SOURCE_BYTES = 4 * 1024 * 1024
_PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")
_SERIAL = re.compile(r"[A-Za-z0-9_.:-]+")
_PATH = re.compile(r"[A-Za-z0-9_.@/-]+")


@dataclass(frozen=True)
class Device:
    serial: str
    state: str


def validate_source_path(path: str) -> str:
    """Allow one named file inside an app's supported private directories."""
    if not isinstance(path, str) or not _PATH.fullmatch(path):
        raise Apk2CliError("invalid_source", "Credential source must be a relative application file path.")
    parts = path.split("/")
    if len(parts) < 2 or parts[0] not in {"shared_prefs", "files", "databases"}:
        raise Apk2CliError("invalid_source", "Credential source must be under shared_prefs, files, or databases.")
    if any(part in {"", ".", ".."} for part in parts) or PurePosixPath(path).is_absolute():
        raise Apk2CliError("invalid_source", "Credential source contains an invalid path component.")
    return path


class Adb:
    def __init__(self, executable: str = "adb", *, serial: str | None = None, timeout: float = 20):
        self.executable = executable
        self.requested_serial = serial or os.environ.get("ANDROID_SERIAL")
        self.timeout = timeout
        self.device: Device | None = None
        if self.requested_serial is not None and not _SERIAL.fullmatch(self.requested_serial):
            raise Apk2CliError("invalid_device", "ADB serial contains unsupported characters.")

    def _run(self, args: list[str], *, limit: int = MAX_SOURCE_BYTES + 1) -> bytes:
        try:
            result = subprocess.run(
                [self.executable, *args], capture_output=True, check=False, timeout=self.timeout
            )
        except FileNotFoundError as exc:
            raise Apk2CliError("adb_not_found", "ADB is unavailable. Install Android platform-tools or set --adb-path.") from exc
        except subprocess.TimeoutExpired as exc:
            raise Apk2CliError("adb_timeout", "ADB command timed out; check the selected device connection.") from exc
        except OSError as exc:
            raise Apk2CliError("adb_failed", "ADB could not be started.") from exc
        # Device output can contain credentials, so never attach it to an error.
        if result.returncode != 0:
            raise Apk2CliError("adb_failed", "ADB could not read the requested application data.")
        if len(result.stdout) > limit:
            raise Apk2CliError("source_too_large", "ADB output exceeded the configured size limit.")
        return result.stdout

    def devices(self) -> list[Device]:
        data = self._run(["devices", "-l"], limit=128 * 1024)
        devices = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            columns = line.split()
            if len(columns) < 2 or columns[0] in {"List", "*"}:
                continue
            if columns[1] in {"device", "offline", "unauthorized", "recovery", "sideload", "bootloader"}:
                devices.append(Device(columns[0], columns[1]))
        return devices

    def select(self) -> Device:
        devices = self.devices()
        if self.requested_serial:
            selected = next((d for d in devices if d.serial == self.requested_serial), None)
            if selected is None:
                raise Apk2CliError("device_not_found", "The selected ADB device is not connected.")
        else:
            if not devices:
                raise Apk2CliError("no_device", "Connect a device with USB debugging enabled, then authorize this computer.")
            if len(devices) != 1:
                raise Apk2CliError("multiple_devices", "More than one ADB device is connected. Select one with --serial.")
            selected = devices[0]
        if selected.state == "unauthorized":
            raise Apk2CliError("device_unauthorized", "Unlock the device and accept the USB debugging authorization prompt.")
        if selected.state != "device":
            raise Apk2CliError("device_offline", "The selected device is not ready for ADB commands.")
        self.device = selected
        return selected

    def read_file(self, package: str, path: str, *, root: bool = False) -> bytes:
        if not isinstance(package, str) or not _PACKAGE.fullmatch(package):
            raise Apk2CliError("invalid_package", "An exact Android package name is required.")
        validate_source_path(path)
        device = self.device or self.select()
        # head bounds the remote stream before credentials cross the ADB connection.
        reader = ["head", "-c", str(MAX_SOURCE_BYTES + 1)]
        if root:
            base = shlex.quote(f"/data/user/0/{package}")
            file = shlex.quote(f"/data/user/0/{package}/{path}")
            command = (
                f"base=$(readlink -f {base}) && file=$(readlink -f {file}) && "
                f'case "$file" in "$base"/*) {shlex.join(reader)} "$file";; *) exit 1;; esac'
            )
            remote = ["su", "-c", shlex.quote(command)]
        else:
            remote = ["run-as", package, *reader, path]
        try:
            data = self._run(["-s", device.serial, "exec-out", *remote])
        except Apk2CliError as exc:
            if exc.code == "adb_failed":
                message = (
                    "Root access could not read the configured application file. Check the device's su authorization and source path."
                    if root else
                    "The configured application file is unavailable through run-as. The installed app must be debuggable; "
                    "a production app requires an explicitly authorized root device or an app-supported export."
                )
                raise Apk2CliError("source_unavailable", message) from exc
            raise
        if len(data) > MAX_SOURCE_BYTES:
            raise Apk2CliError("source_too_large", "Credential source exceeds the 4 MiB limit.")
        return data
