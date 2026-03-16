# Scripting Gotchas (`.wayfinder_runs/` scripts)

Common mistakes when writing run scripts. **Read before writing any script.**

**0. Client vs Adapter return patterns — CRITICAL DIFFERENCE**

**Clients return data directly; Adapters return `(ok, data)` tuples.** This is the #1 source of script errors.

```python
# CLIENTS (return data directly, raise exceptions on errors)
from wayfinder_paths.core.clients import DELTA_LAB_CLIENT, POOL_CLIENT, TOKEN_CLIENT

# WRONG — clients don't return tuples
ok, data = await DELTA_LAB_CLIENT.get_basis_apy_sources(...)  # ❌ ValueError: too many values to unpack

# RIGHT — clients return data directly
data = await DELTA_LAB_CLIENT.get_basis_apy_sources(...)  # ✅ dict
pools = await POOL_CLIENT.get_pools(...)  # ✅ LlamaMatchesResponse
token = await TOKEN_CLIENT.get_token_details(...)  # ✅ TokenDetails

# ADAPTERS (always return tuple[bool, data])
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.hyperliquid_adapter import HyperliquidAdapter

adapter = get_adapter(HyperliquidAdapter)

# WRONG — adapters always return tuples
data = await adapter.get_meta_and_asset_ctxs()  # ❌ data is actually (True, {...})

# RIGHT — destructure the tuple and check ok
ok, data = await adapter.get_meta_and_asset_ctxs()  # ✅
if not ok:
    raise RuntimeError(f"Adapter call failed: {data}")
meta, ctxs = data[0], data[1]
```

**Why the difference?**

- **Clients** are thin HTTP wrappers that let `httpx` exceptions bubble up
- **Adapters** handle multiple failure modes (RPC errors, contract reverts, parsing failures) and return tuples to avoid raising exceptions for expected failures

**Rule of thumb:** If it's in `wayfinder_paths.core.clients`, it returns data directly. If it's in `wayfinder_paths.adapters`, it returns a tuple.

**1. `get_adapter()` already loads config — don't call `load_config()` first**

```python
# WRONG — redundant, and load_config() returns None anyway
config = load_config("config.json")
adapter = MoonwellAdapter(config=config, ...)

# RIGHT — get_adapter() handles config + wallet + signing internally
from wayfinder_paths.mcp.scripting import get_adapter
adapter = get_adapter(MoonwellAdapter, "main")

# Dual-wallet adapters (e.g. BalanceAdapter) take two wallet labels:
adapter = get_adapter(BalanceAdapter, "main", "my_strategy")

# For read-only adapters, omit the wallet label:
adapter = get_adapter(HyperliquidAdapter)
```

**2. `load_config()` returns `None` — it mutates a global**

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

**3. `web3_from_chain_id()` is an async context manager, not a function call**

```python
# WRONG — returns an async generator object, not a Web3 instance
w3 = web3_from_chain_id(8453)

# RIGHT
async with web3_from_chain_id(8453) as w3:
    ...
```

**4. All Web3 calls are async — always `await`**

```python
# WRONG — returns a coroutine, not the result
balance = w3.eth.get_balance(addr)
result = contract.functions.balanceOf(addr).call()

# RIGHT
balance = await w3.eth.get_balance(addr)
result = await contract.functions.balanceOf(addr).call()
```

**5. Use existing ERC20 helpers — don't inline ABIs**

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

**6. Python `quote_swap` amounts are wei strings, not human-readable**

Note: This applies to the Python `quote_swap()` function in scripts. The MCP `execute(...)` tool takes **human-readable** amounts (e.g. `"5"` for 5 USDC).

```python
# WRONG — "10.0" is not a valid wei amount
quote = await quote_swap(from_token="usd-coin-base", to_token="ethereum-base", amount="10.0", ...)

# RIGHT — convert to wei first
from wayfinder_paths.core.utils.units import to_erc20_raw
amount_wei = str(to_erc20_raw(10.0, decimals=6))  # USDC has 6 decimals
quote = await quote_swap(from_token="usd-coin-base", to_token="ethereum-base", amount=amount_wei, ...)
```

**7. Cross-chain simulation IS possible** — fork both chains, seed expected tokens on the destination fork, then continue. Load `/simulation-dry-run` for the full pattern.

**8. Write the script file before calling `run_script`**

`mcp__wayfinder__run_script` executes a file at the given path — the file must exist first. Always `Write` the script, then call `run_script`.

**9. Funding rate sign (CRITICAL for perp trading)**

**CRITICAL: Negative funding means shorts PAY longs** (not the other way around).

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

When evaluating perp positions, always verify the sign interpretation - it's backwards from intuition for many traders.
