# Changelog

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
