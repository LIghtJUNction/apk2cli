"""User and agent entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .apk import inspect_apk
from .builder import build, initialize
from .errors import Apk2CliError
from .integrations import run_tool, tool_inventory
from .spec import load_spec


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="apk2cli", description="Turn an APK's observed behavior into a verified command-line client with AI workflows.")
    root.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = root.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Inspect an APK without executing or extracting it")
    inspect.add_argument("apk", type=Path)
    inspect.add_argument("--json", action="store_true", help="Emit JSON (also the default)")
    init = commands.add_parser("init", help="Create an AI workspace with analysis and built-in .agents/skills")
    init.add_argument("apk", type=Path)
    init.add_argument("--name", required=True)
    init.add_argument("--out", type=Path, required=True)
    generate = commands.add_parser("build", help="Build a standalone CLI from a verified app specification")
    generate.add_argument("apk", type=Path)
    generate.add_argument("--name", required=True)
    generate.add_argument("--spec", type=Path, required=True)
    generate.add_argument("--out", type=Path, required=True)
    validate = commands.add_parser("validate", help="Validate an app specification before building")
    validate.add_argument("spec", type=Path)
    tools = commands.add_parser("tools", help="Discover or explicitly run optional reference tools")
    actions = tools.add_subparsers(dest="action", required=True)
    actions.add_parser("doctor", help="Show installed analysis tools and upstream references")
    run = actions.add_parser("run", help="Run one installed tool and save local analysis evidence")
    run.add_argument("tool", choices=["rea", "craft-anything", "jadx", "apktool"])
    run.add_argument("apk", type=Path)
    run.add_argument("--out", required=True, type=Path)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "inspect":
            result = inspect_apk(args.apk)
        elif args.command == "init":
            result = initialize(args.apk, args.name, args.out)
        elif args.command == "build":
            result = build(args.apk, args.name, args.spec, args.out)
        elif args.command == "validate":
            spec = load_spec(args.spec)
            result = {"valid": True, "name": spec["name"], "package": spec["package"], "commands": len(spec["commands"])}
        elif args.action == "doctor":
            result = tool_inventory()
        else:
            result = run_tool(args.tool, args.apk, args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "tools" and args.action == "run" and result.get("status") != "success":
            return 1
        return 0
    except Apk2CliError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message}}), file=sys.stderr)
        return 1
    except OSError:
        print(json.dumps({"error": {"code": "filesystem_error", "message": "Cannot read or write a required path. Check permissions and available disk space."}}), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
