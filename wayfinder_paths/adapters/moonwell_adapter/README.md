# Moonwell Adapter

Adapter for Moonwell Core lending markets. Moonwell Core uses Compound-v2-style
mToken markets; Moonwell Morpho Vaults and isolated markets are Morpho-based and
should be handled with `MorphoAdapter` rather than duplicated here.

- **Type**: `MOONWELL`
- **Module**: `wayfinder_paths.adapters.moonwell_adapter.adapter.MoonwellAdapter`
- **Default chain**: Base (`8453`)

## Supported Networks

| Network | chain_id | Core writes | Market reads | Rewards read/claim | Notes |
|---------|----------|-------------|--------------|--------------------|-------|
| Base | `8453` | Yes | Yes | Yes | Primary current deployment |
| OP Mainnet | `10` | Yes | Yes | Yes | Uses OP Moonwell Comptroller and Multi-Reward Distributor |
| Moonbeam | `1284` | Yes | Yes | No distributor configured | Includes native GLMR and deprecated/bad-debt market flags |
| Moonriver | `1285` | Yes | Yes | No distributor configured | Official SDK marks all Core markets deprecated |

Use `chain_id=` on any method to override the adapter default:

```python
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter
from wayfinder_paths.core.constants.moonwell_contracts import CHAIN_ID_OPTIMISM

adapter = MoonwellAdapter(config={"chain_id": CHAIN_ID_OPTIMISM})
ok, markets = await adapter.get_all_markets()

ok, markets = await adapter.get_all_markets(chain_id=8453)
```

## Core Market Addresses

The adapter stores audited network, contract, and market metadata in
`wayfinder_paths.core.constants.moonwell_contracts`.

Common Base markets:

| Asset | mToken | Underlying |
|-------|--------|------------|
| USDC | `0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22` | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| WETH | `0x628ff693426583D9a7FB391E54366292F509D457` | `0x4200000000000000000000000000000000000006` |
| wstETH | `0x627Fe393Bc6EdDA28e99AE648fD6fF362514304b` | `0xc1CBa3fCea344f92D9239c08C0568f6F2F0ee452` |

Base protocol contracts:

- **Comptroller**: `0xfBb21d0380beE3312B33c4353c8936a0F13EF26C`
- **Views**: `0x6834770ABA6c2028f448E3259DDEE4BCB879d459`
- **Multi-Reward Distributor**: `0xe9005b078701e2A0948D2EaC43010D35870Ad9d2`
- **WELL token**: `0xA88594D404727625A9437C3f886C7643872296AE`

## Read Methods

```python
ok, state = await adapter.get_full_user_state(
    account="0x...",          # optional if adapter has wallet_address
    chain_id=8453,            # optional, defaults to adapter.chain_id
    include_rewards=True,
    include_apy=True,
    include_usd=False,
)

ok, markets = await adapter.get_all_markets(
    chain_id=8453,
    include_apy=True,
    include_rewards=True,
    include_usd=False,
)

ok, pos = await adapter.get_pos(mtoken="0x...", chain_id=8453)
ok, cf = await adapter.get_collateral_factor(mtoken="0x...", chain_id=8453)
ok, apy = await adapter.get_apy(mtoken="0x...", apy_type="supply", chain_id=8453)
ok, liquidity = await adapter.get_borrowable_amount(account="0x...", chain_id=8453)
```

`get_all_markets()` returns Core market state from Moonwell Views plus local metadata:
`chainId`, `chainName`, `underlyingSymbol`, `deprecated`, `badDebt`, and
`nativeUnderlying` where known.

## Write Methods

All amounts are raw integer token units. Fund-moving methods require a configured
`wallet_address` and `sign_callback`.

```python
ok, tx = await adapter.lend(
    chain_id=8453,
    mtoken="0xEdc817A28E8B93B03976FBd4a3dDBc9f7D176c22",
    underlying_token="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    amount=100 * 10**6,
)

ok, tx = await adapter.set_collateral(chain_id=8453, mtoken="0x...")
ok, tx = await adapter.borrow(chain_id=8453, mtoken="0x...", amount=10**16)

ok, tx = await adapter.repay(
    chain_id=8453,
    mtoken="0x...",
    underlying_token="0x...",
    amount=10**16,
    repay_full=False,
)

ok, info = await adapter.max_withdrawable_mtoken(chain_id=8453, mtoken="0x...")
ok, tx = await adapter.unlend(chain_id=8453, mtoken="0x...", amount=info["cTokens_raw"])
```

Approval behavior:

- `lend()` approves the mToken to pull the supplied underlying token.
- `repay()` approves the mToken to pull the borrowed underlying token.
- `repay_full=True` sends `MAX_UINT256` to `repayBorrow` after allowance setup.
- `set_collateral()` and `remove_collateral()` call the chain-specific Comptroller.
- `wrap_eth()` wraps the network's configured wrapped native token (`WETH`, `WGLMR`, or `WMOVR`).

## Rewards

```python
ok, rewards = await adapter.claim_rewards(chain_id=8453, min_rewards_usd=1.0)
```

Rewards are read through Moonwell's Multi-Reward Distributor where configured.
Base and OP Mainnet currently have distributor addresses. Moonbeam and Moonriver do
not, so `claim_rewards()` returns `(True, {})` there instead of sending a claim
transaction.

## Vaults And Isolated Markets

Moonwell's current vault and isolated-market products are Morpho-based. Use
`MorphoAdapter` for ERC-4626 vault deposits/withdrawals, Morpho market lending,
collateral, borrowing, and Morpho reward flows. Moonwell Morpho contract addresses
are present in `MOONWELL_BY_CHAIN` for Base and OP Mainnet as metadata/handoff
information only.

## Gotchas

- Adapter write methods take **mToken addresses**, not underlying token addresses.
- `unlend()` calls `redeem()` and expects an **mToken amount**, not an underlying amount.
- Supplying does not enable collateral; call `set_collateral()` explicitly.
- Check `get_borrowable_amount()` before borrowing.
- Moonriver markets are deprecated in the official Moonwell SDK source.
- Moonbeam includes markets marked `badDebt`; inspect market metadata before using them.

## Testing

```bash
poetry run pytest -o addopts= wayfinder_paths/adapters/moonwell_adapter -q
```

`test_gorlami_simulation.py` covers Base supply, collateral, borrow, repay,
withdraw, and claim paths on a Gorlami fork when an API key is configured.
