from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from wayfinder_paths.adapters.aerodrome_adapter.adapter import SugarEpoch


def _load_script_module(path: Path, *, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_aerodrome_best_vote_pools_dry_run(monkeypatch, tmp_path, capsys):
    script_path = (
        Path(__file__).resolve().parents[3] / "scripts" / "aerodrome_best_vote_pools.py"
    )
    mod = _load_script_module(script_path, name="aerodrome_best_vote_pools_script")

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}")

    class _FakeAdapter:
        strategy_wallet_address = "0x" + "11" * 20

        async def token_decimals(self, token: str) -> int:  # noqa: ARG002
            return 18

        async def token_symbol(self, _token: str) -> str:
            return "TKN"

        async def rank_pools_by_usdc_per_ve(
            self, *, top_n: int, limit: int, require_all_prices: bool
        ):  # noqa: ARG002
            ep = SugarEpoch(
                ts=0,
                lp="0x" + "22" * 20,
                votes=10,
                emissions=0,
                bribes=[],
                fees=[],
            )
            return [(1.23, ep, 12.34)]

        async def pools_by_lp(self):
            return {
                "0x" + "22" * 20: SimpleNamespace(
                    symbol="POOL", token0="0x" + "33" * 20, token1="0x" + "44" * 20
                )
            }

        async def estimate_votes_for_lock(
            self, *, aero_amount_raw: int, lock_duration_s: int
        ) -> int:  # noqa: ARG002
            return 100

        async def estimate_ve_apr_percent(
            self, *, usdc_per_ve: float, votes_raw: int, aero_locked_raw: int
        ):  # noqa: ARG002
            return 12.34

    def _fake_get_adapter(
        adapter_class, wallet_label: str, *, config_path: str, **kwargs
    ):  # noqa: ARG001
        assert wallet_label == "main"
        assert str(config_path) == str(cfg_path)
        return _FakeAdapter()

    def _fake_load_config(path: str, *, require_exists: bool = False):  # noqa: ARG001
        assert require_exists is True

    async def _fake_native_balance(_wallet: str) -> int:
        return 0

    async def _fake_erc20_balance(_token: str, _wallet: str) -> int:
        return 0

    monkeypatch.setattr(mod, "get_adapter", _fake_get_adapter)
    monkeypatch.setattr(mod, "load_config", _fake_load_config)
    monkeypatch.setattr(mod, "_native_balance", _fake_native_balance)
    monkeypatch.setattr(mod, "_erc20_balance", _fake_erc20_balance)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "aerodrome_best_vote_pools.py",
            "--config",
            str(cfg_path),
            "--wallet-label",
            "main",
            "--top-n",
            "1",
            "--limit",
            "1",
            "--dry-run",
        ],
    )

    assert await mod.main() == 0

    out = capsys.readouterr().out
    assert "Top pools" in out
    assert "usdc_per_ve" in out
