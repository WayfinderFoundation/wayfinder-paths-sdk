from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

import wayfinder_paths.core.config as config
import wayfinder_paths.core.utils.web3 as web3_utils


@pytest.fixture
def restore_global_config() -> Iterator[None]:
    original = copy.deepcopy(config.CONFIG)
    yield
    config.set_config(original)


def _write_api_key_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # get_api_key() reads fresh from config.json on every call, so the key
    # must exist on disk — set_config() alone is not enough.
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"system": {"api_key": "wk_test"}}))
    monkeypatch.setenv("WAYFINDER_CONFIG_PATH", str(cfg_path))


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


def test_missing_explicit_config_does_not_clear_loaded_config(
    restore_global_config: None, tmp_path: Path
) -> None:
    config.set_config(
        {"system": {"api_base_url": "https://strategies-dev.wayfinder.ai/api/v1"}}
    )

    config.load_config(tmp_path / "missing.json")

    assert config.get_api_base_url() == "https://strategies-dev.wayfinder.ai/api/v1"


def test_api_base_url_defaults_to_wayfinder_api(restore_global_config: None) -> None:
    config.set_config({})

    assert config.get_api_base_url() == "https://wayfinder.ai/api/v1"


@pytest.mark.asyncio
async def test_web3s_fallback_to_rpc_proxy(
    restore_global_config: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_api_key_config(tmp_path, monkeypatch)
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

    monkeypatch.setattr(web3_utils, "_fetch_pool_size", lambda _chain_id: None)

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
async def test_user_rpcs_override_proxy(
    restore_global_config: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_api_key_config(tmp_path, monkeypatch)
    config.set_config(
        {
            "system": {
                "api_base_url": "https://strategies.wayfinder.ai/api/v1",
                "api_key": "wk_test",
            },
            "strategy": {"rpc_urls": {"8453": ["https://custom-rpc.example.com"]}},
        }
    )

    from wayfinder_paths.core.constants.chains import CHAIN_ID_ARBITRUM, CHAIN_ID_BASE
    from wayfinder_paths.core.utils.web3 import web3s_from_chain_id

    monkeypatch.setattr(web3_utils, "_fetch_pool_size", lambda _chain_id: None)

    async with web3s_from_chain_id(CHAIN_ID_BASE) as web3s:
        assert web3s[0].provider.endpoint_uri == "https://custom-rpc.example.com"
        assert "X-API-KEY" not in web3s[0].provider._request_kwargs.get("headers", {})

    async with web3s_from_chain_id(CHAIN_ID_ARBITRUM) as web3s:
        uri = web3s[0].provider.endpoint_uri
        assert uri == "https://strategies.wayfinder.ai/api/v1/blockchain/rpc/42161/"
        assert web3s[0].provider._request_kwargs["headers"]["X-API-KEY"] == "wk_test"


@pytest.mark.asyncio
async def test_web3s_uses_indexed_rpc_proxy_pool(
    restore_global_config: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_api_key_config(tmp_path, monkeypatch)
    config.set_config(
        {
            "system": {
                "api_base_url": "https://strategies.wayfinder.ai/api/v1",
                "api_key": "wk_test",
            },
            "strategy": {"rpc_urls": {}},
        }
    )

    from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
    from wayfinder_paths.core.utils.web3 import web3s_from_chain_id

    monkeypatch.setattr(web3_utils, "_fetch_pool_size", lambda _chain_id: 2)

    async with web3s_from_chain_id(CHAIN_ID_BASE) as web3s:
        assert [w3.provider.endpoint_uri for w3 in web3s] == [
            "https://strategies.wayfinder.ai/api/v1/blockchain/rpc/8453/0/",
            "https://strategies.wayfinder.ai/api/v1/blockchain/rpc/8453/1/",
        ]
        assert web3s[0].provider._request_kwargs["headers"]["X-API-KEY"] == "wk_test"


def test_web3s_accept_int_rpc_url_keys(restore_global_config: None) -> None:
    config.set_config({"strategy": {"rpc_urls": {8453: "https://example.invalid"}}})

    from wayfinder_paths.core.constants.chains import CHAIN_ID_BASE
    from wayfinder_paths.core.utils.web3 import get_web3s_from_chain_id

    w3 = get_web3s_from_chain_id(CHAIN_ID_BASE)[0]
    assert w3.provider.endpoint_uri == "https://example.invalid"


@pytest.fixture
def rpc_pool_probe(monkeypatch: pytest.MonkeyPatch) -> Mock:
    monkeypatch.setattr(web3_utils, "_pool_size_cache", {})
    monkeypatch.setattr(web3_utils, "get_rpc_urls", lambda: {})
    monkeypatch.setattr(web3_utils, "_wayfinder_auth_headers", lambda: {})
    probe = Mock(
        return_value=httpx.Response(
            200,
            json={"size": 0},
            request=httpx.Request("GET", "https://example.invalid/count/"),
        )
    )
    monkeypatch.setattr(web3_utils.httpx, "get", probe)
    return probe


@pytest.mark.parametrize("chain_id", [146, 123456])
def test_unconfigured_rpc_fails_before_creating_provider(
    rpc_pool_probe: Mock, monkeypatch: pytest.MonkeyPatch, chain_id: int
) -> None:
    create_provider = Mock()
    monkeypatch.setattr(web3_utils, "_get_web3", create_provider)

    with pytest.raises(
        ValueError, match=f"No RPC endpoints configured for chain {chain_id}"
    ):
        web3_utils.get_web3s_from_chain_id(chain_id)

    rpc_pool_probe.assert_called_once()
    create_provider.assert_not_called()


def test_unconfigured_rpc_is_rechecked_when_support_is_added(
    rpc_pool_probe: Mock,
) -> None:
    with pytest.raises(ValueError, match="No RPC endpoints configured"):
        web3_utils._get_rpcs_for_chain_id(146)

    rpc_pool_probe.return_value = httpx.Response(
        200,
        json={"size": 2},
        request=httpx.Request("GET", "https://example.invalid/count/"),
    )
    expected = [
        f"{config.get_api_base_url()}/blockchain/rpc/146/{i}/" for i in range(2)
    ]
    assert web3_utils._get_rpcs_for_chain_id(146) == expected
    assert web3_utils._get_rpcs_for_chain_id(146) == expected
    assert rpc_pool_probe.call_count == 2  # Cache the positive result, not zero.


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (503, {"size": 0}),
        (404, {}),
        (200, {}),
        (200, {"size": None}),
        (200, {"size": -1}),
    ],
)
def test_failed_pool_discovery_preserves_uncached_legacy_fallback(
    rpc_pool_probe: Mock, status: int, payload: dict[str, int | None]
) -> None:
    rpc_pool_probe.return_value = httpx.Response(
        status,
        json=payload,
        request=httpx.Request("GET", "https://example.invalid/count/"),
    )
    for _ in range(2):
        assert web3_utils._get_rpcs_for_chain_id(146) == [
            f"{config.get_api_base_url()}/blockchain/rpc/146/"
        ]
    assert rpc_pool_probe.call_count == 2


def test_pool_discovery_timeout_preserves_fallback(rpc_pool_probe: Mock) -> None:
    rpc_pool_probe.side_effect = httpx.ReadTimeout("timed out")

    assert web3_utils._get_rpcs_for_chain_id(146) == [
        f"{config.get_api_base_url()}/blockchain/rpc/146/"
    ]
    assert web3_utils._pool_size_cache == {}


@pytest.mark.parametrize(
    "rpc_urls", ["https://custom.example", ["https://custom.example"]]
)
def test_custom_rpc_bypasses_pool_discovery(
    rpc_pool_probe: Mock, monkeypatch: pytest.MonkeyPatch, rpc_urls: str | list[str]
) -> None:
    monkeypatch.setattr(web3_utils, "get_rpc_urls", lambda: {"146": rpc_urls})

    assert web3_utils._get_rpcs_for_chain_id(146) == ["https://custom.example"]
    rpc_pool_probe.assert_not_called()


@pytest.mark.parametrize("rpc_urls", ["", []])
def test_empty_custom_rpc_configuration_fails_fast(
    rpc_pool_probe: Mock, monkeypatch: pytest.MonkeyPatch, rpc_urls: str | list[str]
) -> None:
    monkeypatch.setattr(web3_utils, "get_rpc_urls", lambda: {"146": rpc_urls})

    with pytest.raises(ValueError, match="No RPC endpoints configured for chain 146"):
        web3_utils.get_web3s_from_chain_id(146)
    rpc_pool_probe.assert_not_called()


def test_gorlami_base_url_derived_from_api_base(
    restore_global_config: None,
) -> None:
    from wayfinder_paths.core.clients.GorlamiTestnetClient import GorlamiTestnetClient

    config.set_config(
        {
            "system": {
                "api_base_url": "https://strategies.wayfinder.ai/api/v1",
            }
        }
    )

    client = GorlamiTestnetClient()
    assert (
        client.base_url == "https://strategies.wayfinder.ai/api/v1/blockchain/gorlami"
    )
