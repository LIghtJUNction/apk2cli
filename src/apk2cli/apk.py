"""Bounded APK inventory and Android binary XML manifest reader.

The binary layout follows Android's ResourceTypes.h. No APK code is executed.
"""

from __future__ import annotations

import hashlib
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .errors import Apk2CliError

ANDROID = "http://schemas.android.com/apk/res/android"
MAX_MANIFEST = 8 * 1024 * 1024
MAX_SCAN = 32 * 1024 * 1024
MAX_FILES = 100_000


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _strings(chunk: bytes) -> list[str]:
    header_size = _u16(chunk, 2)
    count = _u32(chunk, 8)
    flags = _u32(chunk, 16)
    start = _u32(chunk, 20)
    if header_size < 28 or count > 100_000 or header_size + count * 4 > len(chunk):
        raise ValueError("invalid string pool")
    if start < header_size + count * 4 or start > len(chunk):
        raise ValueError("invalid string data")

    def length(pos: int, utf8: bool) -> tuple[int, int]:
        if utf8:
            n = chunk[pos]
            return (((n & 0x7F) << 8) | chunk[pos + 1], pos + 2) if n & 0x80 else (n, pos + 1)
        n = _u16(chunk, pos)
        return (((n & 0x7FFF) << 16) | _u16(chunk, pos + 2), pos + 4) if n & 0x8000 else (n, pos + 2)

    result = []
    for i in range(count):
        pos = start + _u32(chunk, header_size + i * 4)
        if flags & 0x100:
            _, pos = length(pos, True)
            n, pos = length(pos, True)
            if pos + n >= len(chunk) or chunk[pos + n] != 0:
                raise ValueError("invalid UTF-8 string")
            result.append(chunk[pos:pos + n].decode("utf-8"))
        else:
            n, pos = length(pos, False)
            if pos + n * 2 + 2 > len(chunk) or _u16(chunk, pos + n * 2):
                raise ValueError("invalid UTF-16 string")
            result.append(chunk[pos:pos + n * 2].decode("utf-16le"))
    return result


def parse_manifest(data: bytes) -> ET.Element:
    """Read compiled AXML; text XML is also accepted for authoring fixtures."""
    try:
        if data.lstrip().startswith(b"<"):
            if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                raise ValueError("entities are not supported")
            return ET.fromstring(data)
        if len(data) < 8 or _u16(data, 0) != 3 or _u32(data, 4) != len(data):
            raise ValueError("invalid binary XML header")
        offset = _u16(data, 2)
        if offset < 8:
            raise ValueError("invalid header size")
        pool: list[str] = []
        stack: list[ET.Element] = []
        root = None

        def string(index: int) -> str:
            if index == 0xFFFFFFFF:
                return ""
            if index >= len(pool):
                raise ValueError("string index outside pool")
            return pool[index]

        while offset < len(data):
            kind, head, size = struct.unpack_from("<HHI", data, offset)
            if size < head or head < 8 or offset + size > len(data):
                raise ValueError("invalid chunk bounds")
            chunk = data[offset:offset + size]
            if kind == 1:
                pool = _strings(chunk)
            elif kind == 0x0102:
                if head < 16 or size < head + 20:
                    raise ValueError("invalid element")
                tag = string(_u32(chunk, head + 4))
                attr_start, attr_size, attr_count = struct.unpack_from("<HHH", chunk, head + 8)
                if attr_start < 20 or attr_size < 20 or head + attr_start + attr_size * attr_count > size:
                    raise ValueError("invalid attributes")
                attributes = {}
                for i in range(attr_count):
                    pos = head + attr_start + i * attr_size
                    ns, name, raw = struct.unpack_from("<III", chunk, pos)
                    dtype = chunk[pos + 15]
                    value = _u32(chunk, pos + 16)
                    if raw != 0xFFFFFFFF:
                        text = string(raw)
                    elif dtype == 3:
                        text = string(value)
                    elif dtype == 0x12:
                        text = "true" if value else "false"
                    elif dtype in (0x10, 0x11):
                        text = str(value)
                    else:
                        text = f"@0x{value:08x}"
                    key = string(name)
                    namespace = string(ns)
                    if namespace:
                        key = "{" + namespace + "}" + key
                    attributes[key] = text
                element = ET.Element(tag, attributes)
                if stack:
                    stack[-1].append(element)
                elif root is None:
                    root = element
                else:
                    raise ValueError("multiple roots")
                stack.append(element)
                if len(stack) > 128:
                    raise ValueError("manifest nesting too deep")
            elif kind == 0x0103:
                if not stack or head < 16 or size < head + 8 or string(_u32(chunk, head + 4)) != stack[-1].tag:
                    raise ValueError("unbalanced elements")
                stack.pop()
            offset += size
        if root is None or stack:
            raise ValueError("incomplete manifest")
        return root
    except (ValueError, IndexError, struct.error, UnicodeError, ET.ParseError) as exc:
        raise Apk2CliError("invalid_manifest", "APK contains an invalid or unsupported Android manifest.") from exc


