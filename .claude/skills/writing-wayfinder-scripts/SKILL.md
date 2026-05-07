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

async with web3_from_chain_id(8453) as w3:
    balance = await w3.eth.get_balance(addr)
```

It reads RPCs from `strategy.rpc_urls` in your config (defaults to repo-root `config.json`, or override via `WAYFINDER_CONFIG_PATH`). For sync access, use `get_web3s_from_chain_id(chain_id)` instead.

Run scripts with poetry: `poetry run python .wayfinder_runs/my_script.py`

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

### 3. `web3_from_chain_id()` is an async context manager, not a function call

```python
# WRONG — returns an async generator object, not a Web3 instance
w3 = web3_from_chain_id(8453)

# RIGHT
async with web3_from_chain_id(8453) as w3:
    ...
```

### 4. All Web3 calls are async — always `await`

```python
# WRONG — returns a coroutine, not the result
balance = w3.eth.get_balance(addr)
result = contract.functions.balanceOf(addr).call()

# RIGHT
balance = await w3.eth.get_balance(addr)
result = await contract.functions.balanceOf(addr).call()
```

### 5. Use existing ERC20 helpers — don't inline ABIs

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

### 6. Python `quote_swap` amounts are wei strings, not human-readable

Note: This applies to the Python `quote_swap()` function in scripts. The MCP `core_execute(...)` tool takes **human-readable** amounts (e.g. `"5"` for 5 USDC).

```python
# WRONG — "10.0" is not a valid wei amount
quote = await quote_swap(from_token="usd-coin-base", to_token="ethereum-base", amount="10.0", ...)

# RIGHT — convert to wei first
from wayfinder_paths.core.utils.units import to_erc20_raw
amount_wei = str(to_erc20_raw(10.0, decimals=6))  # USDC has 6 decimals
quote = await quote_swap(from_token="usd-coin-base", to_token="ethereum-base", amount=amount_wei, ...)
```

### 7. Cross-chain simulation IS possible

Fork both chains, seed expected tokens on the destination fork, then continue. Load `/simulation-dry-run` for the full pattern.

### 8. Write the script file before calling `core_run_script`

`mcp__wayfinder__core_run_script` executes a file at the given path — the file must exist first. Always `Write` the script, then call `core_run_script`.

### 9. Funding rate sign (CRITICAL for perp trading)

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
