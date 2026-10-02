from __future__ import annotations

import copy
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from wayfinder_paths.core import rpc_recovery as recovery


@pytest.fixture(autouse=True)
def hosted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "test-instance")
    monkeypatch.setattr(recovery, "_failed_attempts", {})


def saved_config(
    tmp_path: Path, overrides: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    config = {
        "system": {
            "api_key": "wk_test",
            "api_base_url": "https://gateway.invalid/api/v1",
        },
        "strategy": {"rpc_urls": overrides, "wallets": ["keep"]},
        "other": {"preserved": True},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path, config


@pytest.mark.parametrize("as_list", [False, True])
def test_repairs_persistent_symlink_and_preserves_other_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, as_list: bool
) -> None:
    overrides = {
        k: [v] if as_list else v for k, v in recovery.LEGACY_HOSTED_RPCS.items()
    }
    overrides["137"] = "https://private.invalid/key"
    path, config = saved_config(tmp_path, overrides)
    path.chmod(0o640)
    original = path.read_bytes()
    link = tmp_path / "project-config.json"
    link.symlink_to(path)
    validate = Mock()
    monkeypatch.setattr(recovery, "_validate_gateway", validate)

    recovery.recover_legacy_rpc_overrides(config, link)

    assert link.is_symlink()
    expected = json.loads(original)
    expected["strategy"]["rpc_urls"] = {"137": "https://private.invalid/key"}
    assert json.loads(path.read_text()) == config == expected
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    backups = list(tmp_path.glob("*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert validate.call_count == 3
    # Repeat after restart: zero network or additional backups.
    recovery.recover_legacy_rpc_overrides(json.loads(path.read_text()), link)
    assert validate.call_count == 3
    assert len(list(tmp_path.glob("*.bak"))) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"1": "http://localhost:8545"},
        {"1": "https://gateway.invalid/api/v1/blockchain/gorlami/fork/abc"},
        {"1": [recovery.LEGACY_HOSTED_RPCS["1"], "https://private.invalid"]},
        {"1": recovery.LEGACY_HOSTED_RPCS["1"] + "/personal-key"},
        {"1": "https://base-rpc.publicnode.com"},
    ],
)
def test_custom_and_fork_settings_are_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    path, config = saved_config(tmp_path, overrides)
    before = path.read_bytes()
    validate = Mock(side_effect=AssertionError("Unexpected validation"))
    monkeypatch.setattr(recovery, "_validate_gateway", validate)
    recovery.recover_legacy_rpc_overrides(config, path)
    assert path.read_bytes() == before
    validate.assert_not_called()
    assert not list(tmp_path.glob("*.bak"))


def test_local_install_and_process_local_fork_are_not_migrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, config = saved_config(tmp_path, dict(recovery.LEGACY_HOSTED_RPCS))
    before = path.read_bytes()
    monkeypatch.delenv("OPENCODE_INSTANCE_ID")
    recovery.recover_legacy_rpc_overrides(config, path)
    assert path.read_bytes() == before
    monkeypatch.setenv("OPENCODE_INSTANCE_ID", "instance")
    config["strategy"]["rpc_urls"] = {"1": "http://localhost:8545"}
    recovery.recover_legacy_rpc_overrides(config, path)
    assert path.read_bytes() == before


def test_gateway_failure_preserves_file_and_throttles_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, config = saved_config(tmp_path, dict(recovery.LEGACY_HOSTED_RPCS))
    original = path.read_bytes()
    clock = [100.0]
    monkeypatch.setattr(recovery.time, "monotonic", lambda: clock[0])
    validate = Mock(side_effect=httpx.ConnectError("https://secret.invalid/wk_secret"))
    monkeypatch.setattr(recovery, "_validate_gateway", validate)
    for _ in range(2):
        with pytest.raises(recovery.LegacyRPCConfigurationError) as caught:
            recovery.recover_legacy_rpc_overrides(config, path, chain_id=42161)
        assert "secret" not in str(caught.value)
    assert validate.call_count == 1
    clock[0] += 61
    with pytest.raises(recovery.LegacyRPCConfigurationError):
        recovery.recover_legacy_rpc_overrides(config, path, chain_id=42161)
    assert validate.call_count == 2
    with pytest.raises(recovery.LegacyRPCConfigurationError):
        recovery.recover_legacy_rpc_overrides(config, path, chain_id=42161)
    assert validate.call_count == 2
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.bak"))


