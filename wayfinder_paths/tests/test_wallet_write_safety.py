from __future__ import annotations

import pytest

from wayfinder_paths.core.utils.wallets import (
    load_wallets,
    make_wallet_from_mnemonic,
    write_wallet_to_json,
)


def test_write_wallet_to_json_is_idempotent_for_same_wallet(tmp_path) -> None:
    mnemonic = "test test test test test test test test test test test junk"
    w = make_wallet_from_mnemonic(mnemonic, account_index=0)
    w["label"] = "main"

    write_wallet_to_json(w, out_dir=tmp_path, filename="config.json")
    write_wallet_to_json(w, out_dir=tmp_path, filename="config.json")

    wallets = load_wallets(tmp_path, "config.json")
    assert len(wallets) == 1
    assert wallets[0]["address"].lower() == w["address"].lower()
    assert wallets[0]["label"] == "main"


def test_write_wallet_to_json_refuses_overwrite_by_address(tmp_path) -> None:
    mnemonic = "test test test test test test test test test test test junk"
    w = make_wallet_from_mnemonic(mnemonic, account_index=0)
    w["label"] = "main"
    write_wallet_to_json(w, out_dir=tmp_path, filename="config.json")

    w2 = dict(w)
    w2["label"] = "other"
    with pytest.raises(ValueError, match="refusing to overwrite"):
        write_wallet_to_json(w2, out_dir=tmp_path, filename="config.json")


def test_write_wallet_to_json_refuses_duplicate_label(tmp_path) -> None:
    mnemonic = "test test test test test test test test test test test junk"
    w0 = make_wallet_from_mnemonic(mnemonic, account_index=0)
    w0["label"] = "main"
    write_wallet_to_json(w0, out_dir=tmp_path, filename="config.json")

    w1 = make_wallet_from_mnemonic(mnemonic, account_index=1)
    w1["label"] = "main"
    with pytest.raises(ValueError, match="refusing to create duplicate"):
        write_wallet_to_json(w1, out_dir=tmp_path, filename="config.json")
