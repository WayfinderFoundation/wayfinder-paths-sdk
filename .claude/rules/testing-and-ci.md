---
paths: ["**/test_*.py", "**/tests/**", "**/*_test.py"]
---

# Testing & CI

## Testing Requirements

### Strategies

- **Required**: `examples.json` file (documentation + test data)
- **Required**: Smoke test exercising deposit → update → status → withdraw
- **Required**: Tests must load data from `examples.json`, never hardcode values

### Adapters

- **Required**: Basic functionality tests with mocked dependencies
- **Optional**: `examples.json` file

### Test Markers

- `@pytest.mark.smoke` - Basic functionality validation
- `@pytest.mark.requires_wallets` - Tests needing local wallets configured
- `@pytest.mark.requires_config` - Tests needing config.json

## Configuration

Config priority: Constructor parameter > config.json > Environment variable (`WAYFINDER_API_KEY`)

Copy `config.example.json` to `config.json` (or run `python3 scripts/setup.py`) for local development.

## CI/CD Pipeline

PRs are tested with:

1. Lint & format checks (Ruff)
2. Smoke tests
3. Adapter tests (mocked dependencies)
4. Integration tests (PRs only)
5. Security scans (Bandit, Safety)

## Key Patterns

- Adapters compose one or more clients and raise `NotImplementedError` for unsupported ops
- All async methods use `async/await` pattern
- Return types are `StatusTuple` (success bool, message str) or `StatusDict` (portfolio data)
- Wallet generation updates `config.json` in repo root
- Per-strategy wallets are created automatically via `just create-strategy`

## Publishing

Publishing to PyPI is restricted to `main` branch. Order of operations:

1. Merge changes to main
2. Bump version in `pyproject.toml`
3. Run `just publish`
4. Then dependent apps can update their dependencies
