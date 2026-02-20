# Deployment

## MCP tool: `deploy_contract`

The quickest path — compile + deploy + verify in one call:

```
mcp__wayfinder__deploy_contract(
    wallet_label="main",
    source_path="$WAYFINDER_SCRATCH_DIR/MyToken.sol",
    contract_name="MyToken",
    chain_id=8453,
    constructor_args=["0xabc...", 1000000],
    verify=true
)
```

- **Gated by safety review hook** — shows contract name, chain, wallet before confirming
- `source_path` points at the Solidity file (avoid passing giant inline strings)
- `constructor_args` can be a JSON array (preferred) or a JSON-encoded array string; args are auto-cast to ABI types
- `verify=true` (default) submits to Etherscan V2 after deploy (API key only needed for verification; deploy works without it — set `verify=false` to skip)
- The SDK compiles and deploys your Solidity source **as-is** (no automatic source mutation).
- Deployments are tracked in wallet profiles under protocol `contracts` (query `wayfinder://wallets/{label}`)
- Returns: `{tx_hash, contract_address, abi, bytecode, verified, explorer_url}`

## Script-based deployment

For more control, write a script under `$WAYFINDER_SCRATCH_DIR`:

```python
import asyncio
from wayfinder_paths.core.utils.contracts import deploy_contract

SOURCE = '''
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";

contract MyToken is ERC20 {
    constructor(uint256 initialSupply) ERC20("MyToken", "MTK") {
        _mint(msg.sender, initialSupply);
    }
}
'''

async def main():
    from wayfinder_paths.mcp.utils import find_wallet_by_label
    from wayfinder_paths.mcp.scripting import _make_sign_callback

    wallet = find_wallet_by_label("main")
    sign_callback = _make_sign_callback(wallet["private_key_hex"])

    result = await deploy_contract(
        source_code=SOURCE,
        contract_name="MyToken",
        constructor_args=[1_000_000 * 10**18],
        from_address=wallet["address"],
        chain_id=8453,
        sign_callback=sign_callback,
        verify=True,
    )
    print(f"Deployed: {result['contract_address']}")
    print(f"Tx: {result['tx_hash']}")

asyncio.run(main())
```

## Constructor args casting

Constructor arguments are automatically cast to their ABI types:

| Solidity type | Python input | Cast result |
|---|---|---|
| `address` | `"0xabc..."` | Checksummed address |
| `uint256` | `1000` or `"1000"` | `int` |
| `bool` | `true` / `"true"` / `1` | `bool` |
| `string` | `"hello"` | `str` |
| `bytes32` | `"0xab..."` | `bytes` |
| `tuple(...)` | `[val1, val2]` or `{name: val}` | Cast recursively |
