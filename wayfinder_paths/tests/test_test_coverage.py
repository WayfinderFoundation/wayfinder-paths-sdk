import json
from pathlib import Path

import pytest


def test_all_adapters_have_tests():
    adapters_dir = Path(__file__).parent.parent / "adapters"

    if not adapters_dir.exists():
        pytest.skip("Adapters directory not found")

    missing_tests = []

    # Find all adapter directories (directories containing adapter.py)
    for adapter_dir in adapters_dir.iterdir():
        if not adapter_dir.is_dir() or adapter_dir.name.startswith("_"):
            continue

        adapter_py = adapter_dir / "adapter.py"
        test_adapter_py = adapter_dir / "test_adapter.py"

        # If adapter.py exists, test_adapter_py must exist
        if adapter_py.exists() and not test_adapter_py.exists():
            missing_tests.append(adapter_dir.name)

    if missing_tests:
        pytest.fail(
            f"The following adapters are missing test files:\n"
            f"{', '.join(missing_tests)}\n"
            f"Please create test_adapter.py for each adapter."
        )


def test_all_strategies_have_tests():
    strategies_dir = Path(__file__).parent.parent / "strategies"

    if not strategies_dir.exists():
        pytest.skip("Strategies directory not found")

    missing_tests = []

    # Find all strategy directories (directories containing strategy.py)
    for strategy_dir in strategies_dir.iterdir():
        if not strategy_dir.is_dir() or strategy_dir.name.startswith("_"):
            continue

        strategy_py = strategy_dir / "strategy.py"
        test_strategy_py = strategy_dir / "test_strategy.py"

        # If strategy.py exists, test_strategy.py must exist
        if strategy_py.exists() and not test_strategy_py.exists():
            missing_tests.append(strategy_dir.name)

    if missing_tests:
        pytest.fail(
            f"The following strategies are missing test files:\n"
            f"{', '.join(missing_tests)}\n"
            f"Please create test_strategy.py for each strategy."
        )


def test_all_strategies_have_examples_json():
    strategies_dir = Path(__file__).parent.parent / "strategies"

    if not strategies_dir.exists():
        pytest.skip("Strategies directory not found")

    missing_examples = []

    # Find all strategy directories (directories containing strategy.py)
    for strategy_dir in strategies_dir.iterdir():
        if not strategy_dir.is_dir() or strategy_dir.name.startswith("_"):
            continue

        strategy_py = strategy_dir / "strategy.py"
        examples_json = strategy_dir / "examples.json"

        # If strategy.py exists, examples.json must exist
        if strategy_py.exists() and not examples_json.exists():
            missing_examples.append(strategy_dir.name)

    if missing_examples:
        pytest.fail(
            f"The following strategies are missing examples.json files:\n"
            f"{', '.join(missing_examples)}\n"
            f"examples.json is REQUIRED for all strategies.\n"
            f"See TESTING.md for the required structure."
        )


def test_strategy_tests_use_examples_json():
    strategies_dir = Path(__file__).parent.parent / "strategies"

    if not strategies_dir.exists():
        pytest.skip("Strategies directory not found")

    violations = []

    # Find all strategy test files
    for strategy_dir in strategies_dir.iterdir():
        if not strategy_dir.is_dir() or strategy_dir.name.startswith("_"):
            continue

        test_file = strategy_dir / "test_strategy.py"
        if not test_file.exists():
            continue

        try:
            content = test_file.read_text()

            # (wayfinder_paths/ is added to path by conftest.py or inline)
            has_import = (
                "from tests.test_utils import load_strategy_examples" in content
                or "from wayfinder_paths.tests.test_utils import load_strategy_examples"
                in content
                or "import tests.test_utils" in content
                or "import wayfinder_paths.tests.test_utils" in content
                or (
                    "tests.test_utils" in content
                    and "load_strategy_examples" in content
                )  # fallback importlib pattern
            )

            has_usage = "load_strategy_examples" in content

            # If it doesn't use the shared utility, check for alternative patterns
            if not (has_import and has_usage):
                has_hardcoded = (
                    'Path(__file__).parent / "examples.json"' in content
                    or "examples.json" in content
                ) and "load_strategy_examples" not in content

                if has_hardcoded:
                    violations.append(
                        f"{strategy_dir.name}: Uses hardcoded examples.json loading "
                        f"instead of load_strategy_examples() from tests.test_utils"
                    )
                elif has_usage and not has_import:
                    violations.append(
                        f"{strategy_dir.name}: Uses load_strategy_examples but missing import"
                    )
                elif not has_usage:
                    violations.append(
                        f"{strategy_dir.name}: Test file does not appear to load examples.json"
                    )
        except Exception as e:
            violations.append(f"{strategy_dir.name}: Error reading test file: {e}")

    if violations:
        pytest.fail(
            f"The following strategy tests need to use load_strategy_examples():\n"
            f"{chr(10).join(violations)}\n"
            f"All strategy tests MUST use load_strategy_examples() from tests.test_utils.\n"
            f"See TESTING.md for examples."
        )


def test_strategy_examples_have_smoke():
    strategies_dir = Path(__file__).parent.parent / "strategies"

    if not strategies_dir.exists():
        pytest.skip("Strategies directory not found")

    missing_smoke = []

    # Find all strategy directories
    for strategy_dir in strategies_dir.iterdir():
        if not strategy_dir.is_dir() or strategy_dir.name.startswith("_"):
            continue

        strategy_py = strategy_dir / "strategy.py"
        examples_json = strategy_dir / "examples.json"

        if not strategy_py.exists():
            continue

        if not examples_json.exists():
            # This will be caught by test_all_strategies_have_examples_json
            continue

        try:
            with open(examples_json) as f:
                examples = json.load(f)

            if "smoke" not in examples:
                missing_smoke.append(strategy_dir.name)
        except json.JSONDecodeError:
            missing_smoke.append(f"{strategy_dir.name} (invalid JSON)")
        except Exception as e:
            missing_smoke.append(f"{strategy_dir.name} (error: {e})")

    if missing_smoke:
        pytest.fail(
            f"The following strategies' examples.json are missing 'smoke' entry:\n"
            f"{', '.join(missing_smoke)}\n"
            f"All strategies MUST have a 'smoke' example in examples.json.\n"
            f"See TESTING.md for the required structure."
        )
