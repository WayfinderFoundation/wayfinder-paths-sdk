import argparse
import json
from pathlib import Path

from eth_account import Account

from wayfinder_paths.core.utils.wallets import (
    load_wallets,
    make_random_wallet,
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
        "--default",
        action="store_true",
        help="Create a default 'main' wallet if none exists (used by CI)",
    )
    args = parser.parse_args()

    # --default is equivalent to -n 1 (create main wallet if needed)
    if args.default and args.n == 0 and not args.label:
        args.n = 1

    args.out_dir.mkdir(parents=True, exist_ok=True)

    existing = load_wallets(args.out_dir, "config.json")
    has_main = any(w.get("label") in ("main", "default") for w in existing)

    rows: list[dict[str, str]] = []
    index = 0

    # Custom labeled wallet (e.g., for strategy name)
    if args.label:
        # Check if label already exists - if so, skip (don't create duplicate)
        if any(w.get("label") == args.label for w in existing):
            print(f"Wallet with label '{args.label}' already exists, skipping...")
        else:
            w = make_random_wallet()
            w["label"] = args.label
            rows.append(w)
            print(f"[{index}] {w['address']}  (label: {args.label})")
            write_wallet_to_json(w, out_dir=args.out_dir, filename="config.json")
            if args.keystore_password:
                ks = to_keystore_json(w["private_key_hex"], args.keystore_password)
                ks_path = args.out_dir / f"keystore_{w['address']}.json"
                ks_path.write_text(json.dumps(ks))
            index += 1

            # If no wallets existed before, also create a "main" wallet
            if not existing:
                main_w = make_random_wallet()
                main_w["label"] = "main"
                rows.append(main_w)
                print(f"[{index}] {main_w['address']}  (main)")
                write_wallet_to_json(
                    main_w, out_dir=args.out_dir, filename="config.json"
                )
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
            w = make_random_wallet()
            # Label first wallet as "main" if main doesn't exist, otherwise use temporary_N
            if i == 0 and not has_main:
                w["label"] = "main"
                rows.append(w)
                print(f"[{index}] {w['address']}  (main)")
            else:
                while next_temp_num in temp_numbers:
                    next_temp_num += 1
                w["label"] = f"temporary_{next_temp_num}"
                temp_numbers.add(next_temp_num)
                rows.append(w)
                print(f"[{index}] {w['address']}  (label: temporary_{next_temp_num})")

            write_wallet_to_json(w, out_dir=args.out_dir, filename="config.json")
            if args.keystore_password:
                ks = to_keystore_json(w["private_key_hex"], args.keystore_password)
                ks_path = args.out_dir / f"keystore_{w['address']}.json"
                ks_path.write_text(json.dumps(ks))
            index += 1


if __name__ == "__main__":
    main()
