from __future__ import annotations

from typing import Any

INSTALL_HINT = (
    "Trader Lab local analysis needs the optional extra: "
    "pip install 'wayfinder-paths[trader-lab]' (duckdb, pyarrow)"
)


def duckdb() -> Any:
    try:
        import duckdb as _duckdb
    except ImportError as exc:  # pragma: no cover - depends on install
        raise ImportError(INSTALL_HINT) from exc
    return _duckdb
