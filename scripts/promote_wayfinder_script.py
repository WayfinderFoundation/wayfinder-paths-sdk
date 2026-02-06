#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    cur = Path(__file__).resolve()
    for parent in [cur, *cur.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return Path.cwd()


def _runs_root(repo_root: Path) -> Path:
    candidate = (os.getenv("WAYFINDER_RUNS_DIR") or ".wayfinder_runs").strip()
    p = Path(candidate)
    if not p.is_absolute():
        p = repo_root / p
    return p.resolve(strict=False)


def _library_dir(repo_root: Path, runs_root: Path) -> Path:
    env = os.getenv("WAYFINDER_LIBRARY_DIR", "").strip()
    if env:
        p = Path(env)
        if not p.is_absolute():
            p = repo_root / p
        return p.resolve(strict=False)
    return (runs_root / "library").resolve(strict=False)


_PROTOCOL_PATTERNS: list[tuple[str, str]] = [
    ("hyperliquid", "hyperliquid"),
    ("moonwell", "moonwell"),
    ("hyperlend", "hyperlend"),
    ("pendle", "pendle"),
    ("boros", "boros"),
    ("brap", "brap"),
]


def _sanitize_dirname(raw: str) -> str | None:
    value = str(raw).strip()
    if not value:
        return None
    if "/" in value or "\\" in value:
        return None
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", value)
    safe = safe.strip("._-") or "misc"
    return safe[:80]


def _infer_protocol(src: Path, content: str) -> str | None:
    name = src.stem.lower()
    body = content.lower()
    for pattern, protocol in _PROTOCOL_PATTERNS:
        if pattern in name or pattern in body:
            return protocol
    return None


def _load_index(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return {"scripts": []}
    except json.JSONDecodeError:
        return {"scripts": []}
    return data if isinstance(data, dict) else {"scripts": []}


def _write_index(path: Path, obj: dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _insert_preamble(
    *,
    content: str,
    source_display: str,
    promoted_display: str,
    description: str | None,
) -> str:
    lines = content.splitlines(keepends=True)
    i = 0

    # Preserve shebang/encoding if present.
    if i < len(lines) and lines[i].startswith("#!"):
        i += 1
    if i < len(lines) and ("coding" in lines[i] or "coding:" in lines[i]):
        if lines[i].lstrip().startswith("#") and "coding" in lines[i]:
            i += 1

    preamble_lines = [
        "# Promoted Wayfinder run script.\n",
        f"# Source: {source_display}\n",
        f"# Promoted: {promoted_display}\n",
    ]
    if description:
        preamble_lines.append(f"# Description: {description}\n")
    preamble_lines.append("\n")

    return "".join([*lines[:i], *preamble_lines, *lines[i:]])


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Promote a Wayfinder scratch run script into the local library."
    )
    parser.add_argument(
        "src",
        help="Path to the .py script to promote (must be inside WAYFINDER_RUNS_DIR).",
    )
    parser.add_argument(
        "name",
        nargs="?",
        help="Destination name (defaults to source filename). '.py' is appended if missing.",
    )
    parser.add_argument(
        "--protocol",
        default=None,
        help=(
            "Protocol/category folder under the library (e.g., hyperliquid, moonwell). "
            "If omitted, attempts to infer from filename/content; falls back to 'misc'."
        ),
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help="Move instead of copy (default: copy).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite destination if it already exists.",
    )
    parser.add_argument(
        "--description",
        default=None,
        help="Optional one-line description to include in the promoted script header.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    repo_root = _repo_root()
    runs_root = _runs_root(repo_root)
    library_dir = _library_dir(repo_root, runs_root)
    library_dir.mkdir(parents=True, exist_ok=True)

    src = Path(args.src)
    if not src.is_absolute():
        src = (repo_root / src).resolve(strict=False)
    else:
        src = src.resolve(strict=False)

    try:
        src.relative_to(runs_root)
    except ValueError:
        print(
            f"error: src must be inside WAYFINDER_RUNS_DIR ({runs_root}): {src}",
            file=sys.stderr,
        )
        return 2

    if not src.exists() or not src.is_file():
        print(f"error: src not found: {src}", file=sys.stderr)
        return 2

    if src.suffix.lower() != ".py":
        print("error: only .py scripts can be promoted", file=sys.stderr)
        return 2

    content = src.read_text(encoding="utf-8", errors="replace")

    protocol = _sanitize_dirname(args.protocol or "")
    if args.protocol and not protocol:
        print(
            "error: invalid --protocol (must be a single folder name)", file=sys.stderr
        )
        return 2
    if not protocol:
        protocol = _infer_protocol(src, content) or "misc"

    name = args.name or src.name
    if "/" in name or "\\" in name:
        print("error: name must be a filename (no path separators)", file=sys.stderr)
        return 2
    if not name.lower().endswith(".py"):
        name += ".py"

    dst_dir = (library_dir / protocol).resolve(strict=False)
    try:
        dst_dir.relative_to(library_dir)
    except ValueError:
        print("error: destination folder escapes library dir", file=sys.stderr)
        return 2
    dst_dir.mkdir(parents=True, exist_ok=True)

    dst = (dst_dir / name).resolve(strict=False)
    try:
        dst.relative_to(library_dir)
    except ValueError:
        print("error: destination escapes library dir", file=sys.stderr)
        return 2

    if dst.exists() and not args.force:
        print(
            f"error: destination already exists (use --force): {dst}", file=sys.stderr
        )
        return 2

    try:
        src_display = str(src.relative_to(repo_root))
    except ValueError:
        src_display = str(src)

    now = int(time.time())
    promoted_display = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    dst_content = _insert_preamble(
        content=content,
        source_display=src_display,
        promoted_display=promoted_display,
        description=args.description,
    )
    dst.write_text(dst_content, encoding="utf-8")

    if args.move:
        try:
            src.unlink()
        except OSError:
            pass

    index_path = library_dir / "index.json"
    index_obj = _load_index(index_path)
    scripts = index_obj.get("scripts")
    if not isinstance(scripts, list):
        scripts = []

    try:
        dst_display = str(dst.relative_to(repo_root))
    except ValueError:
        dst_display = str(dst)

    entry = {
        "protocol": protocol,
        "name": name,
        "path": dst_display,
        "source": src_display,
        "promoted_at_unix_s": now,
        "description": args.description,
    }
    scripts.append(entry)
    index_obj["scripts"] = scripts
    _write_index(index_path, index_obj)

    print(dst_display)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
