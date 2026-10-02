"""One effective parameter source for installed Path jobs and their candidates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PARAMS_PATH = "workspace/config/params.json"


def effective_path_params(root: Path, source: dict[str, Any]) -> dict[str, Any]:
    # Old installed jobs only carried params in the pin. An existing workspace
    # file is authoritative (including {}); corruption must never restore old
    # spend permissions by silently falling back to the pin.
    path = root / PARAMS_PATH
    value = json.loads(path.read_text()) if path.exists() else source.get("params", {})
    if not isinstance(value, dict):
        raise ValueError("Path params must be a JSON object")
    return value
