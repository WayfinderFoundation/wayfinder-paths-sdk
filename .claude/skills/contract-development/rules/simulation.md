# Simulation (Gorlami Fork Deploys)

## Pattern: compile -> fork -> deploy -> interact -> verify -> deploy live

Before deploying to a live chain, test on a Gorlami fork:

1. **Fork the target chain** — create a Gorlami vnet forking the chain you'll deploy to
2. **Swap RPCs** — point the SDK at the fork RPC via `set_rpc_urls()`
3. **Deploy** — same `deploy_contract()` call, but it hits the fork
4. **Interact** — call contract functions to verify behavior
5. **Confirm** — if everything works, repeat on the live chain

## Script pattern

```python
import asyncio
from wayfinder_paths.core.config import set_rpc_urls
from wayfinder_paths.core.utils.contracts import deploy_contract
from wayfinder_paths.core.utils.web3 import web3_from_chain_id
from wayfinder_paths.mcp.utils import find_wallet_by_label
from wayfinder_paths.mcp.scripting import _make_sign_callback

SOURCE = '''
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;
contract Counter {
    uint256 public count;
    function increment() external { count += 1; }
}
'''

async def main():
    # 1. Set fork RPC (get this from gorlami_fork() or MCP)
    set_rpc_urls({"8453": ["https://virtual.base.rpc.tenderly.co/YOUR_FORK_ID"]})

    wallet = find_wallet_by_label("main")
    sign_callback = _make_sign_callback(wallet["private_key_hex"])

    # 2. Deploy on fork
    result = await deploy_contract(
        source_code=SOURCE,
        contract_name="Counter",
        from_address=wallet["address"],
        chain_id=8453,
        sign_callback=sign_callback,
        verify=False,  # no Etherscan verification on forks
    )
    print(f"Fork deploy: {result['contract_address']}")

    # 3. Interact
    async with web3_from_chain_id(8453) as w3:
        contract = w3.eth.contract(
            address=result["contract_address"],
            abi=result["abi"],
        )
        count = await contract.functions.count().call()
        print(f"Initial count: {count}")

asyncio.run(main())
```

## Key notes

- Always set `verify=False` on fork deploys (Etherscan can't verify fork contracts)
- The SDK's `send_transaction()` auto-detects Gorlami fork RPCs and adjusts gas/confirmations
- Use `/simulation-dry-run` skill for full Gorlami fork setup patterns
- Fork deploys are free (no real gas spent)
