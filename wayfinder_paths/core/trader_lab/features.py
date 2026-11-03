"""Entry-state features (the server's ``features_sql``) on your own data.

The SQL expects views ``bars_1m(coin, ts, open, high, low, close,
volume_usd)`` and ``funding(coin, ts, rate)`` plus a ``points(coin, ts)``
table of moments to describe. It returns one row per point with z1..z1440
(side-agnostic z-scores of returns), distance from the 24h high/low, volume
ratios, BTC context, hour/weekend and funding — the same columns as the
``entry_states`` dataset, so your own entries compare like for like.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wayfinder_paths.core.trader_lab._safety import check_select


def features_sql(release_dir: Path) -> str:
    meta = json.loads((Path(release_dir) / "manifest.json").read_text())
    sql = meta.get("features_sql")
    if not sql:
        raise ValueError("This release manifest has no features_sql")
    # Untrusted input: only a single SELECT over points / bars_1m / funding.
    return check_select(sql)


def apply(con: Any, points: Any, release_dir: Path) -> Any:
    """Features for ``points`` (a DataFrame/Arrow table with coin, ts) on a
    connection that already has ``bars_1m`` and ``funding`` views (e.g.
    local.connect(..., t0_dir=...), or your own bars registered under those
    names). Returns a pandas DataFrame."""
    con.register("points_input", points)
    try:
        con.execute(
            "CREATE OR REPLACE TEMP VIEW points AS "
            "SELECT DISTINCT coin, ts::TIMESTAMPTZ AS ts FROM points_input"
        )
        return con.sql(features_sql(release_dir)).df()
    finally:
        con.unregister("points_input")
