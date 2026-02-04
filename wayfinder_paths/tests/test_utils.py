import json
from pathlib import Path
from typing import Any


def assert_status_tuple(value: Any) -> tuple[bool, str]:
    assert isinstance(value, tuple), (
        f"Expected StatusTuple (tuple[bool, str]), got {type(value).__name__}: {value!r}"
    )
    assert len(value) == 2, (
        f"Expected StatusTuple length 2, got {len(value)}: {value!r}"
    )

    ok, msg = value
    assert isinstance(ok, bool), (
        f"Expected bool success, got {type(ok).__name__}: {ok!r}"
    )
    assert isinstance(msg, str), (
        f"Expected str message, got {type(msg).__name__}: {msg!r}"
    )

    return ok, msg


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
