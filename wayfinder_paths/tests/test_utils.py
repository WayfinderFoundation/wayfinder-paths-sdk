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


def assert_quote_result(value: Any) -> dict[str, Any]:
    assert isinstance(value, dict), (
        f"Expected QuoteResult (dict), got {type(value).__name__}: {value!r}"
    )

    for key in ("expected_apy", "apy_type", "summary"):
        assert key in value, f"Missing required quote key '{key}': {value!r}"

    expected_apy = value["expected_apy"]
    assert isinstance(expected_apy, (int, float)) and not isinstance(
        expected_apy, bool
    ), (
        f"expected_apy must be a number, got {type(expected_apy).__name__}: {expected_apy!r}"
    )

    apy_type = value["apy_type"]
    assert isinstance(apy_type, str), (
        f"apy_type must be str, got {type(apy_type).__name__}: {apy_type!r}"
    )

    summary = value["summary"]
    assert isinstance(summary, str), (
        f"summary must be str, got {type(summary).__name__}: {summary!r}"
    )

    if (deposit_amount := value.get("deposit_amount")) is not None:
        assert isinstance(deposit_amount, (int, float)) and not isinstance(
            deposit_amount, bool
        ), (
            f"deposit_amount must be a number or None, got {type(deposit_amount).__name__}: {deposit_amount!r}"
        )

    if (as_of := value.get("as_of")) is not None:
        assert isinstance(as_of, str), (
            f"as_of must be str or None, got {type(as_of).__name__}: {as_of!r}"
        )

    if (components := value.get("components")) is not None:
        assert isinstance(components, dict), (
            f"components must be dict or None, got {type(components).__name__}: {components!r}"
        )

    return value


def assert_status_dict(value: Any) -> dict[str, Any]:
    assert isinstance(value, dict), (
        f"Expected StatusDict (dict), got {type(value).__name__}: {value!r}"
    )

    for key in (
        "portfolio_value",
        "net_deposit",
        "strategy_status",
        "gas_available",
        "gassed_up",
    ):
        assert key in value, f"Missing required status key '{key}': {value!r}"

    portfolio_value = value["portfolio_value"]
    assert isinstance(portfolio_value, (int, float)) and not isinstance(
        portfolio_value, bool
    ), (
        f"portfolio_value must be a number, got {type(portfolio_value).__name__}: {portfolio_value!r}"
    )

    net_deposit = value["net_deposit"]
    assert isinstance(net_deposit, (int, float)) and not isinstance(
        net_deposit, bool
    ), (
        f"net_deposit must be a number, got {type(net_deposit).__name__}: {net_deposit!r}"
    )

    gas_available = value["gas_available"]
    assert isinstance(gas_available, (int, float)) and not isinstance(
        gas_available, bool
    ), (
        f"gas_available must be a number, got {type(gas_available).__name__}: {gas_available!r}"
    )

    gassed_up = value["gassed_up"]
    assert isinstance(gassed_up, bool), (
        f"gassed_up must be bool, got {type(gassed_up).__name__}: {gassed_up!r}"
    )

    return value


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
