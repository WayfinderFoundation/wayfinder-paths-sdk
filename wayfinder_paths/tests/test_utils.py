import json
from pathlib import Path
from typing import Any


def load_strategy_examples(strategy_test_file: Path) -> dict[str, Any]:
    examples_path = strategy_test_file.parent / "examples.json"

    if not examples_path.exists():
        raise FileNotFoundError(
            f"examples.json is REQUIRED for strategy tests. "
            f"Create it at: {examples_path}\n"
            f"See TESTING.md for the required structure."
        )

    with open(examples_path) as f:
        return json.load(f)


def get_canonical_examples(examples: dict[str, Any]) -> dict[str, Any]:
    canonical = {}

    # 'smoke' is always canonical
    if "smoke" in examples:
        canonical["smoke"] = examples["smoke"]

    # Any example without 'expect' is considered canonical usage
    for name, example_data in examples.items():
        if name == "smoke":
            continue
        if isinstance(example_data, dict) and "expect" not in example_data:
            canonical[name] = example_data

    return canonical
