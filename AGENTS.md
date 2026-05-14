# AGENTS.md

## Project Overview

Wayfinder Paths is a public Python SDK for DeFi trading strategies and adapters. It provides the building blocks for automated trading: adapters (exchange/protocol integrations), strategies (trading algorithms), and clients (low-level API wrappers).

## Personality

- Cost Efficient, you don't waste time exploring random information, you only call tools minimally, everything has a strong time cost.
- Precise, you always understand and execute the user's requirements exactly.

## Notes

- If confused about wallet balances, fetch fresh balances! Since the user has the private key and other ways to fund wallets, they might have modified wallet state themselves, we want to proactively check misalignments in wallet expectations.

## First-Time Setup (Auto-detect)

**IMPORTANT: On every new conversation, Detect Shells Instance first.**

Probe `http://localhost:4096/global/health`. If it returns `{ "healthy": true, ... }`, you are running inside a Shells instance — the SDK is already installed at `/wf/sdk`, the API key is already in the environment, and remote wallets are managed for you. **Do NOT run `setup.py`, do NOT prompt for an API key, do NOT touch `config.json`** — proceed normally.

## Wallets

**On Wayfinder Shells Instances, ALL wallets MUST be remote. No local wallets — ever.** Remote wallets are managed for you and provide analytics, activity tracking, and session-aware policies. Local wallets are invisible to the rest of the platform and break those guarantees. The `wallets` MCP tool enforces this and will reject local-wallet creation when running on Wayfinder Shells.

**Always read wallets through the MCP tools below. Never grep `config.json` for `wallets[]` or read wallet files directly.** They are the only source of truth — on Wayfinder Shells the remote wallets are not in `config.json`, so reading the file misses them entirely.

| Tool                             | What you get                                                                                            |
| -------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `core_get_wallets()`             | Every wallet — label, address, profile, tracked protocols, and USD-aggregated per-chain balances inline |
| `core_get_wallets(label="X")`    | Single wallet by label, same shape                                                                      |
| `onchain_get_wallet_activity(…)` | Recent on-chain activity (best-effort)                                                                  |

- **Session wallet** (default, recommended for normal trading) — 1-hour TTL, refreshed while the user has the UI open.
- **Strategy wallet** — 7-day TTL, intended for longer-running scheduled automation that signs without a human in the loop.

On a Wayfinder Shells Instance, always pass `remote=True` when creating wallets — local wallets are rejected.

In Python scripts, use the helpers in `wayfinder_paths.core.utils.wallets` (`load_wallets`, `find_wallet_by_label`) — they hit the same code path as `core_get_wallets` and return remote wallets transparently. See `/writing-wayfinder-scripts`.

## Wayfinder Shells Instance Environment Variables

When the SDK runs inside Wayfinder Shells, two env vars are injected at startup:

| Variable               | What it is                                                                             |
| ---------------------- | -------------------------------------------------------------------------------------- |
| `WAYFINDER_API_KEY`    | The user's `wf_…` Wayfinder API key. Picked up automatically by config priority below. |
| `OPENCODE_INSTANCE_ID` | The Wayfinder Shells identifier for this runtime. Useful for logs / diagnostics.       |

## Safety defaults

- **Quote before swap (MANDATORY):** Before executing any swap, always quote first. Verify the resolved `from_token` and `to_token` (symbol, address, chain) match intent, then show the user the route, estimated output, and fee. Only proceed after the user confirms.
- **Route planning for non-trivial swaps:** Before quoting, assess whether a direct route is likely to exist between the two tokens. If the pair is illiquid, cross-chain, or involves a long-tail token, reason through candidate intermediate hops first (e.g. tokenA → USDC → tokenB). Quote the most promising paths and compare outputs.

