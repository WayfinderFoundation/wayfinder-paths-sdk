#!/usr/bin/env python3

import argparse
import re
import shutil
from pathlib import Path

from wayfinder_paths.core.utils.wallets import make_random_wallet, write_wallet_to_json


def sanitize_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")
    return name.lower()


def update_strategy_file(strategy_path: Path, class_name: str) -> None:
    content = strategy_path.read_text()
    content = content.replace("MyStrategy", class_name)
    content = re.sub(
        r"my_strategy", class_name.lower().replace("Strategy", ""), content
    )
    strategy_path.write_text(content)


def main():
    parser = argparse.ArgumentParser(
        description="Create a new strategy from template with dedicated wallet"
    )
    parser.add_argument(
        "name",
        help="Strategy name (e.g., 'my_awesome_strategy' or 'My Awesome Strategy')",
    )
    parser.add_argument(
        "--template-dir",
        type=Path,
        default=Path(__file__).parent.parent
        / "wayfinder_paths"
        / "templates"
        / "strategy",
        help="Path to strategy template directory",
    )
    parser.add_argument(
        "--strategies-dir",
        type=Path,
        default=Path(__file__).parent.parent / "wayfinder_paths" / "strategies",
        help="Path to strategies directory",
    )
    parser.add_argument(
        "--wallets-file",
        type=Path,
        default=Path(__file__).parent.parent / "config.json",
        help="Path to config.json file",
    )
    parser.add_argument(
        "--override",
        action="store_true",
        help="Override existing strategy directory if it exists",
    )
    args = parser.parse_args()

    dir_name = sanitize_name(args.name)
    strategy_dir = args.strategies_dir / dir_name

    if strategy_dir.exists() and not args.override:
        raise SystemExit(
            f"Strategy directory already exists: {strategy_dir}\n"
            "Use --override to replace it"
        )

    if not args.template_dir.exists():
        raise SystemExit(f"Template directory not found: {args.template_dir}")

    if strategy_dir.exists():
        print(f"Removing existing directory: {strategy_dir}")
        shutil.rmtree(strategy_dir)
    strategy_dir.mkdir(parents=True, exist_ok=True)
    print(f"Created strategy directory: {strategy_dir}")

    template_files = [
        "strategy.py",
        "test_strategy.py",
        "examples.json",
        "README.md",
    ]
    for filename in template_files:
        src = args.template_dir / filename
        if src.exists():
            dst = strategy_dir / filename
            shutil.copy2(src, dst)
            print(f"  Copied {filename}")

    class_name = "".join(word.capitalize() for word in dir_name.split("_"))
    if not class_name.endswith("Strategy"):
        class_name += "Strategy"

    strategy_file = strategy_dir / "strategy.py"
    if strategy_file.exists():
        update_strategy_file(strategy_file, class_name)
        print(f"  Updated strategy.py with class name: {class_name}")

    # If config.json doesn't exist, create it with a main wallet first
    if not args.wallets_file.exists():
        print("  Creating new config.json with main wallet...")
        main_wallet = make_random_wallet()
        main_wallet["label"] = "main"
        write_wallet_to_json(
            main_wallet,
            out_dir=args.wallets_file.parent,
            filename=args.wallets_file.name,
        )
        print(f"  Generated main wallet: {main_wallet['address']}")

    wallet = make_random_wallet()
    wallet["label"] = dir_name
    write_wallet_to_json(
        wallet, out_dir=args.wallets_file.parent, filename=args.wallets_file.name
    )
    print(f"  Generated strategy wallet: {wallet['address']} (label: {dir_name})")

    print("\n✅ Strategy created successfully!")
    print(f"   Directory: {strategy_dir}")
    print(f"   Name: {dir_name}")
    print(f"   Class: {class_name}")
    print(f"   Wallet: {wallet['address']}")
    print("\nNext steps:")
    print(f"   1. Edit {strategy_dir / 'strategy.py'} to implement your strategy")
    print("   2. Add required adapters in __init__")
    print(f"   3. Test with: just test-strategy {dir_name}")


if __name__ == "__main__":
    main()
