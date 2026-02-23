# Contract interactions

Use MCP tools to read from (eth_call) and write to (broadcast a tx) any deployed EVM contract without using `run_script`.

## Fetch ABI: `contract_get_abi`

If you need the ABI (e.g. for a verified contract), fetch it directly:

```
mcp__wayfinder__contract_get_abi(
  chain_id=8453,
  contract_address="0xa75cEf1de8D2FC22847837B525830824C2364453",
  resolve_proxy=True
)
```

## Read-only: `contract_call`

Example: read a staking contract’s `getStaked(user)`:

```
mcp__wayfinder__contract_call(
  chain_id=8453,
  contract_address="0xa75cEf1de8D2FC22847837B525830824C2364453",
  abi=[
    {
      "type": "function",
      "name": "getStaked",
      "stateMutability": "view",
      "inputs": [{"name": "user", "type": "address"}],
      "outputs": [{"name": "", "type": "uint256"}]
    }
  ],
  function_name="getStaked",
  args=["0xYourAddressHere"]
)
```

Notes:
- Pass a **minimal ABI** (only the function(s) you need).
- If you omit `abi` and `abi_path`, the tool checks the **local artifact store** first (`.wayfinder_runs/contracts/{chain_id}/{address}/abi.json`) for contracts you deployed, then falls back to **Etherscan V2**
  (requires `system.etherscan_api_key` in `config.json` or `ETHERSCAN_API_KEY`, and the contract must be verified).
- If the target address is a common proxy type (EIP-1967 / ZeppelinOS / EIP-897), the tool will attempt to fetch the
  **implementation** ABI automatically.
- If a function is overloaded, pass `function_signature` like `deposit(uint256)` instead of `function_name`.

## Write: `contract_execute`

Example: approve USDC, then deposit into a staking contract:

```
# 0.10 USDC (6 decimals)
amount_raw = 100_000

# Approve
mcp__wayfinder__contract_execute(
  wallet_label="main",
  chain_id=8453,
  contract_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  abi=[
    {
      "type": "function",
      "name": "approve",
      "stateMutability": "nonpayable",
      "inputs": [
        {"name": "spender", "type": "address"},
        {"name": "value", "type": "uint256"}
      ],
      "outputs": [{"name": "", "type": "bool"}]
    }
  ],
  function_name="approve",
  args=["0xa75cEf1de8D2FC22847837B525830824C2364453", amount_raw]
)

# Deposit
mcp__wayfinder__contract_execute(
  wallet_label="main",
  chain_id=8453,
  contract_address="0xa75cEf1de8D2FC22847837B525830824C2364453",
  abi=[
    {
      "type": "function",
      "name": "deposit",
      "stateMutability": "nonpayable",
      "inputs": [{"name": "amount", "type": "uint256"}],
      "outputs": []
    }
  ],
  function_name="deposit",
  args=[amount_raw]
)
```

Notes:
- `contract_execute` is for **state-changing** writes. It rejects `view`/`pure` functions (use `contract_call` instead).
- Use `value_wei` for payable functions (defaults to `0`).