Transaction outcome rules (don't assume a tx hash means success):

- A transaction is only successful if the on-chain receipt has `status=1`.
- The SDK raises `TransactionRevertedError` when a receipt returns `status=0`.
- If a fund-moving step fails/reverts, stop the flow and report the error; don't continue executing dependent steps.

## Simulation / scenario testing (vnet only)

- Before broadcasting complex fund-moving flows live, run at least one forked **dry-run scenario** (Gorlami). These are EVM virtual testnets (vnets) that simulate **sequential on-chain operations** with real EVM state changes.
- **Cross-chain:** For flows spanning multiple EVM chains, spin up a fork per chain. Execute the source tx on the source fork, seed the expected tokens on the destination fork (simulating bridge delivery), then continue on the destination fork.
- **Scope:** Vnets only cover EVM chains (Base, Arbitrum, etc.). Off-chain or non-EVM protocols like Hyperliquid **cannot** be simulated.

## Data accuracy (no guessing)

When answering questions about **rates/APYs/funding**:

- Never invent or estimate values.
- Always fetch the value via an adapter/client/tool call when possible.
- Before searching external docs, consult this repo's own adapters/clients (and their `manifest.yaml` + `examples.json`) first.
- If you cannot fetch it, say so explicitly and provide the exact call/script needed to fetch it.

## MCP vs scripting — pick the right tool

Prefer **MCP tools** for simple, one-shot actions: a single quote, a single swap, reading a
balance, placing one order, querying a strategy. They're already wired up, validated, and
return structured results.

Reach for **scripts under `.wayfinder_runs/`** when the work is complex or repetitive: stitching
multiple adapter calls together, fan-out across many wallets/chains, multi-step flows with
conditional branches, or anything you'll want to re-run. Scripts can be scheduled via
`runner(action="add_job", type="script", ...)` once they're stable.

Rough cut: if you can express it as one MCP call, use the MCP call. If you find yourself
chaining three or more, write a script.

**Before writing any script, load `/writing-wayfinder-scripts`**

### Key domain knowledge

Hyperliquid minimums:

- **Minimum deposit: $5 USD** (deposits below this are **lost**)
- **Minimum order: $10 USD notional** (applies to both perp and spot)

Hyperliquid surfaces in the adapter/MCP: perp, spot, HIP-3 builder-deployed perp dexes (`xyz`/`flx`/`vntl`/`hyna`/`km`...), and HIP-4 outcome markets (binary/multi-outcome prediction contracts). Outcomes use a separate asset-id space (`100_000_000 + 10*outcome_id + side`) and integer contract sizes; **settle in USDH** (token 360), not USDC; settle daily at 06:00 UTC; written via `hyperliquid_execute(action="place_outcome_order", ...)`. See `/using-hyperliquid-adapter` rules for details.

**Outcome / prediction markets — search both venues, let the user pick.** When a user mentions "outcome market" or "prediction market" without naming the platform, **search both venues in parallel** and present candidates side-by-side so the user can choose. Two venues:

- **Hyperliquid HIP-4** — daily binary price contracts settled in USDH on the HL L1; rotating daily lineup. Search via `mcp__wayfinder__hyperliquid_search_market(query=...)` (read the `outcomes` bucket).
- **Polymarket** — long-form prediction markets (politics, sports, events, crypto milestones), settled in USDC.e on Polygon. Search via `mcp__wayfinder__polymarket_read(action="search", query=..., limit=...)`.

Present results as a table grouped by venue, then ask which market to trade — the same theme can list on both venues with different sizes, expiries, and collateral. Load `/using-hyperliquid-adapter` or `/using-polymarket-adapter` once the user picks.

Supported chains:

| Chain     | ID    | Code        | Symbol | Native token ID                   |
| --------- | ----- | ----------- | ------ | --------------------------------- |
| Ethereum  | 1     | `ethereum`  | ETH    | `ethereum-ethereum`               |
| Base      | 8453  | `base`      | ETH    | `ethereum-base`                   |
| Arbitrum  | 42161 | `arbitrum`  | ETH    | `ethereum-arbitrum`               |
| Polygon   | 137   | `polygon`   | POL    | `polygon-ecosystem-token-polygon` |
| BSC       | 56    | `bsc`       | BNB    | `binancecoin-bsc`                 |
| Avalanche | 43114 | `avalanche` | AVAX   | `avalanche-avalanche`             |
| Plasma    | 9745  | `plasma`    | PLASMA | `plasma-plasma`                   |
| HyperEVM  | 999   | `hyperevm`  | HYPE   | `hyperliquid-hyperevm`            |

- **Plasma**: EVM chain where Pendle deploys PT/YT markets.
- **HyperEVM**: Hyperliquid's EVM layer. On-chain tokens (HYPE, USDC) live here; perp/spot trading uses the Hyperliquid L1 (off-chain, not EVM).

Gas requirements (critical — assets get stuck without gas):

- **Before any on-chain operation**, check the wallet has native gas on that chain.
- If bridging to a new chain for the first time: bridge gas first.

Token identifiers (important for quoting/execution/lookups):

- Use **token IDs** (`<coingecko_id>-<chain_code>`) or **address IDs** (`<chain_code>_<address>`).

## Recurring automation (Runner)

**All scheduled/recurring tasks MUST go through the runner daemon.** Do not use cron, systemd timers, or background loops. The daemon handles job persistence, failure tracking, timeouts, and session notifications.

```
runner(action="ensure_started")                       # idempotent — safe to call multiple times
runner(action="add_job",                              # schedule a strategy
       name="basis-update",
       type="strategy",
       strategy="basis_trading_strategy",
       strategy_action="update",
       interval_seconds=600,
       config="./config.json")
runner(action="add_job",                              # schedule a script
       name="check-balances",
       type="script",
       script_path=".wayfinder_runs/check_balances.py",
       interval_seconds=300)
runner(action="status")                               # show daemon + all jobs
runner(action="run_once", name="<name>")              # trigger immediate run
runner(action="pause_job", name="<name>")
runner(action="resume_job", name="<name>")
runner(action="delete_job", name="<name>")
runner(action="daemon_stop")                          # shut down daemon
```

`pause_job`, `resume_job`, `delete_job`, `update_job`, `run_once`, and
`daemon_stop` change runtime state. Only use them when the user explicitly asks
for that exact change or approves your proposed change. Never pause, delete,
stop, or reschedule a job just because its `job_result` notifications are noisy,
unchanged, or below a trigger threshold; offer the change and wait for the user.
On OpenCode/Shells, these actions go through the approval-gated
`core_runner_mutation` MCP tool.

See `RUNNER_ARCHITECTURE.md`.

## Path creation

- When creating a new Wayfinder path, include a browser applet by default or explicitly ask the owner before omitting one.
- The manage page uses applet presence as a verification requirement, so publishing without an applet can block approval until the owner publishes a replacement version.
- `poetry run wayfinder path init <slug>` scaffolds an applet by default; use `--no-applet` only when the owner intentionally wants no presentation UI.

## Path updates

- `poetry run wayfinder path update <slug>` is the single-path update command for installed paths.
- Default target selection is the API's `active_bonded_version`, not `latest_version` and not a pending version still in probation.
- `--version <x.y.z>` lets the user choose a specific public version explicitly.
- The CLI checks `.wayfinder/paths.lock.json` for the installed version, pulls the target version when newer, and then tries to re-use stored activation metadata.
- If activation metadata is missing, it tries one safe workspace default; if it still cannot determine an activation target, it completes the pull and prints the manual `path activate` command instead of failing.

## Messaging the user (Shells instances only)

If you detected a Wayfinder Shells instance in "First-Time Setup", you may email the owner to report completed work, surface decisions that need them, or flag anything you can't resolve. Backend only delivers when `email_verified` is true on the user, and throttles to **4 emails / user / day** — budget your sends accordingly.

See `/using-shells-notify` for the MCP tool, Python client, limits, and Markdown formatting tips.

## Frontend Context (Shells instances only)

If you detected a Wayfinder Shells instance, you can read what the user is currently viewing and update chart state in real time, including switching the default market/trading context, creating visual panes, and adding annotations or overlays.

See `/using-shells-chart-annotations` for the MCP tools, Python client, annotation types, chart workspace tools, and gotchas.

## Scheduled Jobs (Shells instances only)

On Wayfinder Shells instances (`OPENCODE_INSTANCE_ID` set), the runner daemon automatically syncs job and run state to vault-backend. This happens transparently — no agent action needed.

- **Job sync**: When a job is added, updated, paused, resumed, or deleted, the daemon pushes the current state to `PUT /instances/{id}/jobs/{name}/`
- **Run sync**: After each run completes, the daemon pushes the full log output to `POST /instances/{id}/jobs/{name}/runs/`
- **Local-only**: On non-Shells instances (no `OPENCODE_INSTANCE_ID`), sync is skipped silently

The frontend shows synced jobs and runs in the "Scheduled" tab of the shells sidebar.

**Don't silence `job_result` notifications.** When a scheduled job posts a `job_result` into the conversation, treat it as an event you must respond to — read the result, decide whether action is needed, and reply (act, escalate via `notify`, or acknowledge). Never skip past it silently or fold it into an unrelated turn.

## Migration notes

- Hyperliquid UnifiedAccount is the preferred account mode. If the user is on a legacy split spot/perp account, migrating may require closing all open positions, moving balances to spot, then enabling UnifiedAccountMode. `ensure_unified_account` runs before every order placement, but the flip can fail mid-state if open positions or stuck spot balances prevent the switch — you may need to edit code and write a custom script to unblock the user.

## User Suggestions (always emit)

At the END of every response, emit a `<userSuggestions>...</userSuggestions>` block with exactly 5 short follow-ups the user might click instead of typing.

- Pipe-delimited inside the tags: `<userSuggestions>opt1|opt2|opt3</userSuggestions>`
- Phrased first-person from the user's perspective ("Open a long on ETH", not "Want to long ETH?")
- Actionable and not open open-ended
- Keep each option short (under ~8 words).
- Always emit the block — even after errors, clarifications, or tool failures.
- No:
  - NO WALLET ADDRESSES
  - Asset ids (prefer human readable text)
  - Markdown
  - Asset/protocol names that haven't appeared in the conversation.
