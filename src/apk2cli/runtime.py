"""Runtime copied into each generated CLI; depends only on Python's stdlib."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from urllib.parse import quote

from . import __version__
from .auth import authenticate, get_token, logout, status
from .errors import Apk2CliError
from .http import request_json
from .spec import load_spec


def emit(value, *, token: str | None = None, file=None) -> None:
    def redact(item):
        if isinstance(item, str):
            return item.replace(token, "[redacted]") if token else item
        if isinstance(item, dict):
            return {redact(key): redact(child) for key, child in item.items()}
        if isinstance(item, list):
            return [redact(child) for child in item]
        return item
    text = json.dumps(redact(value), ensure_ascii=False, indent=2, allow_nan=False)
    print(text, file=file or sys.stdout)


def _state_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", default=argparse.SUPPRESS,
                        help="Private session directory (default: APK2CLI_STATE_DIR or user state directory)")


def _boolean(value: str) -> bool:
    if value.lower() not in {"true", "false"}:
        raise argparse.ArgumentTypeError("expected true or false")
    return value.lower() == "true"


def _number(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a number") from exc
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("expected a finite number")
    return number


def make_parser(spec: dict) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=spec["name"], description=f"Command-line access to {spec['package']}.")
    parser.add_argument("--version", action="version", version=f"%(prog)s (apk2cli {__version__})")
    _state_option(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    auth = commands.add_parser("auth", help="Import and manage a verified app session")
    _state_option(auth)
    auth.add_argument("--adb", action="store_true", help="Import the phone app's existing session and verify it")
    auth.add_argument("--serial", help="ADB device serial (or ANDROID_SERIAL)")
    auth.add_argument("--adb-path", default="adb", help="ADB executable")
    auth.add_argument("--root", action="store_true", help="Read configured app files with su on an already rooted device")
    actions = auth.add_subparsers(dest="auth_action")
    show = actions.add_parser("status", help="Show account and import metadata without the credential")
    _state_option(show)
    show.add_argument("--verify", action="store_true", help="Recheck the account with the server")
    clear = actions.add_parser("logout", help="Remove this CLI's local session")
    _state_option(clear)
    for command in spec["commands"]:
        p = commands.add_parser(command["name"], help=command.get("help", ""))
        _state_option(p)
        for parameter in command.get("parameters", []):
            kind = {"string": str, "integer": int, "number": _number, "boolean": _boolean}[parameter.get("type", "string")]
            kwargs = {"dest": "_param_" + parameter["name"], "type": kind,
                      "help": parameter.get("help", f"{parameter['in']} parameter")}
            if parameter["in"] == "path" or parameter.get("required", False):
                kwargs["required"] = True
            kwargs["default"] = parameter.get("default", argparse.SUPPRESS)
            p.add_argument("--" + parameter["name"], **kwargs)
    return parser


def run(spec_path: Path | str, argv: list[str] | None = None) -> int:
    try:
        spec = load_spec(spec_path)
        parser = make_parser(spec)
        args = parser.parse_args(argv)
        state_dir = getattr(args, "state_dir", None)
        if args.command == "auth":
            if args.auth_action:
                if args.adb or args.root or args.serial:
                    parser.error("auth status/logout cannot be combined with import options")
                result = status(spec, state_dir=state_dir, verify=args.verify) if args.auth_action == "status" else logout(spec, state_dir=state_dir)
            elif args.adb:
                result = authenticate(spec, state_dir=state_dir, adb_path=args.adb_path, serial=args.serial, root=args.root)
            else:
                parser.error("use auth --adb, auth status, or auth logout")
            emit(result)
            return 0
        command = next(c for c in spec["commands"] if c["name"] == args.command)
        path, query, body = command["path"], {}, {}
        values = vars(args)
        for parameter in command.get("parameters", []):
            name = parameter["name"]
            destination = "_param_" + name
            if destination not in values:
                continue
            value = values[destination]
            if parameter["in"] == "path":
                path = path.replace("{" + name + "}", quote(str(value), safe=""))
            elif parameter["in"] == "query":
                query[parameter.get("key", name)] = str(value).lower() if isinstance(value, bool) else value
            else:
                body[parameter.get("key", name)] = value
        token = get_token(spec, state_dir=state_dir)
        result = request_json(spec, command["method"], path, token=token, params=query, body=body or None)
        emit(result, token=token)
        return 0
    except Apk2CliError as exc:
        emit({"error": {"code": exc.code, "message": exc.message}}, file=sys.stderr)
        return 1
    except OSError:
        emit({"error": {"code": "filesystem_error", "message": "Cannot access the local session directory. Check its path, permissions and available disk space."}}, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        emit({"error": {"code": "interrupted", "message": "Operation interrupted."}}, file=sys.stderr)
        return 130