def test_concurrent_callers_reuse_persistent_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, config = saved_config(tmp_path, {"1": recovery.LEGACY_HOSTED_RPCS["1"]})
    second = copy.deepcopy(config)
    validate = Mock()
    monkeypatch.setattr(recovery, "_validate_gateway", validate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(recovery.recover_legacy_rpc_overrides, item, path)
            for item in (config, second)
        ]
        for future in futures:
            future.result()
    assert config == second == json.loads(path.read_text())
    validate.assert_called_once()
    assert len(list(tmp_path.glob("*.bak"))) == 1


def test_does_not_overwrite_config_changed_during_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, config = saved_config(tmp_path, {"1": recovery.LEGACY_HOSTED_RPCS["1"]})
    monkeypatch.setattr(
        recovery, "_validate_gateway", lambda *_: path.write_text('{"user_edit": true}')
    )
    with pytest.raises(recovery.LegacyRPCConfigurationError):
        recovery.recover_legacy_rpc_overrides(config, path)
    assert json.loads(path.read_text()) == {"user_edit": True}


@pytest.mark.parametrize(
    "failure", [None, "auth", "wrong_chain", "no_block", "empty_pool", "timeout"]
)
def test_validation_checks_auth_identity_and_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    path, config = saved_config(tmp_path, {"1": recovery.LEGACY_HOSTED_RPCS["1"]})
    original = path.read_bytes()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.headers["X-API-KEY"] == "wk_test"
        if failure == "auth":
            return httpx.Response(401)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret URL")
        if request.method == "GET":
            return httpx.Response(
                200, json={"size": 0 if failure == "empty_pool" else 4}
            )
        method = json.loads(request.content)["method"]
        value = "0x2" if failure == "wrong_chain" else "0x1"
        if method == "eth_blockNumber":
            value = "0x0" if failure == "no_block" else "0xabc"
        return httpx.Response(200, json={"result": value})

    client_class = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw),
    )
    if failure:
        with pytest.raises(recovery.LegacyRPCConfigurationError):
            recovery.recover_legacy_rpc_overrides(config, path)
        assert path.read_bytes() == original
    else:
        recovery.recover_legacy_rpc_overrides(config, path)
        assert len(calls) == 3
        assert config["strategy"]["rpc_urls"] == {}


def test_hosted_setup_migrates_before_mcp_loads_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import remote_setup_utils as setup

    path, _ = saved_config(tmp_path, dict(recovery.LEGACY_HOSTED_RPCS))
    original_loader = setup._load_core_module
    monkeypatch.setattr(
        setup,
        "_load_core_module",
        lambda name, *args: recovery
        if name == "rpc_recovery"
        else original_loader(name, *args),
    )
    monkeypatch.setattr(recovery, "_validate_gateway", Mock())
    setup.ensure_config(api_key="wk_new_test", config_path=path)
    saved = json.loads(path.read_text())
    assert saved["strategy"]["rpc_urls"] == {}
    assert saved["system"]["api_key"] == "wk_new_test"


def test_runtime_first_use_recovers_and_selects_managed_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wayfinder_paths.core.utils import web3

    path, config = saved_config(
        tmp_path, {"42161": recovery.LEGACY_HOSTED_RPCS["42161"]}
    )
    monkeypatch.setattr(web3, "CONFIG", config)
    monkeypatch.setattr(web3, "get_rpc_urls", lambda: config["strategy"]["rpc_urls"])
    monkeypatch.setattr(web3, "resolve_config_path", lambda: path)
    monkeypatch.setattr(
        web3, "get_api_base_url", lambda: "https://gateway.invalid/api/v1"
    )
    monkeypatch.setattr(web3, "_fetch_pool_size", lambda _: 4)
    monkeypatch.setattr(recovery, "_validate_gateway", Mock())
    urls = web3._get_rpcs_for_chain_id(42161)
    assert len(urls) == 4
    assert all(
        url.startswith("https://gateway.invalid/api/v1/blockchain/rpc/42161/")
        for url in urls
    )
    assert config["strategy"]["rpc_urls"] == {}
    assert json.loads(path.read_text())["strategy"]["rpc_urls"] == {}


@pytest.mark.asyncio
async def test_async_resolution_keeps_blocking_io_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from unittest.mock import AsyncMock

    from wayfinder_paths.core.utils import web3

    caller = threading.get_ident()

    def resolve(_: int) -> list[str]:
        assert threading.get_ident() != caller
        return ["https://rpc.invalid"]

    provider = Mock()
    provider.provider.disconnect = AsyncMock()
    monkeypatch.setattr(web3, "_get_rpcs_for_chain_id", resolve)
    monkeypatch.setattr(web3, "_get_web3", lambda *_: provider)
    async with web3.web3s_from_chain_id(1) as providers:
        assert providers == [provider]
    provider.provider.disconnect.assert_awaited_once()
