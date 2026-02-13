from __future__ import annotations

import copy
from pathlib import Path

import pytest

import wayfinder_paths.core.config as config


@pytest.fixture
def restore_global_config() -> None:
    original = copy.deepcopy(config.CONFIG)
    yield
    config.set_config(original)


def test_resolve_config_path_defaults_to_repo_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("WAYFINDER_CONFIG_PATH", raising=False)
    monkeypatch.delenv("WAYFINDER_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)

    repo_root = Path(__file__).resolve().parents[2]
    assert config.resolve_config_path() == repo_root / "config.json"


def test_resolve_config_path_env_relative_is_repo_relative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WAYFINDER_CONFIG_PATH", "config.example.json")
    monkeypatch.chdir(tmp_path)

    repo_root = Path(__file__).resolve().parents[2]
    assert config.resolve_config_path() == repo_root / "config.example.json"


def test_load_config_json_supports_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WAYFINDER_CONFIG_PATH", "config.example.json")
    monkeypatch.chdir(tmp_path)

    cfg = config.load_config_json()
    assert isinstance(cfg.get("strategy"), dict)
    rpc_urls = cfg["strategy"].get("rpc_urls")
    assert isinstance(rpc_urls, dict)


@pytest.mark.asyncio
async def test_web3s_fallback_to_rpc_proxy(
    restore_global_config: None,
) -> None:
    config.set_config(
        {
            "system": {
                "api_base_url": "https://strategies.wayfinder.ai/api/v1",
                "api_key": "wk_test",
            },
            "strategy": {"rpc_urls": {}},
        }
    )

    from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE, CHAIN_ID_HYPEREVM
    from wayfinder_paths.core.utils.web3 import web3s_from_chain_id

    async with web3s_from_chain_id(CHAIN_ID_BASE) as web3s:
        uri = web3s[0].provider.endpoint_uri
        assert uri == "https://strategies.wayfinder.ai/api/v1/blockchain/rpc/8453/"
        assert web3s[0].provider._request_kwargs["headers"]["X-API-KEY"] == "wk_test"

    async with web3s_from_chain_id(CHAIN_ID_HYPEREVM) as web3s:
        uri = web3s[0].provider.endpoint_uri
        assert uri == "https://strategies.wayfinder.ai/api/v1/blockchain/rpc/999/"
        assert web3s[0].provider._request_kwargs["headers"]["X-API-KEY"] == "wk_test"
        assert hasattr(web3s[0], "hype")


@pytest.mark.asyncio
async def test_user_rpcs_override_proxy(restore_global_config: None) -> None:
    config.set_config(
        {
            "system": {
                "api_base_url": "https://strategies.wayfinder.ai/api/v1",
                "api_key": "wk_test",
            },
            "strategy": {"rpc_urls": {"8453": ["https://custom-rpc.example.com"]}},
        }
    )

    from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE, CHAIN_ID_ARBITRUM
    from wayfinder_paths.core.utils.web3 import web3s_from_chain_id

    async with web3s_from_chain_id(CHAIN_ID_BASE) as web3s:
        assert web3s[0].provider.endpoint_uri == "https://custom-rpc.example.com"
        assert "X-API-KEY" not in web3s[0].provider._request_kwargs.get("headers", {})

    async with web3s_from_chain_id(CHAIN_ID_ARBITRUM) as web3s:
        uri = web3s[0].provider.endpoint_uri
        assert uri == "https://strategies.wayfinder.ai/api/v1/blockchain/rpc/42161/"
        assert web3s[0].provider._request_kwargs["headers"]["X-API-KEY"] == "wk_test"


def test_web3s_accept_int_rpc_url_keys(restore_global_config: None) -> None:
    config.set_config({"strategy": {"rpc_urls": {8453: "https://example.invalid"}}})

    from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
    from wayfinder_paths.core.utils.web3 import get_web3s_from_chain_id

    w3 = get_web3s_from_chain_id(CHAIN_ID_BASE)[0]
    assert w3.provider.endpoint_uri == "https://example.invalid"
