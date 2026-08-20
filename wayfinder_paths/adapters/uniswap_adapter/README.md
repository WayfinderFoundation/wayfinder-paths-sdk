# Uniswap Adapter

The Uniswap adapter currently supports Uniswap V3 concentrated-liquidity position
management through the V3 `NonfungiblePositionManager` and V3 factory contracts.

- **Type**: `UNISWAP`
- **Module**: `wayfinder_paths.adapters.uniswap_adapter.adapter.UniswapAdapter`
- **Scope**: V3 LP NFT positions only
- **Not supported**: V2 swaps/liquidity, V3 swaps, Universal Router command
  execution, Permit2 signature approvals/transfers, Uniswap v4 pools or positions

## Supported Chains

The adapter uses `UNISWAP_V3_NPM` and `UNISWAP_V3_FACTORY` from
`wayfinder_paths.core.constants.contracts`:

| Chain ID | Network |
| --- | --- |
| 1 | Ethereum |
| 42161 | Arbitrum One |
| 137 | Polygon |
| 8453 | Base |
| 56 | BNB Smart Chain |
| 43114 | Avalanche C-Chain |

The official Uniswap v3 docs warn that deployment addresses are chain-specific.
Do not assume a new chain can reuse an address from another chain.

## Usage

```python
from eth_account import Account
from wayfinder_paths.adapters.uniswap_adapter import UniswapAdapter

acct = Account.create()

async def sign_cb(tx: dict) -> bytes:
    return acct.sign_transaction(tx).raw_transaction

adapter = UniswapAdapter(
    config={"chain_id": 8453},
    sign_callback=sign_cb,
    wallet_address=acct.address,
)
```

## Read Methods

| Method | Purpose |
| --- | --- |
| `get_pool(token_a, token_b, fee)` | Read a V3 pool address from the V3 factory |
| `get_position(token_id)` | Read one V3 LP NFT position |
| `get_positions(owner=None)` | List V3 LP NFT positions for an owner |
| `get_uncollected_fees(token_id)` | Simulate V3 fee collection amounts |
| `get_full_user_state(account=...)` | Return a V3 position snapshot for an account |

## Fund-Moving Methods

| Method | On-chain action |
| --- | --- |
| `add_liquidity(...)` | ERC20 approvals when needed, then V3 NPM `mint` |
| `increase_liquidity(...)` | ERC20 approvals when needed, then V3 NPM `increaseLiquidity` |
| `remove_liquidity(...)` | V3 NPM `multicall` with `decreaseLiquidity`, optional `collect`, optional `burn` |
| `collect_fees(token_id)` | V3 NPM `collect` |

All write methods require a `sign_callback`. The adapter never silently approves
or signs transactions. ERC20 approvals use the shared `ensure_allowance` helper,
which includes the repo's reset-before-approve handling for tokens such as USDT.

## Current Gap Matrix

| Area | Current SDK adapter | Current Uniswap expectation | Decision |
| --- | --- | --- | --- |
| V2 swaps/liquidity | No V2 router or pair support | V2 is a legacy AMM surface, and swaps can be composed through Universal Router | Out of scope |
| V3 LP positions | Supported through V3 NPM and factory | V3 remains valid for existing integrations and V3-specific workflows | Keep and test |
| V3 swaps | No SwapRouter02 or Universal Router support | Uniswap docs identify Universal Router as the preferred ERC20/NFT swap entrypoint | Follow-up router design |
| Universal Router | No `execute` command encoder, sub-plans, balance checks, or cleanup commands | Command stream composes v2/v3/v4 swaps, Permit2, wrapping, position-manager calls | Follow-up adapter/helper |
| Permit2 | No Permit2 ABI, signatures, allowance expiration, or SignatureTransfer support | Permit2 is used by Universal Router and PositionManager workflows | Follow-up approval design |
| V4 swaps/LP | No PoolManager, StateView, Quoter, v4 PositionManager, `PoolKey`, hooks, native ETH, or action encoding | v4 is recommended for new integrations; deployments are per-chain | Follow-up v4 adapter |

The existing V3 adapter should not be extended by bolting v4 or Universal Router
behavior onto `UniswapV3BaseAdapter`. Those are separate protocol surfaces with
different address maps, calldata encodings, approval semantics, and safety checks.

## Gorlami Simulation

The fund-moving V3 mint path is covered by:

```bash
poetry run pytest -o addopts= wayfinder_paths/adapters/uniswap_adapter/test_gorlami_simulation.py -q
```

The test runs only when the Gorlami fork proxy is configured. It creates a Base
fork, funds a temporary wallet with gas, WETH, and USDC, then exercises the
adapter's ERC20 approval and V3 NPM mint flow against the Base WETH/USDC 0.05%
pool.

## Official Docs Checked

- Uniswap protocols overview:
  `https://developers.uniswap.org/docs/protocols/overview`
- Universal Router overview and commands:
  `https://developers.uniswap.org/docs/protocols/universal-router/overview`
  and `https://developers.uniswap.org/docs/protocols/universal-router/concepts/commands`
- Permit2 overview:
  `https://developers.uniswap.org/docs/protocols/permit2/overview`
- Uniswap v3 deployments:
  `https://developers.uniswap.org/docs/protocols/v3/deployments`
- Uniswap v4 deployments and liquidity guides:
  `https://developers.uniswap.org/docs/protocols/v4/deployments`
  and `https://developers.uniswap.org/docs/protocols/v4/guides/managing-liquidity/overview`
