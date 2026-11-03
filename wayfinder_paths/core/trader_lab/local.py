"""Local DuckDB over a downloaded release, with the server's own views, so
SQL written here runs unchanged in TRADER_LAB_CLIENT.query (and back)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from wayfinder_paths.core.trader_lab._deps import duckdb

_VIEW = re.compile(
    r"CREATE OR REPLACE VIEW (\w+) AS SELECT \* FROM read_parquet\('([^']+)'(.*?)\);"
)


def _views(
    views_sql: str, release_dir: Path, t0_dir: Path | None
) -> list[tuple[str, str, str]]:
    out = []
    for name, path, opts in _VIEW.findall(views_sql):
        if path.startswith("{t0}") and t0_dir is None:
            continue
        resolved = path.replace("{release}", str(release_dir)).replace(
            "{t0}", str(t0_dir) if t0_dir else ""
        )
        out.append((name, resolved, opts))
    return out


def available_views(release_dir: Path, *, t0_dir: Path | None = None) -> list[str]:
    """Views connect() will create: those whose files are present locally."""
    return [name for name, _path, _opts in _resolved(release_dir, t0_dir)]


def _resolved(release_dir: Path, t0_dir: Path | None) -> list[tuple[str, str, str]]:
    meta = json.loads((Path(release_dir) / "manifest.json").read_text())
    return [
        view
        for view in _views(meta.get("views_sql") or "", Path(release_dir), t0_dir)
        if _has_files(view[1])
    ]


def connect(
    release_dir: Path,
    *,
    t0_dir: Path | None = None,
    database: str = ":memory:",
    threads: int | None = None,
    memory_limit: str | None = None,
) -> Any:
    """A DuckDB connection with one view per downloaded dataset.

    release_dir: from bulk.download_release(). t0_dir: optional local copy
    of t0 data (bars_1m, account_day, funding) laid out as on the server;
    without it those views are skipped, as are views whose files weren't
    downloaded (see available_views)."""
    con = duckdb().connect(database)
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    if memory_limit:
        con.execute("SET memory_limit = ?", [memory_limit])
    for name, path, opts in _resolved(Path(release_dir), t0_dir):
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{path}'{opts})"
        )
    return con


def _has_files(pattern: str) -> bool:
    path = Path(pattern)
    anchor = next(
        (i for i, part in enumerate(path.parts) if "*" in part), len(path.parts)
    )
    base = Path(*path.parts[:anchor]) if anchor else Path("/")
    rest = "/".join(path.parts[anchor:])
    return base.exists() and (not rest or any(base.glob(rest)))
