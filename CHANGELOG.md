# Changelog

## [0.11.0] - 2026-06-10

Added

1. **Polymarket v2** (#213): pUSD (V2 collateral) on Polygon with deposit wallets (#311), BRAP-routed bridge legs (#319), market-order slippage cap + AutoWrap (#326), vault-backend search (#334), and limit orders by `market_slug` + `outcome` (#424).
2. **HIP-4 outcome markets** (#239): Hyperliquid binary daily prediction contracts wired into the adapter and MCP tools — price-bucket markets (#295), named outcome markets like CPI (#388), Wayfinder builder code attached to outcome orders (#296), and collateral migrated from USDH to USDC (#386).
3. **Per-action MCP write tools** (#336, #360): `hyperliquid_execute` / `polymarket_execute` split into per-action tools (`hyperliquid_place_market_order`, `polymarket_redeem_positions`, etc.), and `core_execute` split into `onchain_swap` + `onchain_send`.
4. **Safety guards**: swap approval gate (#414), pre-flight balance guards for `onchain_swap`/`onchain_send` (#375), pUSD balance guard + collateral-suffixed deposit tools (#373), on-chain allowance polling after approval txs (#418), and Hyperliquid margin / runner monitor guardrails (#346).
5. **Pendle limit order execution** (#352): Taker fills and maker order support.
6. **Research gateway** (#301): New research SDK client and MCP tool, with sanitized Exa payloads (#425) and updated social X search response types (#376).
7. **Delta Lab v2** (#224): Updated time-series endpoints (#196), specific time-series defaults (#290), and APY filtering by type (#413).
8. **Packs MVP and path install flows** (#131, #220, #227, #230): Wayfinder packs with applet scaffolds (#302), split remote installs, dynamic strategy loading (#387), and paths DB sync from the SDK (#390).
9. **Wallet sessions** (#186, #190, #342): TTL-based session wallets (renamed to sessions), 15-minute default TTL, instance-filtered wallet lists (#192), and local wallets blocked on hosted instances (#189).
10. **Runner upgrades**: crontab notation for jobs (#433), per-job locks replacing the global lock (#404), event-driven bulk job sync to the backend (#399), idempotent `runner start` (#183), and job-completion session notifications (#182, #238).
11. **OpenCode agent platform**: subagent delegation (#331), email + SMS notifications (#200, #300), per-agent temperatures (#377), and visual pane / chart tooling (#288, #385).
12. **MCP tool execution metrics** (#372, #374): Fire-and-forget metrics and per-tool latency tracking.

Changed

1. **Hyperliquid UnifiedAccount migration** (#294): Moved off dexAbstraction mode; spot↔perp USD class transfers dropped (#321), builder fee set to 5 bps (#426), QuickNode info client split out (#408) with a whitelisted info dispatcher (#371).
2. **MCP surface overhaul**: every tool namespaced as `{namespace}_{name}` (#248), resources removed and folded into tools (#247, #254), registry organized reads-before-writes (#348), `web_search`/`web_fetch` moved to `core_` (#349), and `@catch_errors` + `throw_if_*` guards across all tools (#280, #283).
3. **Transaction layer hardening**: Polygon priority fee floor at 25 gwei (#379), per-RPC errors surfaced when gas estimation fails everywhere (#382), and nonce reads fanned out across the WF-proxy pool (#240).
4. **Backtesting**: faster runs (#427), completed-bars-only enforcement (#431), CCXT data sources allowed (#328), and clarified timing prompts (#434).
5. **Docs and agent prompts consolidated**: AGENTS.md merged into wayfinder.md (#347), default domain moved to wayfinder.ai (#393, #394), Terms cover all live domains (#395), and token id format docs clarified (#383).
6. **Constants hygiene**: inline ABIs moved into `core/constants` (#316), address checksum source-of-truth invariant (#315), Polymarket builder code hardcoded as a constant (#314).

Fixed

1. **HIP-4 order sizing** (#391, #415, #416): `usd_amount` minimum sizing corrected, `usd_amount` rejected on limit orders with an actionable error, and minimum-notional suggestions now survive lot-size rounding.
2. **Polymarket reliability**: outcome label resolved from `token_id` on market orders (#419), batch submit retried on relayer registry races (#338), unknown `wallet_label` surfaced instead of a generic error (#341), and structured Gamma errors (#398).
3. **Hyperliquid trigger orders** (#429): Non-reduce-only triggers allowed via an optional flag.
4. **Runner daemon lock contention** (#401, #403): Dropped redundant SQLite lock (WAL serializes) and cleaned up daemon locking.
5. **MCP server broken outside OpenCode instances** (#402).
6. **Duplicate `/v1` in OpenCode client URLs** (#204).
7. **Adapter audits**: Moonwell chain coverage (#361) and Morpho API field corrections (#355).

## [0.10.0] - 2026-03-31

Added

1. **Remote signing** (#169, #170): Server-side transaction signing via Privy, enabling hosted execution without local private keys. Docs and integration guide included.
2. **Aerodrome adapter** (#163): Classic Aerodrome pools on Base — market discovery, route/liquidity quoting, LP/gauge state, veAERO voting, and reward claims.
3. **Aerodrome Slipstream adapter** (#166): Concentrated liquidity on Base — pool discovery, position reads, mint/increase/decrease flows, gauge staking, and veAERO-linked reward claims.
4. **SparkLend adapter** (#151, #160): Refactored from Aave V3 base with SparkLend-specific market reads, user state, supply/withdraw, borrow/repay, collateral, rewards, and native-token flows. Skill docs (#161).
5. **Polymarket book-based quote support** (#178): Quote swap prices from Polymarket orderbook depth.
6. **New chains** (#156): Added Katana, Monad, and MegaETH chain support.
7. **AGENTS.md** (#174): Codegen agent guidelines for the repository.

Changed

1. **Signing cleanup** (#167): Consolidated wallet/signing utilities, one global constant replacing scattered duplicates (#165).
2. **Boros vault views and docs improved** (#177): Enhanced vault read patterns and updated skill documentation.
3. **Eigencloud adapter readme** (#168): Expanded docs for EigenLayer restaking adapter.
4. **Etherfi Claude skills docs** (#159): Added skill documentation for ether.fi adapter.
5. **SDK skill coverage refreshed** (#179): Updated all protocol skill docs to reflect current adapter APIs.

Fixed

1. **Backtesting bugs** (#162): Missing config field and duplicate timestamp handling fixed.
2. **Multi-venue backtest docs and behaviour** (#164): Corrected docs and logic for multi-venue backtest runs.
3. **Backtesting debt handling** (#158): Fixed incorrect debt accounting in backtest simulations.

## [0.9.0] - 2026-03-16 (a789e2d30d1f1ac540a859ee6d2587649f066cc6)

Added

1. **Alpha Lab integration** (#141, #144): Scored alpha insight feed (`AlphaLabClient`) surfacing actionable DeFi signals (tweets, chain flows, APY highlights, delta-neutral pairs). MCP resources for search and type listing (`wayfinder://alpha-lab/...`). Claude skill (`/using-alpha-lab`) with docs, gotchas, and response structures.
2. **Etherfi adapter** (#140): Full protocol adapter with ABI constants, read/write support, Gorlami simulation tests, and unit tests.
3. **Boros vault split strategy** (#142): `multi_vault_split_strategy` distributing capital across Boros vaults with isolated-only deposit support. Multicall/caching optimizations, strategy logging, expanded Boros adapter with vault workflows, golden tests, and Gorlami simulation tests.
4. **Yield strategy backtesting** (#139): New `yield_strategies.py` module for carry trade, delta-neutral, and yield rotation backtests. Example scripts, existing-strategy reproduction workflow, and `matplotlib` dependency added.

Changed

1. **Basis strategy rotation hardened** (#147): Improved rotation logic with leg repair flow fixes and 410+ lines of new test coverage.
2. **Gorlami auth and URL simplification** (#149): Simplified auth and URL handling in `GorlamiTestnetClient` and test helpers.
3. **Pendle skill wallet label fix** (#146): Fixed wallet label handling and added PT redemption docs.
4. Claude docs updated: Alpha Lab MCP resources, screening resources, expanded protocol table, refreshed strategy READMEs (#148, #144).

## [0.8.0] - 2026-03-05 (252e0e018ac10143779785bb4ddba5087267cbb7)

Added

1. **Delta Lab client and MCP resources** (#69): Full yield-discovery client (`DeltaLabClient`) with basis APY sources, delta-neutral pair finding, top APY ranking, and screening endpoints (price, lending, perp, borrow routes). MCP resources for quick queries (`wayfinder://delta-lab/...`). Includes asset search by ID/address and chain-based filters (#135).
2. **Backtesting framework** (`core/backtesting/`): `quick_backtest` and `run_backtest` with automatic data fetching from Delta Lab and Hyperliquid, realistic transaction costs, funding rate integration, liquidation simulation, multi-leverage testing, and comprehensive stats (Sharpe, Sortino, CAGR, max drawdown, profit factor).
3. **Euler v2 adapter** (#104): EVK/eVault lending and borrowing on Ethereum — vault market discovery, APYs, positions, and EVC-batched lend/borrow flows with Claude skill docs.
4. **Ethena sUSDe vault adapter** (#117): Spot APY reads, cooldown/position queries, and USDe→sUSDe stake/unstake flows on Ethereum mainnet with Claude skill docs (#133).
5. **Lido adapter** (#121): wstETH staking/unstaking on Ethereum with safety guards, `require_wallet` decorator, and Gorlami simulation tests.
6. **Eigencloud adapter** (#127): EigenLayer restaking integration with withdrawal root tracking and Gorlami simulation coverage.
7. **Web3 multicall utility** (#129): Batched read-only contract calls via `Multicall3` (`core/utils/multicall.py`) with chain support detection and tests.
8. **Hyperliquid stop-loss and trigger orders** (#134): New order types added to the Hyperliquid MCP execution tool.

Changed

1. `require_wallet` decorator moved to shared `core/adapters/BaseAdapter.py` (#124) — adapters no longer duplicate wallet-check logic.
2. Claude docs and skills expanded: backtesting skill, Ethena vault skill, Euler v2 skill, Delta Lab skill, Avantis skill, and updated Boros/Hyperliquid skill docs.

## [0.7.0] - 2026-02-23 (5919548c8b95964e89854a51f68cef92168710b1)

**Breaking Changes**

1. Adapter constructor signatures standardized (#101): `strategy_wallet_signing_callback` → `sign_callback`, with explicit `wallet_address` parameter. Config-based wallet resolution removed from adapter constructors.
2. BalanceAdapter now takes `main_sign_callback`/`main_wallet_address` + `strategy_sign_callback`/`strategy_wallet_address` (previously `main_wallet_signing_callback`/`strategy_wallet_signing_callback`).
3. `get_adapter()` in `mcp/scripting.py` refactored to introspect adapter `__init__` signatures — direct adapter instantiation now requires explicit parameters with no config fallback.

Added

1. Solidity contract tooling (#106): compilation via solcx (solc 0.8.26, OpenZeppelin v5), MCP tools (`compile_contract`, `deploy_contract`, `contract_execute`, `contract_get_abi`), Etherscan V2 verification, proxy ABI support, artifact persistence, and `/contract-development` skill.
2. Avantis adapter (#103): ERC-4626 avUSDC LP vault on Base with `deposit()`/`withdraw()` flows.
3. MCP strategy integration tests (#97) and hyperlend_stable_yield strategy smoke test (#98).

Changed

1. Aave V3 contract addresses stored lowercase; removed redundant checksumming helpers (#100).
2. Avantis README updated to reflect `deposit()`/`withdraw()` naming (#108).

Fixed

1. Reward APR now converted to APY before combining with base APY in Aave V3 `get_all_markets()`/`get_user_state()` (#95).
2. Slippage parameter now passed through to BRAP quote calls (#76).
3. Polymarket `_normalize_market()` no longer crashes on markets missing `outcomes`/`outcomePrices`/`clobTokenIds` fields (#92).

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
