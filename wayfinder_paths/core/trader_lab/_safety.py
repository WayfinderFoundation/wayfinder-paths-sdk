"""Treat release listings and manifests as untrusted input.

They come from the API (or a cache file on disk that anything could have
edited), so nothing in them may choose where files land or run arbitrary
SQL locally.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from wayfinder_paths.core.trader_lab._deps import duckdb

RELEASE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATASET = re.compile(r"^[a-z_]{1,40}$")
FILE_PATH = re.compile(r"^([a-z_]{1,40})/([A-Za-z0-9_=.-]{1,120}\.parquet)$")

# features_sql may only read these (plus its own CTEs) and call no file,
# settings or environment functions.
FEATURE_TABLES = frozenset({"points", "bars_1m", "funding"})
DENIED_PREFIXES = ("read_", "glob", "query", "getenv", "duckdb_", "pragma_", "parquet_")


class UnsafeReleaseData(ValueError):
    pass


def check_release(release: Any) -> str:
    if not isinstance(release, str) or not RELEASE.match(release):
        raise UnsafeReleaseData(f"unexpected release id {release!r}")
    return release


def check_file_path(path: Any) -> tuple[str, str]:
    m = FILE_PATH.match(path) if isinstance(path, str) else None
    if not m or ".." in path:
        raise UnsafeReleaseData(f"unexpected dataset file path {path!r}")
    return m.group(1), m.group(2)


def inside(base: Path, target: Path) -> Path:
    resolved = target.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise UnsafeReleaseData(f"{target} escapes {base}")
    return resolved


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _walk(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def check_select(sql: Any, tables: Iterable[str] = FEATURE_TABLES) -> str:
    """Raise unless `sql` is one SELECT over `tables` and its own CTEs."""
    if not isinstance(sql, str) or not sql.strip():
        raise UnsafeReleaseData("empty SQL")
    con = duckdb().connect(config={"enable_external_access": False})
    try:
        tree = json.loads(
            con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
        )
    finally:
        con.close()
    if tree.get("error") or len(tree.get("statements") or []) != 1:
        raise UnsafeReleaseData("release SQL is not a single SELECT")
    allowed = set(tables)
    nodes = list(_walk(tree["statements"][0]))
    for node in nodes:
        cte_map = node.get("cte_map")
        if isinstance(cte_map, dict):
            allowed.update(e["key"].lower() for e in cte_map.get("map", []))
    for node in nodes:
        kind, cls = node.get("type"), node.get("class")
        if kind == "BASE_TABLE" and cls is None:
            name = str(node.get("table_name", "")).lower()
            if (
                node.get("schema_name")
                or node.get("catalog_name")
                or name not in allowed
            ):
                raise UnsafeReleaseData(f"release SQL reads {name!r}")
        elif kind == "TABLE_FUNCTION" and cls is None:
            fn = str((node.get("function") or {}).get("function_name", "")).lower()
            if fn not in ("range", "generate_series", "unnest"):
                raise UnsafeReleaseData(f"release SQL calls {fn!r}")
        elif cls in ("FUNCTION", "WINDOW"):
            if str(node.get("function_name", "")).lower().startswith(DENIED_PREFIXES):
                raise UnsafeReleaseData(
                    f"release SQL calls {node.get('function_name')!r}"
                )
        elif kind == "SHOW_REF":
            raise UnsafeReleaseData("release SQL uses SHOW/DESCRIBE")
    return sql
