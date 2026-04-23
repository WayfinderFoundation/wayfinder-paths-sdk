"""Shared artifact I/O for pipeline workers.

Every pipeline worker writes exactly one JSON artifact per phase.  Use
``write_artifact`` instead of hand-rolling ``json.dumps`` so that:

* numpy scalars are converted automatically,
* the artifact-must-be-a-dict contract is enforced once, and
* the output format is consistent across all workers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class _ArtifactEncoder(json.JSONEncoder):
    """JSON encoder that coerces numpy types to Python builtins."""

    def default(self, o: object) -> Any:
        # Deferred import so numpy remains optional at the SDK level.
        try:
            import numpy as np
        except ImportError:
            return super().default(o)

        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.floating):
            return float(o)
        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        return super().default(o)


def write_artifact(path: str | Path, payload: dict[str, Any]) -> Path:
    """Serialize *payload* as JSON and write it to *path*.

    Parameters
    ----------
    path:
        Destination file.  Parent directories are created if needed.
    payload:
        Must be a ``dict`` (the pipeline artifact contract).

    Returns
    -------
    The resolved :class:`~pathlib.Path` that was written.
    """
    if not isinstance(payload, dict):
        raise TypeError(
            f"Artifact payload must be a dict, got {type(payload).__name__}"
        )
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(payload, indent=2, cls=_ArtifactEncoder) + "\n",
        encoding="utf-8",
    )
    return dest
