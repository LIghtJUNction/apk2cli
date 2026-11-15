"""Private, origin-bound local sessions for generated commands."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import Apk2CliError


def default_state_dir() -> Path:
    configured = os.environ.get("APK2CLI_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "apk2cli"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "apk2cli"


def parse_expiry(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        expiry = float(value)
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Timezone is required")
            expiry = parsed.timestamp()
        except (ValueError, OverflowError) as exc:
            raise Apk2CliError("invalid_expiry", "The session expiry must be an epoch timestamp or a timezone-qualified ISO timestamp.") from exc
    else:
        raise Apk2CliError("invalid_expiry", "The session expiry has an unsupported type.")
    if not (0 < expiry < 253402300800):
        raise Apk2CliError("invalid_expiry", "The session expiry is outside the supported timestamp range.")
    return expiry


def require_live(record: dict[str, Any]) -> None:
    if record.get("expires_at") is not None and parse_expiry(record["expires_at"]) <= time.time():
        raise Apk2CliError("session_expired", "The imported session has expired. Log in on the device and run auth --adb again.")


class SessionStore:
    def __init__(self, spec: dict[str, Any], state_dir: str | Path | None = None):
        self.binding = {key: spec[key] for key in ("name", "package", "base_url")}
        fingerprint = hashlib.sha256(json.dumps(self.binding, sort_keys=True).encode()).hexdigest()
        self.directory = Path(state_dir).expanduser() if state_dir is not None else default_state_dir()
        self.path = self.directory / f"{fingerprint}.json"

    def _check_directory(self, *, create: bool = False) -> bool:
        if create:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.is_symlink():
            raise Apk2CliError("unsafe_session_directory", "The session directory must not be a symlink.")
        if not self.directory.exists():
            return False
        if not self.directory.is_dir():
            raise Apk2CliError("unsafe_session_directory", "The configured session path is not a directory.")
        if os.name != "nt" and self.directory.stat().st_mode & 0o077:
            raise Apk2CliError("unsafe_session_directory", "Use a private session directory with permissions 0700.")
        return True

    def save(self, record: dict[str, Any]) -> None:
        if any(record.get(key) != value for key, value in self.binding.items()):
            raise Apk2CliError("session_mismatch", "The session does not belong to this application and API origin.")
        require_live(record)
        self._check_directory(create=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".session-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                if os.name != "nt":
                    os.fchmod(handle.fileno(), 0o600)
                json.dump(record, handle, ensure_ascii=False, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def load(self, *, required: bool = True) -> dict[str, Any] | None:
        if not self._check_directory() or not self.path.exists():
            if required:
                raise Apk2CliError("not_authenticated", "No imported session exists. Run auth --adb first.")
            return None
        if self.path.is_symlink():
            raise Apk2CliError("unsafe_session_file", "The session file must not be a symlink.")
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
            raise Apk2CliError("invalid_session", "The stored session is not a valid session file.")
        if os.name != "nt" and info.st_mode & 0o077:
            raise Apk2CliError("unsafe_session_file", "The session file must have permissions 0600.")
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except (UnicodeError, ValueError, OSError) as exc:
            raise Apk2CliError("invalid_session", "The stored session could not be read.") from exc
        if not isinstance(record, dict) or record.get("schema_version") != 1:
            raise Apk2CliError("invalid_session", "The stored session has an unsupported format.")
        if any(record.get(key) != value for key, value in self.binding.items()):
            raise Apk2CliError("session_mismatch", "The session does not belong to this application and API origin.")
        token = record.get("token")
        if not isinstance(token, str) or not token or len(token) > 16384 or any(c in token for c in "\r\n\0"):
            raise Apk2CliError("invalid_session", "The stored credential is invalid.")
        require_live(record)
        return record

    def delete(self) -> bool:
        if not self._check_directory():
            return False
        existed = self.path.exists() or self.path.is_symlink()
        self.path.unlink(missing_ok=True)
        return existed


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
