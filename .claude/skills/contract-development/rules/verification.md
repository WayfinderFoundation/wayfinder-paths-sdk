# Etherscan Verification

## How it works

Verification uses the **Etherscan V2 unified API** with `solidity-standard-json-input` mode. This submits the exact same compiler input JSON that `solcx` used — no source flattening needed.

**API endpoint:** `https://api.etherscan.io/v2/api?chainid={id}`

This single endpoint covers all Etherscan-supported chains (Etherscan, Basescan, Arbiscan, etc.).

## API key setup

The Etherscan API key is optional unless you want verification. If you don’t care about explorer verification, set `verify=false` on deploy.

Set the Etherscan API key in one of these locations (checked in order):

1. `config.json` → `system.etherscan_api_key`
2. Environment variable `ETHERSCAN_API_KEY`

Get a free key at [etherscan.io/apis](https://etherscan.io/apis). The same key works across all chains via the V2 API.

## Automatic verification

When using `deploy_contract()` (MCP tool or Python), set `verify=True` (the default). Verification:

1. Compiles the source using `compile_solidity_standard_json()` and reuses the exact compiler input JSON
2. Encodes constructor args if present
3. Submits to Etherscan V2 with `codeformat=solidity-standard-json-input`
4. Polls with exponential backoff (up to 10 attempts, max ~30s between)
5. Reports success/failure in the result (non-fatal — deploy still succeeds if verification fails)

## Manual verification

```python
from wayfinder_paths.core.utils.contracts import verify_on_etherscan
from wayfinder_paths.core.utils.solidity import compile_solidity_standard_json

std_json = compile_solidity_standard_json(source_code)

verified = await verify_on_etherscan(
    chain_id=8453,
    contract_address="0x...",
    standard_json_input=std_json["input"],
    contract_name="MyToken",
    source_filename="Contract.sol",       # must match the key in sources
    constructor_args_encoded="000...abc",  # hex-encoded, no 0x prefix
    compiler_version="v0.8.26+commit.8a97fa7a",
)
```

## Retry behavior

Etherscan often needs time to index a newly deployed contract. The verification poller:

- Starts with 1s delay, doubles each attempt (capped at 30s)
- Continues while Etherscan reports "pending"
- Fails after 10 attempts (~5 minutes total)

If Etherscan returns `Unable to locate ContractCode ...`, it usually means the explorer hasn’t indexed the new contract yet; wait a few blocks and retry. The SDK also retries verification submission automatically on this error.
