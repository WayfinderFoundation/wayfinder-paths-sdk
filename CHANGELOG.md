# Changelog

## [0.11.1] - 2026-09-09

Published to PyPI on 2026-09-09 (tag `v0.11.1`). The version string has read 0.11.1 on main since 2026-06-23 and no 0.11.1 wheel shipped before this date, so this entry covers everything merged from #445 through #782.

Added

1. **Solana support** (#521, #522, #524, #525, #530, #532, #534, #549, #553, #554, #574, #576, #584): Wallets are now EVM+SVM rings — create returns both addresses, reads come from the ring endpoint, and `core_get_wallets` includes SOL/SPL balances. Solana signing + broadcast with priority-fee fan-out across RPCs, base58 mint resolution in token lookups, Solana branches in `onchain_swap`/`onchain_send`, cross-chain swaps signed with the source-chain leg and paid to the destination-chain leg, and core prompt guidance.
2. **Gas sponsorship** (#446, #449, #450, #457): Remote-wallet transactions on sponsored chains (Ethereum, Base, Arbitrum, Polygon, BSC, Monad, MegaEth, Plasma, Robinhood) are broadcast by the backend with gas covered. Gated by the `privy_gas_sponsorship_enabled` feature switch; pre-broadcast 4xx rejections fall back to local sign-and-broadcast.
3. **Robinhood Chain (4663)** (#445) and **Uniswap v4 exact-in swaps** (#462) on Ethereum, Base, Arbitrum, and Robinhood for pools aggregators cannot reach.
4. **Mobile messaging agent** (#590, #591, #595, #598, #600, #608, #610, #614, #616, #618, #620, #621, #622, #650): `wayfinder-mobile` plain-text agent with iMessage effects, scheduled initiative check-ins with `<skip/>`, a hard 500-character / three-sentence reply contract, and permission overrides for the no-UI channel. Cannot be spawned as a subagent.
5. **SMS notifications** (#640, #682): `notification_send(delivery="email" | "sms")`; texts are server-gated by quiet hours, a daily budget, and dedupe, with `override=true` for urgent updates. Successful sends return the remaining daily text budget.
6. **Pattern Match quant workflow** (#594, #602, #605, #607, #609): Chart-selected historical analogue scans (formerly Fractal Scan) with a cached same-market baseline, labelled CEX proxy, perpetual outcome distributions, compact overlay handoff by `match_id`, and rejection of superseded forecasts.
7. **Hyperliquid state** (#448, #459, #611, #619): `hyperliquid_get_state` now includes open orders (resting limits + untriggered TP/SL), reports canonical `asset_name`, and returns a mode-aware `summary` with real margin fields (`unified_usdc_equity`, `unified_usdc_margin_used`, `unified_usdc_margin_available`, liquidation floor).
8. **Onchain token discovery** (#464, #465, #612): `onchain_list_tokens(chain_code, dimension)` for trending / volume / new / active tokens per chain, low-cap agent guidance, and canonical settlement safety checks.
9. **Contract ABIs via the Wayfinder API** (#771, #773): New transport-only `CONTRACT_CLIENT`; `contracts_call`, `contract_get_abi`, and proxy resolution no longer need an Etherscan key.
10. **Contract execution guard** (#526): `contracts_execute` refuses ERC20 `transfer` to a contract address unless `override=True`.
11. **Signing session renewal prompt** (#670, #674): Expired remote-signing sessions raise `SessionExpiredError`, mapped to an actionable `session_expired` result on every transaction path, including sponsored broadcast.
12. **Visual tools** (#451, #453): `visual_preview_series` dry-run and `visual_set_chart_indicators`; `visual_import_chart_spec` works on Shells and surfaces workspace API errors.
13. **Alpha Lab sort** (#593): `sort` arg on `research_search_alpha` (e.g. `-created` for newest-first).
14. **Wallets** (#689, #724): Instance id sent on remote wallet create; an all-wallets label for venue state tools.
15. **Paths runtime reliability** (#760): Shared async HTTP clients created lazily and closed explicitly across CLI / strategy / MCP boundaries, new paths pinned to the SDK they were built against, MCP packaged as a runtime dependency. Shell paths gain inventory sync context (#633).
16. **Runtime compatibility for installed paths** (#774, #778, #779): The rendered skill runner accepts any same-minor SDK at or above the pinned patch, 0.11.0 pins are floored to 0.11.1 at install time, `sign_and_send_transaction` is restored as a thin wrapper over `send_transaction` (dropped in #472 while the version stayed 0.11.0), and the bootstrap's packaging-free Python check handles zero-padded versions, wildcards, and compatible-release bounds.
17. **Direct-route swap prerequisites** (#780): `onchain_swap` and `BRAPAdapter.swap_from_quote` execute the quoted ERC20 and Permit2 approvals in sequence, waiting on each receipt before submitting a direct Pons/Uniswap route; source-chain transactions are validated for chain, sender, and atomic-only routes before signing.
18. **Cross-chain quote recipients** (#775): `best_quote` / `swap_from_token_ids` take a keyword-only `to_address`; EVM-to-Solana quotes without a destination wallet fail before any network call, and `include_calldata=true` returns an additive `execution_quote` with router, native value, chain, approval, and serialized SVM data.
19. **Path inspection permissions** (#781): Generated orchestrators and Bash-enabled workers opt into runtime-validated inspection permissions so routine read-only commands stop prompting; existing installs pick it up on reactivation.
20. **Sports desk-analyst workflow**: broad sports and market-edge prompts now produce a fast executable PM/HL board, a concise BUY/SELL/WATCH/SKIP shortlist, and defer heavy simulation until after candidates are selected.
21. **Sports data gateway tools**: added provider-agnostic `sports_snapshot`, `sports_backtest_state`, and hidden sports-worker provider facade support for bounded live sports data, run monitoring, and model workflows.
22. **Sports regression evals**: added coverage for World Cup prop scans, country/outright scans, fair-value delta framing, unavailable sports-tool fail-fast behavior, and HYPE/SPCX trade setup routing.

Changed

1. **Wallet API shape** (#521, #522): Remote wallet create returns `{"evm": ..., "svm": ...}`; wallet reads consume the ring-centric endpoint with the ring label as source of truth.
2. **Agent context** (#456, #461, #466, #467, #576): Supported-chains tables synced to all 12 chains in `SUPPORTED_CHAINS`, balances always re-pulled before reporting, higher step limits, and an OpenCode compaction prompt baseline.
3. **Hyperliquid info reads** (#696): Retry on 429 / 5xx / connection failures with the shared backoff policy; `get_user_state` documented as perp-only (#615).
4. **Docs and examples** (#550): Delta Lab APY examples corrected to the `directions.LONG/SHORT` envelope.
5. **Agent prompts** (#776): Hyperliquid **Fees** subsection so the agent reports platform fees factually and never suggests routing around them.
6. **Mobile agent subagents** (#782): `task` is now an allowlist (`"*": deny` plus the built-in Wayfinder agents), so path-installed orchestrators cannot be spawned from a messaging session where their permission prompts are unanswerable.
7. **Bonded upgrades** (#770): The owner-wallet argument is only required for an initial bonded publish; upgrades can omit it.
8. **Cleanup** (#472, #666, #777): Dead transaction utils removed; Apex GMX Velocity smoke test no longer gates on live market return (floor relaxed to -0.35 in #666, then dropped in #777).
9. **Prediction-market sports routing**: broad prop scans now check real sports markets before novelty word/phrase markets, hydrate surfaced buckets before global no-edge claims, and default live player-prop reads to bounded pages.
10. **Sports edge framing**: PM/HL cross-venue gaps are treated as venue-noise and liquidity context; recommendations focus on hypothesized fair-value delta versus executable price.
11. **Research influence flow**: research signals are ledgered as evidence/context or bounded model modifiers rather than silent freehand probability jumps.

Fixed

1. **Polymarket deposit wallets** (#454): Deposit addresses are resolved on-chain after Polymarket's factory beacon upgrade; locally derived addresses were unownable and stranded funds.
2. **Polymarket redeem** (#455): WCOL unwrap runs atomically inside the redeem batch; new sweep recovery tool for stranded WCOL.
3. **Hyperliquid deposit/withdraw on split-mode accounts** (#447): Fresh accounts credit perp, not spot; deposit no longer reports a false failure and withdraw finds the balance.
4. **Hyperliquid spot token names** (#523): Spot universe indexed by token `index` field instead of array position, recovering pairs shadowed after delistings.
5. **Runner backend sync** (#632): Sync thread gets its own DB connection; side-effect crashes are surfaced instead of silently killing the sync.
6. **Import cycle** (#647): `core.utils.wallets` and `core.utils.transaction` can each be imported first.
7. **Compaction plugin** (#551): Module-private template so OpenCode's plugin loader no longer drops the plugin.
8. **Polymarket read hydration**: price, order-book, and price-history reads can resolve exact or loose `market_slug` plus outcome when the agent does not already have a token id.
9. **Hyperliquid HIP-4 discovery**: added a dedicated outcome-market search wrapper so sports scans do not pull large unrelated perp/spot boards.
10. **Sports answer failure modes**: prompts now guard against repeated invalid sports-tool retries, unsupported model-to-market comparisons, and unscoped "no edge" conclusions.

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
