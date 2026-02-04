#!/usr/bin/env python3

import argparse
import re
import shutil
from pathlib import Path

from wayfinder_paths.core.utils.wallets import make_random_wallet, write_wallet_to_json


def sanitize_name(name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_").lower()


STRATEGY_PY = """from wayfinder_paths.core.strategies.Strategy import StatusDict, StatusTuple, Strategy


class {class_name}(Strategy):
    name = "{class_name}"

    async def deposit(self, **kwargs) -> StatusTuple:
        return (True, "Deposit successful")

    async def withdraw(self, **kwargs) -> StatusTuple:
        return (True, "Withdraw successful")

    async def update(self) -> StatusTuple:
        return (True, "Update successful")

    async def exit(self, **kwargs) -> StatusTuple:
        return (True, "Exit successful")

    async def _status(self) -> StatusDict:
        return StatusDict(
            portfolio_value=0.0,
            net_deposit=0.0,
            strategy_status={{}},
            gas_available=0.0,
            gassed_up=False,
        )

    @staticmethod
    async def policies() -> list[str]:
        return []
"""

MANIFEST_YAML = """schema_version: "0.1"
status: wip
entrypoint: "wayfinder_paths.strategies.{dir_name}.strategy.{class_name}"
adapters: []
"""

TEST_PY = """from pathlib import Path

import pytest

from wayfinder_paths.strategies.{dir_name}.strategy import {class_name}
from wayfinder_paths.tests.test_utils import load_strategy_examples


@pytest.fixture
def strategy():
    mock_config = {{
        "main_wallet": {{"address": "0x1234567890123456789012345678901234567890"}},
        "strategy_wallet": {{"address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"}},
    }}
    return {class_name}(config=mock_config)


@pytest.mark.asyncio
@pytest.mark.smoke
async def test_smoke(strategy):
    examples = load_strategy_examples(Path(__file__))
    smoke_data = examples.get("smoke", {{}})

    st = await strategy.status()
    assert isinstance(st, dict)

    ok, msg = await strategy.deposit(**smoke_data.get("deposit", {{}}))
    assert isinstance(ok, bool)
    assert isinstance(msg, str)

    ok, msg = await strategy.update()
    assert isinstance(ok, bool)

    ok, msg = await strategy.withdraw(**smoke_data.get("withdraw", {{}}))
    assert isinstance(ok, bool)
"""

README_MD = """# {class_name}

TODO: Brief description of what this strategy does.

- **Module**: `wayfinder_paths.strategies.{dir_name}.strategy.{class_name}`
- **Chain**: TODO
- **Token**: TODO

## Overview

TODO: Describe how the strategy works.

## Adapters Used

TODO: List adapters used by this strategy.

## Testing

```bash
poetry run pytest wayfinder_paths/strategies/{dir_name}/ -v
```
"""


def main():
    parser = argparse.ArgumentParser(
        description="Create a new strategy with dedicated wallet"
    )
    parser.add_argument("name", help="Strategy name (e.g., 'my_awesome_strategy')")
    parser.add_argument(
        "--strategies-dir",
        type=Path,
        default=Path(__file__).parent.parent / "wayfinder_paths" / "strategies",
    )
    parser.add_argument(
        "--wallets-file",
        type=Path,
        default=Path(__file__).parent.parent / "config.json",
    )
    parser.add_argument("--override", action="store_true")
    args = parser.parse_args()

    dir_name = sanitize_name(args.name)
    class_name = "".join(word.capitalize() for word in dir_name.split("_"))
    if not class_name.endswith("Strategy"):
        class_name += "Strategy"

    strategy_dir = args.strategies_dir / dir_name
    if strategy_dir.exists() and not args.override:
        raise SystemExit(f"Strategy exists: {strategy_dir}\nUse --override to replace")
    if strategy_dir.exists():
        shutil.rmtree(strategy_dir)
    strategy_dir.mkdir(parents=True, exist_ok=True)

    fmt = {"class_name": class_name, "dir_name": dir_name}
    (strategy_dir / "strategy.py").write_text(STRATEGY_PY.format(**fmt))
    (strategy_dir / "manifest.yaml").write_text(MANIFEST_YAML.format(**fmt))
    (strategy_dir / "test_strategy.py").write_text(TEST_PY.format(**fmt))
    (strategy_dir / "examples.json").write_text("{}\n")
    (strategy_dir / "README.md").write_text(README_MD.format(**fmt))

    if not args.wallets_file.exists():
        main_wallet = make_random_wallet()
        main_wallet["label"] = "main"
        write_wallet_to_json(
            main_wallet,
            out_dir=args.wallets_file.parent,
            filename=args.wallets_file.name,
        )

    wallet = make_random_wallet()
    wallet["label"] = dir_name
    write_wallet_to_json(
        wallet, out_dir=args.wallets_file.parent, filename=args.wallets_file.name
    )

    print(f"Created {strategy_dir}")
    print(f"  Class: {class_name}")
    print(f"  Wallet: {wallet['address']}")


if __name__ == "__main__":
    main()
