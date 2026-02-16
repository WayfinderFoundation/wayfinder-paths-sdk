from __future__ import annotations

from typing import Any


def chunks(seq: list[Any], n: int) -> list[list[Any]]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]
