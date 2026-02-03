lint:
    @poetry run ruff check --fix

format:
    @poetry run ruff format

setup:
    @python3 scripts/setup.py


# Generate N dev wallets and write/update config.json in repo root
# First wallet is labeled "main" if main doesn't exist, others are labeled "temporary_N"
wallets N:
    @poetry run python scripts/make_wallets.py -n {{N}}

# Generate N dev wallets and also write geth-compatible keystores with PASSWORD
# First wallet is labeled "main" if main doesn't exist, others are labeled "temporary_N"
wallets-ks N PASSWORD:
    @poetry run python scripts/make_wallets.py -n {{N}} --keystore-password {{PASSWORD}}

# Run all smoke tests
test-smoke:
    @poetry run pytest -k smoke -v

# Test a specific strategy
test-strategy STRATEGY:
    @poetry run pytest wayfinder_paths/strategies/{{STRATEGY}}/ -v

# Test a specific adapter
test-adapter ADAPTER:
    @poetry run pytest wayfinder_paths/adapters/{{ADAPTER}}/ -v

# Run all tests
test:
    @poetry run pytest -v

# Run tests with coverage
test-cov:
    @poetry run pytest --cov=wayfinder-paths --cov-report=html -v


# Create a new strategy from template with dedicated wallet
# Usage: just create-strategy "My Strategy Name"
create-strategy NAME:
    @poetry run python scripts/create_strategy.py "{{NAME}}"

# Create a new strategy from template with dedicated wallet (override existing)
create-strategy-force NAME:
    @poetry run python scripts/create_strategy.py "{{NAME}}" --override

# Create a wallet with a strategy name label
# Usage: just create-wallet "hyperlend_stable_yield_strategy"
# If wallet with that label already exists, it will be skipped
create-wallet STRATEGY_NAME:
    @poetry run python scripts/make_wallets.py --label {{STRATEGY_NAME}}

# Create main wallet for initial setup
# Creates a main wallet if it doesn't exist
create-wallets:
    @poetry run python scripts/make_wallets.py -n 1

# Build the package distribution files (wheel and source distribution)
build:
    @poetry build

publish:
    @poetry build
    @bash scripts/publish_to_pypi.sh
