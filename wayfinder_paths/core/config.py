import json
import os
from pathlib import Path
from typing import Any

_CONFIG_ENV_KEYS = ("WAYFINDER_CONFIG_PATH", "WAYFINDER_CONFIG")
_DEFAULT_CONFIG_FILENAME = "config.json"
_WALLET_MNEMONIC_KEY = "wallet_mnemonic"


def _find_project_root(start: Path) -> Path | None:
    cur = start.resolve()
    for parent in [cur, *cur.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return None


def _project_root() -> Path | None:
    return _find_project_root(Path.cwd()) or _find_project_root(Path(__file__).parent)


def resolve_config_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()

    env_path = next(
        (os.getenv(k, "").strip() for k in _CONFIG_ENV_KEYS if os.getenv(k)), ""
    )
    if env_path:
        p = Path(env_path).expanduser()
        if p.is_absolute():
            return p
        root = _project_root()
        return (root / p) if root else p

    root = _project_root()
    return (root / _DEFAULT_CONFIG_FILENAME) if root else Path(_DEFAULT_CONFIG_FILENAME)


def load_config_json(
    path: str | Path | None = None, *, require_exists: bool = False
) -> dict[str, Any]:
    cfg_path = resolve_config_path(path)
    if not cfg_path.exists():
        if require_exists:
            raise FileNotFoundError(f"Config file not found: {cfg_path}")
        return {}
    try:
        return json.loads(cfg_path.read_text())
    except Exception:
        return {}


def write_config_json(path: str | Path | None, config: dict[str, Any]) -> Path:
    cfg_path = resolve_config_path(path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(config, indent=2) + "\n")
    return cfg_path


CONFIG: dict[str, Any] = load_config_json()


def set_config(config: dict[str, Any]) -> None:
    """Replace the global CONFIG dict in-place.

    This allows code that imported CONFIG at module import time to see updates.
    """
    CONFIG.clear()
    CONFIG.update(config)


def load_config(
    path: str | Path | None = None, *, require_exists: bool = False
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
    system = CONFIG.get("system", {})
    api_url = system.get("api_base_url")
    if api_url:
        return str(api_url).strip()
    return "https://wayfinder.ai/api"


def get_api_key() -> str | None:
    system = CONFIG.get("system", {})
    api_key = system.get("api_key")
    if api_key:
        return str(api_key).strip()
    return os.environ.get("WAYFINDER_API_KEY")


def load_wallet_mnemonic(path: str | Path | None = None) -> str | None:
    config = CONFIG if path is None else load_config_json(path)
    value = config.get(_WALLET_MNEMONIC_KEY)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def write_wallet_mnemonic(mnemonic: str, path: str | Path | None = None) -> Path:
    cfg_path = resolve_config_path(path)
    config = load_config_json(cfg_path)
    config[_WALLET_MNEMONIC_KEY] = mnemonic
    write_config_json(cfg_path, config)

    default_path = resolve_config_path()
    if cfg_path.resolve() == default_path.resolve():
        CONFIG[_WALLET_MNEMONIC_KEY] = mnemonic

    return cfg_path


_GORLAMI_PATH = "/blockchain/gorlami"


def get_gorlami_base_url() -> str:
    return f"{get_api_base_url().rstrip('/')}{_GORLAMI_PATH}"
