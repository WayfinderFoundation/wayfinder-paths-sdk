from __future__ import annotations

import json
from pathlib import Path

from wayfinder_paths.core.utils.wallets import (
    ensure_wallet_mnemonic,
    load_wallet_mnemonic,
    make_local_wallet,
    make_wallet_from_mnemonic,
)


def test_make_wallet_from_mnemonic_derives_metamask_addresses() -> None:
    mnemonic = "test test test test test test test test test test test junk"

    w0 = make_wallet_from_mnemonic(mnemonic, account_index=0)
    w1 = make_wallet_from_mnemonic(mnemonic, account_index=1)

    assert w0["derivation_path"] == "m/44'/60'/0'/0/0"
    assert w1["derivation_path"] == "m/44'/60'/0'/0/1"

    assert w0["address"].lower() == "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
    assert w1["address"].lower() == "0x70997970c51812dc3a010c7d01b50e0d17dc79c8"


def test_ensure_wallet_mnemonic_persists(tmp_path: Path) -> None:
    m1 = ensure_wallet_mnemonic(out_dir=tmp_path, filename="config.json")
    m2 = ensure_wallet_mnemonic(out_dir=tmp_path, filename="config.json")

    assert isinstance(m1, str) and m1
    assert len(m1.split()) == 12
    assert m1 == m2
    assert load_wallet_mnemonic(tmp_path, "config.json") == m1

    cfg = json.loads((tmp_path / "config.json").read_text())
    assert cfg["wallet_mnemonic"] == m1


def test_make_local_wallet_skips_existing_addresses_for_mnemonic() -> None:
    mnemonic = "test test test test test test test test test test test junk"
    existing = [
        {
            "label": "already_used",
            "address": make_wallet_from_mnemonic(mnemonic, account_index=1)["address"],
        }
    ]

    w = make_local_wallet(
        label="strategy_wallet",
        existing_wallets=existing,
        mnemonic=mnemonic,
    )
    assert w["derivation_index"] == 2
