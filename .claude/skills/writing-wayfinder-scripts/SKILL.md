---
name: writing-wayfinder-scripts
description: How to write Python scripts under `.wayfinder_runs/` — `get_adapter()` patterns, `web3_from_chain_id()` usage, and the common gotchas (clients vs adapters return shapes, async/await, ERC20 helpers, wei vs human amounts, funding-rate sign).
metadata:
  tags: wayfinder, scripting, wayfinder_runs, adapters, clients, web3, gotchas
---

## When to load

Load this skill **before writing any script under `.wayfinder_runs/`**. Skip for one-shot MCP calls.

## Scripting helper for adapters

When writing scripts under `.wayfinder_runs/`, use `get_adapter()` to simplify setup:

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

# Single-wallet adapter (sign_callback + wallet_address)
adapter = await get_adapter(MoonwellAdapter, "main")
await adapter.set_collateral(mtoken=USDC_MTOKEN)

# Dual-wallet adapter (main + strategy, e.g. BalanceAdapter)
from wayfinder_paths.adapters.balance_adapter import BalanceAdapter
adapter = await get_adapter(BalanceAdapter, "main", "my_strategy")

# Read-only (no wallet needed)
adapter = await get_adapter(PendleAdapter)
```

`get_adapter()` auto-loads `config.json`, looks up wallets by label (local or remote), creates signing callbacks, and wires them into the adapter constructor. It introspects the adapter's `__init__` signature to determine the wiring:

- `sign_callback` + `wallet_address` → single-wallet adapter (most adapters)
- `sign_hash_callback` → also wired if the adapter accepts it (e.g. `PolymarketAdapter` for CLOB signing)
- `main_sign_callback` + `strategy_sign_callback` → dual-wallet adapter (`BalanceAdapter`); requires two wallet labels

**Before writing any adapter-using script**, also load the matching protocol skill (e.g. `/using-pendle-adapter`, `/using-hyperliquid-adapter`). Skills document method signatures, return shapes, and field names — guessing wastes iterations.

For direct Web3 usage in scripts, **do not hardcode RPC URLs**. Use `web3_from_chain_id(chain_id)` from `wayfinder_paths.core.utils.web3` — it's an **async context manager**:

```python
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.core.utils.tokens import get_token_balance

async with web3_from_chain_id(8453) as w3:
    balance = await w3.eth.get_balance(addr)
    token_balance = await get_token_balance(
        token_address=token, chain_id=8453, wallet_address=addr, web3=w3
    )
```

It uses the Wayfinder RPC proxy by default. `strategy.rpc_urls` is only for
explicit overrides such as local forks, Gorlami simulations, or debugging a
specific provider. In normal Shell usage, keep `strategy.rpc_urls` empty.
`get_web3s_from_chain_id` also returns **async** clients; it is not a synchronous
alternative. Prefer the context manager above, which closes providers for you.

On hosted Shells, write the file under `.wayfinder_runs/`, then use
`core_run_script(script_path=".wayfinder_runs/my_script.py")`. It selects the SDK
interpreter and repo working directory; do not use system Python or install missing
dependencies to repair an interpreter mismatch. For local development outside
Shells, `poetry run python .wayfinder_runs/my_script.py` is supported.

For a simple task, an import/argument error permits one targeted repair after
reading the installed signature/source. Chain constants live in
`wayfinder_paths.core.constants.chains`; use keyword arguments for token helpers.
If the repair fails, report the concrete blocker. Do not re-run a script containing
writes until any earlier submissions have been reconciled.

## Read-only recovery of an existing EVM transaction

Use this only when the tool did not establish the outcome; never submit again to
find out whether the first attempt worked. Preserve the original hash and chain.

```python
from wayfinder_paths.core.utils.transaction import wait_for_transaction_receipt

receipt = await wait_for_transaction_receipt(
    chain_id=chain_id, txn_hash=txn_hash, confirmations=0, timeout=15
)
```

`status=1` confirms source execution; `TransactionRevertedError` establishes a
source revert. A timeout/RPC error is **unknown**, not failure or proof that no funds
moved. Stop after this bounded check if unresolved.

For a bridge, source success is not destination delivery. Only when the original
route's `bridge_tracking` is available, check it without submitting a new quote/swap:

```python
import asyncio
from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT

bridge = await asyncio.wait_for(
    BRAP_CLIENT.wait_for_bridge_execution(
        bridge_tracking=bridge_tracking, tx_hash=txn_hash,
        poll_interval_seconds=3, timeout_seconds=15,
    ),
    timeout=20,
)
```

Require `bridge["is_success"]` before continuing dependent steps. Missing tracking,
pending delivery, or a failed check means destination completion is unresolved;
report that and the source hash instead of polling indefinitely or bridging again.

## Wallet helpers in scripts

Don't grep `config.json` for `wallets[]` or read wallet files directly — on Wayfinder Shells the remote wallets aren't in `config.json` and you'll miss them. Use the helpers:

```python
from wayfinder_paths.core.utils.wallets import load_wallets, find_wallet_by_label

# Every wallet (local + remote, deduped)
wallets = await load_wallets()

# Single wallet by label
wallet = await find_wallet_by_label("main")
if wallet is None:
    raise RuntimeError("wallet 'main' not found")
