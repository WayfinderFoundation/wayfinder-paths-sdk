import argparse
import json
from pathlib import Path

from eth_account import Account

from wayfinder_paths.core.utils.wallets import (
    ensure_wallet_mnemonic,
    load_wallet_mnemonic,
    load_wallets,
    make_random_wallet,
    make_wallet_from_mnemonic,
    next_derivation_index_for_mnemonic,
    write_wallet_mnemonic,
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

    existing_mnemonic = load_wallet_mnemonic(args.out_dir, "config.json")
    mnemonic_to_use = existing_mnemonic
    generated_new_mnemonic = False

    if args.mnemonic is not None:
        if args.mnemonic == "__generate__":
            if not mnemonic_to_use:
                mnemonic_to_use = ensure_wallet_mnemonic(
                    out_dir=args.out_dir, filename="config.json"
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
                write_wallet_mnemonic(
                    phrase, out_dir=args.out_dir, filename="config.json"
                )
                mnemonic_to_use = phrase

    if generated_new_mnemonic and mnemonic_to_use:
        print("Generated wallet mnemonic (saved to config.json):")
        print(mnemonic_to_use)

    existing = load_wallets(args.out_dir, "config.json")
    existing_was_empty = not existing
    has_main = any(w.get("label") in ("main", "default") for w in existing)

    rows: list[dict[str, str]] = []
    index = 0

    # Custom labeled wallet (e.g., for strategy name)
    if args.label:
        # Check if label already exists - if so, skip (don't create duplicate)
        if any(w.get("label") == args.label for w in existing):
            print(f"Wallet with label '{args.label}' already exists, skipping...")
        else:
            if mnemonic_to_use:
                is_main_label = str(args.label).strip().lower() == "main"
                derivation_index = (
                    0
                    if is_main_label
                    else next_derivation_index_for_mnemonic(
                        mnemonic_to_use, existing, start=1
                    )
                )
                w = make_wallet_from_mnemonic(
                    mnemonic_to_use, account_index=derivation_index
                )
            else:
                w = make_random_wallet()
            w["label"] = args.label
            rows.append(w)
            print(f"[{index}] {w['address']}  (label: {args.label})")
            write_wallet_to_json(w, out_dir=args.out_dir, filename="config.json")
            existing.append(w)
            if args.keystore_password:
                ks = to_keystore_json(w["private_key_hex"], args.keystore_password)
                ks_path = args.out_dir / f"keystore_{w['address']}.json"
                ks_path.write_text(json.dumps(ks))
            index += 1

            # If no wallets existed before, also create a "main" wallet
            if existing_was_empty and str(args.label).strip().lower() != "main":
                if mnemonic_to_use:
                    main_w = make_wallet_from_mnemonic(mnemonic_to_use, account_index=0)
                else:
                    main_w = make_random_wallet()
                main_w["label"] = "main"
                rows.append(main_w)
                print(f"[{index}] {main_w['address']}  (main)")
                write_wallet_to_json(
                    main_w, out_dir=args.out_dir, filename="config.json"
                )
                existing.append(main_w)
                has_main = True
                if args.keystore_password:
                    ks = to_keystore_json(
                        main_w["private_key_hex"], args.keystore_password
                    )
                    ks_path = args.out_dir / f"keystore_{main_w['address']}.json"
                    ks_path.write_text(json.dumps(ks))
                index += 1
    else:
        if args.n == 0:
            args.n = 1

        # Find next temporary number
        existing_labels = {
            w.get("label", "")
            for w in existing
            if w.get("label", "").startswith("temporary_")
        }
        temp_numbers = set()
        for label in existing_labels:
            try:
                num = int(label.replace("temporary_", ""))
                temp_numbers.add(num)
            except ValueError:
                pass
        next_temp_num = 1
        if temp_numbers:
            next_temp_num = max(temp_numbers) + 1

        for i in range(args.n):
            # Label first wallet as "main" if main doesn't exist, otherwise use temporary_N
            if i == 0 and not has_main:
                if mnemonic_to_use:
                    w = make_wallet_from_mnemonic(mnemonic_to_use, account_index=0)
                else:
                    w = make_random_wallet()
                w["label"] = "main"
                rows.append(w)
                print(f"[{index}] {w['address']}  (main)")
                has_main = True
            else:
                if mnemonic_to_use:
                    derivation_index = next_derivation_index_for_mnemonic(
                        mnemonic_to_use, existing, start=1
                    )
                    w = make_wallet_from_mnemonic(
                        mnemonic_to_use, account_index=derivation_index
                    )
                else:
                    w = make_random_wallet()
                while next_temp_num in temp_numbers:
                    next_temp_num += 1
                w["label"] = f"temporary_{next_temp_num}"
                temp_numbers.add(next_temp_num)
                rows.append(w)
                print(f"[{index}] {w['address']}  (label: temporary_{next_temp_num})")

            write_wallet_to_json(w, out_dir=args.out_dir, filename="config.json")
            existing.append(w)
            if args.keystore_password:
                ks = to_keystore_json(w["private_key_hex"], args.keystore_password)
                ks_path = args.out_dir / f"keystore_{w['address']}.json"
                ks_path.write_text(json.dumps(ks))
            index += 1


if __name__ == "__main__":
    main()
