# Changelog

## [0.2.0] - 2026-02-06

Added

1. Hyperliquid Spot support.
2. Project-local runner scheduler.
3. CLI support for other platforms.
4. Strategy + Adapter creation script.
5. Plasma HL deposit wait.

Changed

1. Hyperliquid utils no longer a class; removed dead functions.
2. Hyperliquid utils squashed into Exchange.

Fixed

1. Zero address handling for native tokens in swap quoting.
2. Strategy status tuples bug.
3. Withdraw failure due to unexpected kwargs.
4. policies now async + awaited.
5. CLI vars return None when not provided.

Chore / Docs

1. Remove dead simulation param.
2. Remove defensive import / variable reassignment.
3. Update repo clone URL in README.
