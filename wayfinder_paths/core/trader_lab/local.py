"""Local DuckDB over a downloaded release, with the same views as the server
(one per dataset, plus t0 data if you have it), so SQL written here runs
unchanged in TRADER_LAB_CLIENT.query (and back).

Views are built from validated dataset names, never by executing SQL text
from the manifest: the manifest is untrusted input (see _safety.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wayfinder_paths.core.trader_lab._deps import duckdb
from wayfinder_paths.core.trader_lab._safety import DATASET, sql_literal

T0_DATASETS = ("account_day", "bars_1m", "funding")


def _view_specs(release_dir: Path, t0_dir: Path | None) -> list[tuple[str, str, bool]]:
    """(view name, parquet glob, hive_partitioning) for files present locally."""
    meta = json.loads((release_dir / "manifest.json").read_text())
    specs = []
    for name in meta.get("datasets") or []:
        if (
            isinstance(name, str)
            and DATASET.match(name)
            and any((release_dir / name).glob("*.parquet"))
        ):
            specs.append((name, str(release_dir / name / "*.parquet"), False))
    if t0_dir is not None:
        for name in T0_DATASETS:
            if any((t0_dir / name).glob("*/part.parquet")):
                specs.append((name, str(t0_dir / name / "*" / "part.parquet"), True))
    return specs


def available_views(release_dir: Path, *, t0_dir: Path | None = None) -> list[str]:
    """Views connect() will create: those whose files are present locally."""
    return [name for name, _glob, _hive in _view_specs(Path(release_dir), t0_dir)]


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
    of t0 data (bars_1m, account_day, funding) laid out as on the server
    (``<t0_dir>/<name>/date=YYYY-MM-DD/part.parquet``); without it those
    views are skipped, as are datasets whose files weren't downloaded."""
    con = duckdb().connect(
        database,
        config={
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
        },
    )
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    if memory_limit:
        con.execute("SET memory_limit = ?", [memory_limit])
    for name, pattern, hive in _view_specs(Path(release_dir), t0_dir):
        opts = ", hive_partitioning = true" if hive else ""
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_parquet({sql_literal(pattern)}{opts})"
        )
    return con
