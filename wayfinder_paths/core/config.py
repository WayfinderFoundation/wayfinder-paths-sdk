import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths.core.api_origin import (
    on_shells_instance,
    pinned_api_base_url,
    validate_api_base_url,
)

_CONFIG_ENV_KEYS = ("WAYFINDER_CONFIG_PATH", "WAYFINDER_CONFIG")
_DEFAULT_CONFIG_FILENAME = "config.json"
_DEFAULT_API_BASE_URL = "https://wayfinder.ai/api/v1"
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


def _drop_volume_endpoint_overrides(
    config: dict[str, Any], cfg_path: Path
) -> dict[str, Any]:
    # On a Shells instance config.json lives on the user volume and survives
    # reboots, so endpoint overrides written there must not steer the SDK.
    # In-process overrides (set_rpc_urls, used by Gorlami forks) still apply.
    if not on_shells_instance():
        return config
    dropped: list[str] = []
    for section, key in (("system", "paths_api_base_url"), ("strategy", "rpc_urls")):
        block = config.get(section)
        if isinstance(block, dict) and block.pop(key, None) is not None:
            dropped.append(f"{section}.{key}")
    if dropped:
        logger.warning(
            "Ignoring {} from {}: endpoint overrides in config.json are not "
            "trusted inside a Wayfinder Shells instance",
            ", ".join(dropped),
            cfg_path,
        )
    return config


# Where CONFIG was last loaded from, named in error messages about its values.
_config_path: Path = resolve_config_path()
CONFIG: dict[str, Any] = _drop_volume_endpoint_overrides(
    load_config_json(_config_path), _config_path
)


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
    global _config_path
    cfg_path = resolve_config_path(path)
    if not cfg_path.exists() and path is not None and not require_exists:
        return
    set_config(
        _drop_volume_endpoint_overrides(
            load_config_json(cfg_path, require_exists=require_exists), cfg_path
        )
    )
    _config_path = cfg_path


def set_rpc_urls(rpc_urls):
    if "strategy" not in CONFIG:
        CONFIG["strategy"] = {}
    if "rpc_urls" not in CONFIG["strategy"]:
        CONFIG["strategy"]["rpc_urls"] = {}
    CONFIG["strategy"]["rpc_urls"] = rpc_urls


def get_rpc_urls() -> dict[str, Any]:
    return CONFIG.get("strategy", {}).get("rpc_urls", {})


def _validate_config_url(value: Any, key: str, default: str) -> str:
    return validate_api_base_url(
        str(value),
        label=f"config.json key {key} ({_config_path})",
        fix=f"correct {key} in {_config_path}, or delete it to use {default}",
    )


def get_api_base_url() -> str:
    if pinned := pinned_api_base_url():
        return pinned
    api_url = CONFIG.get("system", {}).get("api_base_url")
    if not api_url:
        return _DEFAULT_API_BASE_URL
    return _validate_config_url(api_url, "system.api_base_url", _DEFAULT_API_BASE_URL)


def get_paths_api_base_url() -> str:
    system = CONFIG.get("system", {})
    paths_url = system.get("paths_api_base_url")
    if paths_url:
        return _validate_config_url(
            paths_url, "system.paths_api_base_url", "the system.api_base_url host"
        )

    env_url = os.environ.get("WAYFINDER_PATHS_API_URL")
    if env_url:
        return validate_api_base_url(
            env_url,
            label="Environment variable WAYFINDER_PATHS_API_URL",
            fix="set WAYFINDER_PATHS_API_URL to an allowed URL, or unset it",
        )

    # Fallback: derive from api_base_url by stripping known API path suffixes
    base = get_api_base_url()
    for suffix in ("/api/v1", "/api"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def allow_local_wallets() -> bool:
    system = CONFIG.get("system", {})
    return bool(system.get("allow_local_wallets", True))


def get_api_key() -> str | None:
    api_key = load_config_json().get("system", {}).get("api_key")
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


def get_etherscan_api_key() -> str | None:
    system = CONFIG.get("system", {})
    api_key = system.get("etherscan_api_key")
    if api_key:
        return str(api_key).strip()
    return os.environ.get("ETHERSCAN_API_KEY")


def get_openai_credentials() -> dict[str, str | None]:
    """OpenAI API key + organization from ``system.openai`` (env fallback).

    Mirrors the ``system.<provider>`` config pattern (see ``get_api_key`` /
    ``get_etherscan_api_key``). The eval judge resolves these into the environment so
    opencode's OpenAI provider can authenticate; credentials stay in the config, never a
    tracked file.
    """
    openai = CONFIG.get("system", {}).get("openai", {})
    key = openai.get("api_key") or os.environ.get("OPENAI_API_KEY")
    org = openai.get("organization") or os.environ.get("OPENAI_ORGANIZATION")
    return {
        "api_key": str(key).strip() if key else None,
        "organization": str(org).strip() if org else None,
    }


def is_opencode_instance() -> bool:
    return bool(os.environ.get("OPENCODE_INSTANCE_ID"))


def get_opencode_instance_id() -> str:
    if not (instance_id := os.environ.get("OPENCODE_INSTANCE_ID")):
        raise RuntimeError(
            "No OPENCODE_INSTANCE_ID set, this is unexpected as the caller assumes this is an OpenCode environment."
        )
    return instance_id
