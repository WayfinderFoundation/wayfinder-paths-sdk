# Changelog

## [0.6.1] - 2026-02-16 (57da66ca33a10fd68d128c80970ac989d6addb7e)

Added

1. `from_erc20_raw()` utility in `units.py` — replaces manual `float(x) / (10 ** decimals)` patterns across adapters and strategies.
2. GitHub Actions workflow for Claude Code.

Changed

1. Replaced duplicate raw-to-float conversions in balance, boros, and projectx adapters with `from_erc20_raw()`.
2. Removed redundant `_get_strategy/main_wallet_address()` overrides in stablecoin_yield and basis_trading strategies (identical to base class).
3. Simplified `config.py` (redundant `isinstance` checks), `transaction.py` (defensive guards, bare `except`), and `projectx.py` (already-narrowed type checks).
4. Moved inline import in `runner/daemon.py` to top-level.
5. Removed self-documenting comments in pendle and boros_hype adapters/strategies.
6. Polymarket CLOB URL switched from proxy to official endpoint (`clob.polymarket.com`).

## [0.6.0] - 2026-02-15 (262f633b8ea2d0b87fee83f0ed2b042b8ec4b0e2)

Added

1. Morpho Blue adapter with vault discovery, rewards, public allocator, and multi-chain fork simulation.
2. Aave V3 adapter with lending/borrowing, collateral management, and fork simulation.
3. Standardized user snapshot format across lending adapters.
4. Market risk and supply cap fields surfaced in Moonwell and Hyperlend adapters.
5. Merkl, Morpho, and MorphoRewards clients in core.
6. Retry utilities for Gorlami fork RPC calls.

Changed

1. Hyperlend manifest updated with missing capabilities (borrow, repay, collateral toggles).
2. Hyperlend stable yield strategy simplified — removed symbol wrapper methods.
3. Gorlami testnet client refactored with unified retry logic and multi-chain support.

## [0.5.0] - 2026-02-14 (57cac507e8e00165f9027b30584e93ff2d7f596b)

Added

1. Moonwell and Hyperlend market views, including expanded adapter support, constants/ABI coverage, and symbol utilities for market-level reads.
2. Hyperlend borrow/repay flows, including ERC-20 and native-token paths, plus full-repay handling and test coverage.
3. Polymarket bridge preflight checks with broader adapter test coverage.

Changed

1. Quote flow cleanup in MCP swap tooling, including corresponding quote test updates.
2. Documentation updates across adapter READMEs, high-value read rules, and config/readme references for the new market view capabilities.

## [0.4.1] - 2026-02-13 (1277255355859b1d11a082bb445e23541fe2ca19)

Added

1. CCXT adapter for multi-exchange reads & trades (Binance, Hyperliquid, Aster, etc.).
2. Wallet generation from BIP-39 mnemonic phrase.
3. Polymarket search filters, trimmed search/trending returns, and funding prompt updates.
4. Wayfinder RPCs and user RPC overrides.

Changed

1. Approvals are now automatic; fixed missing approval flows.
2. Replaced `load_config_json()` calls with `CONFIG` constant.
3. Removed redundant type casts, defensive code patterns, and redundant comments.
4. ProjectX swaps pagination support.

Fixed

1. `resolve_token_meta` for reverse token lookups.
2. Native tokens not handled properly in swaps.
3. Claude-vacuum workflow (invalid model input, lint/format).

## [0.3.0] - 2026-02-10 (dcd133eecc7d36e8051f5ba690e0fdfa1493d41d)

Added

1. Polymarket adapter and MCP tools.
2. ProjectX adapter and THBILL/USDC strategy.
3. Uniswap adapter support with shared math/utilities and tests.
4. VNet simulation via API.

Changed

1. Hyperliquid adapter refactor (cleanup, exchange consolidation, HIP3 updates).
2. Strategy runtime and multiple strategy implementations.
3. MCP wallet/address resolution and Gorlami configuration behavior.

Fixed

1. Type-checking and compatibility issues across adapters and utilities.
2. Moonwell portfolio value calculation (removed gas component).
3. Frontend open-orders path by removing unused functions and simplifying flow.

Chore / Docs

1. Added Claude vacuum workflow and related CI configuration updates.
2. Updated dependency and Python environment files.
3. Expanded adapter/testing documentation and simulation scripts.

## [0.2.0] - 2026-02-06 (4d13d6c0dc131f2e4469db60a3058e215b5b8fd1)

Added

1. Hyperliquid Spot support.
2. Project-local runner scheduler.
3. CLI support for other platforms.
4. Strategy + Adapter creation script.
5. Added Plasma chain support (chain ID 9745) with default RPCs.

Changed

1. Hyperliquid utils no longer a class; removed dead functions.
2. Hyperliquid utils squashed into Exchange.

Fixed

1. Zero address handling for native tokens in swap quoting.
2. Strategy status tuples bug.
3. Withdraw failure due to unexpected kwargs.
4. policies now async + awaited.
5. CLI vars return None when not provided.
6. Improved Hyperliquid deposit confirmation (ledger-based checks, avoids extra wait).

Chore / Docs

1. Remove dead simulation param.
2. Remove defensive import / variable reassignment.
3. Update repo clone URL in README.
