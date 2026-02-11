import argparse
import json
from pathlib import Path

from eth_account import Account

from wayfinder_paths.core.config import load_wallet_mnemonic, write_wallet_mnemonic
from wayfinder_paths.core.utils.wallets import (
    ensure_wallet_mnemonic,
    load_wallets,
    make_local_wallet,
    write_wallet_to_json,
)


def to_keystore_json(private_key_hex: str, password: str):
    return Account.encrypt(private_key_hex, password)


def main():
    parser = argparse.ArgumentParser(description="Generate local dev wallets")
    parser.add_argument(
        "-n",
        type=int,
        default=0,
        help="Number of wallets to create (ignored if --label is used)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("."),
        help="Output directory for config.json (and keystore files)",
    )
    parser.add_argument(
        "--keystore-password",
        type=str,
        default=None,
        help="Optional password to write geth-compatible keystores",
    )
    parser.add_argument(
        "--label",
        type=str,
        default=None,
        help="Create a wallet with a custom label (e.g., strategy name). If not provided, auto-generates labels.",
    )
    parser.add_argument(
        "--mnemonic",
        nargs="?",
        const="__generate__",
        default=None,
        help=(
            "Use mnemonic-derived deterministic wallets (MetaMask path). "
            "If provided without a value, generates and persists a new mnemonic in config.json. "
            "If config.json already has wallet_mnemonic, it will be used automatically."
        ),
    )
    parser.add_argument(
        "--default",
        action="store_true",
        help="Create a default 'main' wallet if none exists (used by CI)",
    )
    args = parser.parse_args()

    # --default is equivalent to -n 1 (create main wallet if needed)
    if args.default and args.n == 0 and not args.label:
        args.n = 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    config_path = args.out_dir / "config.json"

    existing_mnemonic = load_wallet_mnemonic(config_path)
    mnemonic_to_use = existing_mnemonic
    generated_new_mnemonic = False

    if args.mnemonic is not None:
        if args.mnemonic == "__generate__":
            if not mnemonic_to_use:
                mnemonic_to_use = ensure_wallet_mnemonic(
                    config_path=config_path,
                )
                generated_new_mnemonic = True
        else:
            phrase = str(args.mnemonic).strip()
            if not phrase:
                raise SystemExit("--mnemonic was provided but empty")
            if mnemonic_to_use and phrase != mnemonic_to_use:
                raise SystemExit(
                    "config.json already contains wallet_mnemonic; refusing to overwrite"
                )
            if not mnemonic_to_use:
                write_wallet_mnemonic(phrase, config_path)
                mnemonic_to_use = phrase

    if generated_new_mnemonic and mnemonic_to_use:
        print("Generated wallet mnemonic (saved to config.json):")
        print(mnemonic_to_use)

    existing = load_wallets(args.out_dir, "config.json")
    existing_was_empty = not existing
    has_main = any(w.get("label") in ("main", "default") for w in existing)

    labels_to_create: list[str] = []
    if args.label:
        want = str(args.label).strip()
        if any(w.get("label") == want for w in existing):
            print(f"Wallet with label '{want}' already exists, skipping...")
            return
        labels_to_create.append(want)
        if existing_was_empty and want.lower() != "main":
            labels_to_create.append("main")
    else:
        if args.n == 0:
            args.n = 1

        remaining = int(args.n)
        if not has_main:
            labels_to_create.append("main")
            remaining -= 1

        existing_temp_numbers = set()
        for w in existing:
            label = str(w.get("label", ""))
            if label.startswith("temporary_"):
                suffix = label.removeprefix("temporary_")
                if suffix.isdigit():
                    existing_temp_numbers.add(int(suffix))

        next_temp_num = max(existing_temp_numbers, default=0) + 1
        for _ in range(max(0, remaining)):
            while next_temp_num in existing_temp_numbers:
                next_temp_num += 1
            labels_to_create.append(f"temporary_{next_temp_num}")
            existing_temp_numbers.add(next_temp_num)
            next_temp_num += 1

    for i, label in enumerate(labels_to_create):
        w = make_local_wallet(
            label=label,
            existing_wallets=existing,
            mnemonic=mnemonic_to_use,
        )
        suffix = "(main)" if label.lower() == "main" else f"(label: {label})"
        print(f"[{i}] {w['address']}  {suffix}")
        write_wallet_to_json(w, out_dir=args.out_dir, filename="config.json")
        existing.append(w)
        if args.keystore_password:
            ks = to_keystore_json(w["private_key_hex"], args.keystore_password)
            ks_path = args.out_dir / f"keystore_{w['address']}.json"
            ks_path.write_text(json.dumps(ks))


if __name__ == "__main__":
    main()