def _manifest_info(root: ET.Element) -> dict:
    if root.tag != "manifest" or not root.get("package"):
        raise Apk2CliError("invalid_manifest", "Manifest must identify an Android package.")
    android = lambda el, key: el.get(f"{{{ANDROID}}}{key}") if el is not None else None
    app = root.find("application")
    sdk = root.find("uses-sdk")
    package = root.attrib["package"]
    launchers = []
    if app is not None:
        for component in [*app.findall("activity"), *app.findall("activity-alias")]:
            for intent in component.findall("intent-filter"):
                main = any(android(x, "name") == "android.intent.action.MAIN" for x in intent.findall("action"))
                launcher = any(android(x, "name") == "android.intent.category.LAUNCHER" for x in intent.findall("category"))
                if main and launcher:
                    name = android(component, "name") or ""
                    launchers.append(package + name if name.startswith(".") else name if "." in name else package + "." + name)
    return {
        "package": package,
        "version_name": android(root, "versionName"),
        "version_code": android(root, "versionCode"),
        "min_sdk": android(sdk, "minSdkVersion"),
        "target_sdk": android(sdk, "targetSdkVersion"),
        "debuggable": android(app, "debuggable") == "true",
        "permissions": sorted({android(p, "name") for p in root.findall("uses-permission") if android(p, "name")}),
        "launcher_activities": sorted(set(launchers)),
    }


def inspect_apk(path: Path | str) -> dict:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise Apk2CliError("apk_not_found", "APK file does not exist.")
    if path.stat().st_size > 2 * 1024**3:
        raise Apk2CliError("apk_too_large", "APK exceeds the 2 GiB inspection limit.")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES:
                raise Apk2CliError("apk_too_large", "APK contains too many archive entries.")
            names = [entry.filename for entry in entries]
            if names.count("AndroidManifest.xml") != 1:
                raise Apk2CliError("invalid_apk", "APK must contain exactly one AndroidManifest.xml.")
            info = archive.getinfo("AndroidManifest.xml")
            if info.file_size > MAX_MANIFEST:
                raise Apk2CliError("invalid_manifest", "APK manifest exceeds the size limit.")
            manifest = _manifest_info(parse_manifest(archive.read(info)))
            urls: dict[str, str] = {}
            scanned = 0
            candidates = [e for e in entries if re.fullmatch(r"classes\d*\.dex", e.filename) or e.filename.startswith("assets/") and e.filename.endswith((".json", ".xml", ".txt", ".js"))]
            for entry in candidates:
                if scanned >= MAX_SCAN:
                    break
                with archive.open(entry) as stream:
                    data = stream.read(min(MAX_SCAN - scanned, entry.file_size))
                scanned += len(data)
                for match in re.finditer(rb"https?://[A-Za-z0-9._~:/?#@!$&()*+,;=%-]{4,2048}", data):
                    try:
                        parsed = urlsplit(match.group().decode("ascii"))
                        if not parsed.hostname or parsed.username or parsed.password:
                            continue
                        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
                        if len(urls) < 200:
                            urls.setdefault(url, entry.filename)
                    except ValueError:
                        continue
            result = {
                "schema_version": 1,
                "apk": {"path": str(path), "size_bytes": path.stat().st_size},
                "manifest": manifest,
                "archive": {"entry_count": len(entries), "dex_files": [n for n in names if re.fullmatch(r"classes\d*\.dex", n)],
                            "native_abis": sorted({n.split("/")[1] for n in names if n.startswith("lib/") and n.endswith(".so") and len(n.split("/")) >= 3})},
                "url_candidates": [{"url": u, "source": source, "status": "unverified"} for u, source in sorted(urls.items())],
                "scan": {"bytes_scanned": scanned, "byte_limit": MAX_SCAN, "complete": sum(e.file_size for e in candidates) <= MAX_SCAN},
            }
    except (zipfile.BadZipFile, KeyError, RuntimeError, OSError) as exc:
        raise Apk2CliError("invalid_apk", "APK is not a readable Android ZIP archive.") from exc
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    result["apk"]["sha256"] = digest.hexdigest()
    return result
