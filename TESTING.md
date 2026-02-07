# Testing Guide

This guide covers testing strategies and adapters in Wayfinder Paths.

## Quick Start

```bash
# 1. Install dependencies
poetry install

# 2. Generate test wallets
just create-wallets
# Or: poetry run python scripts/make_wallets.py -n 1

# 3. Run smoke tests
poetry run pytest -k smoke -v
```

## Scenario Testing (Simulation / Forks)

Unit tests catch regressions, but complex fund-moving flows (multi-step swaps, lending loops, approvals) should also be validated with at least one **dry-run scenario** on a fork before running live.

This repo supports fork-based dry-runs via Gorlami. See `SIMULATION.md` for setup and examples.

## Testing Strategies

### Required: Smoke Test with examples.json

Strategies must use `examples.json` for test data. This keeps documentation and tests in sync.

Create `wayfinder_paths/strategies/my_strategy/test_strategy.py`:

```python
import pytest
from pathlib import Path
from tests.test_utils import load_strategy_examples
from .strategy import MyStrategy


@pytest.mark.asyncio
async def test_smoke():
    """Basic strategy lifecycle test."""
    examples = load_strategy_examples(Path(__file__))
    smoke_example = examples["smoke"]

    s = MyStrategy()

    # Deposit
    deposit_params = smoke_example.get("deposit", {})
    ok, _ = await s.deposit(**deposit_params)
    assert ok

    # Update
    ok, _ = await s.update()
    assert ok

    # Status
    st = await s.status()
    assert "portfolio_value" in st

    # Withdraw
    ok, _ = await s.withdraw()
    assert ok
```

### examples.json Structure

```json
{
  "smoke": {
    "deposit": {"main_token_amount": 100, "gas_token_amount": 0.01},
    "update": {},
    "status": {},
    "withdraw": {}
  },
  "error_case": {
    "deposit": {"main_token_amount": 0},
    "expect": {"success": false}
  }
}
```

### Running Strategy Tests

```bash
# Test specific strategy
poetry run pytest wayfinder_paths/strategies/my_strategy/ -v

# Run smoke tests only
poetry run pytest wayfinder_paths/strategies/my_strategy/ -k smoke -v

# All strategy tests
poetry run pytest wayfinder_paths/strategies/ -v
```

## Testing Adapters

Adapters don't require `examples.json`. Focus on functionality tests with mocked dependencies.

Create `wayfinder_paths/adapters/my_adapter/test_adapter.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch
from .adapter import MyAdapter


class TestMyAdapter:
    @pytest.fixture
    def adapter(self):
        return MyAdapter()

    @pytest.mark.asyncio
    async def test_get_data_success(self, adapter):
        """Test adapter method with mocked client."""
        with patch.object(adapter, "client") as mock_client:
            mock_client.get_data = AsyncMock(return_value={"data": "test"})

            success, data = await adapter.get_data(param="value")

            assert success
            assert "data" in data

    @pytest.mark.asyncio
    async def test_get_data_failure(self, adapter):
        """Test error handling."""
        with patch.object(adapter, "client") as mock_client:
            mock_client.get_data = AsyncMock(side_effect=Exception("API error"))

            success, data = await adapter.get_data(param="value")

            assert not success
            assert "error" in data.lower()
```

### Running Adapter Tests

```bash
# Test specific adapter
poetry run pytest wayfinder_paths/adapters/my_adapter/ -v

# All adapter tests
poetry run pytest wayfinder_paths/adapters/ -v
```

## Test Commands Reference

```bash
# Run all tests
poetry run pytest -v

# Run smoke tests only
poetry run pytest -k smoke -v

# Run with coverage
poetry run pytest --cov=wayfinder_paths -v

# Run specific test file
poetry run pytest wayfinder_paths/strategies/my_strategy/test_strategy.py -v

# Run specific test function
poetry run pytest wayfinder_paths/strategies/my_strategy/test_strategy.py::test_smoke -v

# Run tests in parallel
poetry run pytest -n auto -v
```

## What to Test

### Strategies (Minimum)
- Smoke test: deposit -> update -> status -> withdraw
- All examples in `examples.json` work correctly
- Error cases return `(False, message)` not exceptions

### Adapters (Minimum)
- Each public method works with valid input
- Error handling returns `(False, message)` tuples
- Methods handle missing/invalid parameters gracefully

### Best Practices
- Mock external dependencies (APIs, blockchain calls)
- Test behavior, not implementation details
- Don't assert on specific error message text
- Keep tests fast and isolated

## CI/CD Pipeline

Pull requests run these checks:
- Lint & format (ruff)
- Smoke tests
- Adapter tests
- Security scans (bandit, safety)

Make sure tests pass locally before pushing:

```bash
# Run all checks
poetry run pytest -k smoke -v
poetry run ruff check .
```

## Troubleshooting

### Missing config.json
```bash
just create-wallets
```

### Import errors
- Run tests from repository root
- Use `poetry run pytest` not just `pytest`
- Check import paths match package structure

### Async test issues
- Use `@pytest.mark.asyncio` decorator
- Make sure test function is `async def`
- Install `pytest-asyncio`

### Test not found
- File must be named `test_*.py`
- Functions must be named `test_*`
- Classes must be named `Test*`