```

Same code path as the `core_get_wallets` MCP tool, so remote wallets work transparently. `get_adapter("main")` already calls these for you — only reach for them directly when you need raw wallet metadata (e.g. address, chain) outside an adapter context.

## Gotchas — read before writing

### 0. Client vs Adapter return patterns — CRITICAL DIFFERENCE

**Clients return data directly; Adapters return `(ok, data)` tuples.** This is the #1 source of script errors.

```python
# CLIENTS (return data directly, raise exceptions on errors)
from wayfinder_paths.core.clients.DeltaLabClient import DELTA_LAB_CLIENT
from wayfinder_paths.core.clients.PoolClient import POOL_CLIENT
from wayfinder_paths.core.clients.TokenClient import TOKEN_CLIENT

# WRONG — clients don't return tuples
ok, data = await DELTA_LAB_CLIENT.get_basis_apy_sources(...)  # ❌ ValueError: too many values to unpack

# RIGHT — clients return data directly
data = await DELTA_LAB_CLIENT.get_basis_apy_sources(...)  # ✅ dict
pools = await POOL_CLIENT.get_pools(...)  # ✅ LlamaMatchesResponse
token = await TOKEN_CLIENT.get_token_details(...)  # ✅ TokenDetails

# ADAPTERS (always return tuple[bool, data])
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter

adapter = await get_adapter(HyperliquidAdapter)

# WRONG — adapters always return tuples
data = await adapter.get_meta_and_asset_ctxs()  # ❌ data is actually (True, {...})

# RIGHT — destructure the tuple and check ok
ok, data = await adapter.get_meta_and_asset_ctxs()  # ✅
if not ok:
    raise RuntimeError(f"Adapter call failed: {data}")
meta, ctxs = data[0], data[1]
```

**Rule of thumb:** `wayfinder_paths.core.clients` → data directly. `wayfinder_paths.adapters` → `(ok, data)` tuple.

### 1. `get_adapter()` already loads config — don't call `load_config()` first.

### 2. `load_config()` returns `None` — it mutates a global

```python
# WRONG — config will be None
config = load_config("config.json")
api_key = config["system"]["api_key"]  # TypeError!

# RIGHT — use the CONFIG global, or use load_config_json() for a dict
from wayfinder_paths.core.config import load_config, CONFIG
load_config("config.json")
api_key = CONFIG["system"]["api_key"]

# OR — if you need a plain dict:
from wayfinder_paths.core.config import load_config_json
config = load_config_json("config.json")
```

### 3. Web3 is async

Use `async with` and `await` as shown above, including
`await contract.functions.balanceOf(addr).call()`; an unawaited call is not a result.

### 4. Use existing ERC20 helpers — don't inline ABIs

```python
# WRONG — verbose, error-prone
abi = [{"inputs": [{"name": "account", ...}], ...}]
contract = w3.eth.contract(address=token, abi=abi)
balance = await contract.functions.balanceOf(addr).call()

# RIGHT — one-liner
from wayfinder_paths.core.utils.tokens import get_token_balance
balance = await get_token_balance(token_address, chain_id=8453, wallet_address=addr)

# OR if you need the contract object:
from wayfinder_paths.core.constants.erc20_abi import ERC20_ABI
contract = w3.eth.contract(address=token, abi=ERC20_ABI)
```

### 5. Python `quote_swap` amounts are wei strings, not human-readable

Note: This applies to the Python `quote_swap()` function in scripts. The MCP `onchain_swap(...)` / `onchain_send(...)` tools take **decimal human-readable** amount strings (e.g. `"5.0"` for 5 USDC). MCP amount strings must include a decimal point; `"5"` is rejected.

```python
# WRONG — "10.0" is not a valid wei amount
quote = await quote_swap(from_token="usd-coin-base", to_token="ethereum-base", amount="10.0", ...)

# RIGHT — convert to wei first
from wayfinder_paths.core.utils.units import to_erc20_raw
amount_wei = str(to_erc20_raw(10.0, decimals=6))  # USDC has 6 decimals
quote = await quote_swap(from_token="usd-coin-base", to_token="ethereum-base", amount=amount_wei, ...)
```

### 6. Cross-chain simulation IS possible

Fork both chains, seed expected tokens on the destination fork, then continue. Load `/simulation-dry-run` for the full pattern.

### 7. Write the script file before calling `core_run_script`

`mcp__wayfinder__core_run_script` executes a file at the given path — the file must exist first. Always `Write` the script, then call `core_run_script`.

### 8. Funding rate sign (CRITICAL for perp trading)

**Negative funding means shorts PAY longs** (not the other way around).

```python
# WRONG interpretation
funding_rate = -0.08  # -8% annually
print("Negative = good for shorts!")  # ❌ BACKWARDS!

# RIGHT interpretation
funding_rate = -0.08  # -8% annually
if funding_rate > 0:
    # Positive funding: Longs pay shorts (good for shorts)
    print("Shorts receive funding")  # ✅
else:
    # Negative funding: Shorts pay longs (bad for shorts)
    print("Shorts PAY funding")  # ✅
```

This applies to:

- Hyperliquid perp funding rates
- Delta Lab perp opportunities
- Any perp trading strategy analysis

When evaluating perp positions, always verify the sign interpretation — it's backwards from intuition for many traders.
