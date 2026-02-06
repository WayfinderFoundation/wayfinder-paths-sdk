import json
import os
from pathlib import Path
from typing import Any


def load_config_json(
    path: str | Path = "config.json", *, require_exists: bool = False
) -> dict[str, Any]:
    cfg_path = Path(path)
    if not cfg_path.exists():
        if require_exists:
            raise FileNotFoundError(f"Config file not found: {cfg_path}")
        return {}
    try:
        return json.loads(cfg_path.read_text())
    except Exception:
        return {}


CONFIG: dict[str, Any] = load_config_json()


def set_config(config: dict[str, Any]) -> None:
    """Replace the global CONFIG dict in-place.

    This allows code that imported CONFIG at module import time to see updates.
    """
    CONFIG.clear()
    CONFIG.update(config)


def load_config(
    path: str | Path = "config.json", *, require_exists: bool = False
) -> None:
    """Load config from disk into the global CONFIG dict."""
    set_config(load_config_json(path, require_exists=require_exists))


def set_rpc_urls(rpc_urls):
    if "strategy" not in CONFIG:
        CONFIG["strategy"] = {}
    if "rpc_urls" not in CONFIG["strategy"]:
        CONFIG["strategy"]["rpc_urls"] = {}
    CONFIG["strategy"]["rpc_urls"] = rpc_urls


def get_rpc_urls() -> dict[str, Any]:
    return CONFIG.get("strategy", {}).get("rpc_urls", {})


def get_api_base_url() -> str:
    system = CONFIG.get("system", {}) if isinstance(CONFIG, dict) else {}
    api_url = system.get("api_base_url")
    if api_url and isinstance(api_url, str):
        return api_url.strip()
    return "https://wayfinder.ai/api"


def get_api_key() -> str | None:
    system = CONFIG.get("system", {}) if isinstance(CONFIG, dict) else {}
    api_key = system.get("api_key")
    if api_key and isinstance(api_key, str):
        return api_key.strip()
    return os.environ.get("WAYFINDER_API_KEY")


def get_gorlami_base_url() -> str:
    system = CONFIG.get("system", {}) if isinstance(CONFIG, dict) else {}
    url = system.get("gorlami_base_url")
    if not url:
        raise ValueError("gorlami_base_url not configured in system config")
    return url


def get_gorlami_api_key() -> str | None:
    system = CONFIG.get("system", {}) if isinstance(CONFIG, dict) else {}
    api_key = system.get("gorlami_api_key")
    if api_key and isinstance(api_key, str):
        return api_key.strip()
    return os.environ.get("GORLAMI_API_KEY")
