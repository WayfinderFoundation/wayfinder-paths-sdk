"""Uniswap v4 exact-in swaps.

Aggregators (LiFi/BRAP) have no v4 coverage on newer chains like Robinhood,
so v4-only tokens either don't quote or route through dust pools at a huge
markup — the INDEX/ETH 1% pool ($87.5k) quotes ~24% more direct than the
aggregator's dust fallback. This module discovers a token pair's v4 pools
from PoolManager Initialize events, ranks them by live liquidity (StateView),
quotes via the V4Quoter, and executes through the Universal Router.

Pool selection is by liquidity, never fee tier: v4 lets anyone open a pool at
any fee, so the 10/20/50% "pools" are traps with dust in them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

from wayfinder_paths.core.constants.contracts import (
    UNISWAP_PERMIT2,
    UNISWAP_V4_POOL_MANAGER,
    UNISWAP_V4_QUOTER,
    UNISWAP_V4_STATE_VIEW,
    UNISWAP_V4_UNIVERSAL_ROUTER,
)
from wayfinder_paths.core.constants.uniswap_v4_abi import (
    PERMIT2_ABI,
    UNIVERSAL_ROUTER_ABI,
)
from wayfinder_paths.core.utils.tokens import ensure_allowance, is_native_token
from wayfinder_paths.core.utils.transaction import send_transaction
from wayfinder_paths.core.utils.web3 import web3_from_chain_id

NATIVE_ADDRESS = "0x0000000000000000000000000000000000000000"

# PoolManager.Initialize(bytes32 indexed id, address indexed currency0,
#   address indexed currency1, uint24 fee, int24 tickSpacing, address hooks,
#   uint160 sqrtPriceX96, int24 tick)
_INITIALIZE_TOPIC = (
    "0x"
    + keccak(
        b"Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
    ).hex()
)

# StateView view selector.
_GET_LIQUIDITY_SELECTOR = "0x" + keccak(b"getLiquidity(bytes32)")[:4].hex()

# Standard hookless (fee, tickSpacing) tiers — the v3-inherited convention that
# mainstream pairs (ETH/USDC etc.) use on every chain. Enumerating these by
# poolId + StateView is scan-free, so pool discovery works on large chains
# (mainnet/Base/Arbitrum) where a from-genesis Initialize log scan is infeasible.
_STANDARD_TIERS: tuple[tuple[int, int], ...] = (
    (100, 1),
    (500, 10),
    (2500, 50),
    (3000, 60),
    (10000, 200),
)

# Chains small enough for a full-history Initialize log scan, which also finds
# hooked/custom-tier pools (e.g. Robinhood meme pools with bespoke hooks). Large
# chains rely on standard-tier enumeration for their mainstream (hookless) pools.
_FULL_LOG_SCAN_CHAINS: frozenset[int] = frozenset({4663})

# V4Quoter.quoteExactInputSingle((PoolKey,bool,uint128,bytes))
_QUOTE_EXACT_IN_SELECTOR = (
    "0x"
    + keccak(
        b"quoteExactInputSingle(((address,address,uint24,int24,address),bool,uint128,bytes))"
    )[:4].hex()
)

# Universal Router command byte + v4 Actions (see @uniswap/v4-periphery).
_UR_V4_SWAP = 0x10
_V4_SWAP_EXACT_IN_SINGLE = 0x06
_V4_SETTLE_ALL = 0x0C
_V4_TAKE_ALL = 0x0F

_UINT160_MAX = (1 << 160) - 1
_UINT48_MAX = (1 << 48) - 1


def v4_supported(chain_id: int) -> bool:
    return chain_id in UNISWAP_V4_POOL_MANAGER


@dataclass(frozen=True)
class PoolKey:
    currency0: str
    currency1: str
    fee: int
    tick_spacing: int
    hooks: str

    def as_tuple(self) -> tuple[str, str, int, int, str]:
        return (
            to_checksum_address(self.currency0),
            to_checksum_address(self.currency1),
            self.fee,
            self.tick_spacing,
            to_checksum_address(self.hooks),
        )

    @property
    def pool_id(self) -> str:
        encoded = abi_encode(
            ["address", "address", "uint24", "int24", "address"],
            list(self.as_tuple()),
        )
        return "0x" + keccak(encoded).hex()


@dataclass(frozen=True)
class V4Pool:
    key: PoolKey
    liquidity: int


def _normalize_currency(address: str | None) -> str:
    if address is None or is_native_token(address):
        return NATIVE_ADDRESS
    return to_checksum_address(address)


def _sorted_currencies(token_a: str, token_b: str) -> tuple[str, str]:
    a = _normalize_currency(token_a)
    b = _normalize_currency(token_b)
    return (a, b) if int(a, 16) <= int(b, 16) else (b, a)


async def _rpc_call(web3, to: str, data: str) -> str:
    return (await web3.eth.call({"to": to_checksum_address(to), "data": data})).hex()


async def _pool_liquidity(web3, state_view: str, pool_id: str) -> int:
    liquidity_hex = await _rpc_call(
        web3, state_view, _GET_LIQUIDITY_SELECTOR + pool_id[2:]
    )
    return int(liquidity_hex, 16) if liquidity_hex not in ("", "0x") else 0


async def find_pools(chain_id: int, token_a: str, token_b: str) -> list[V4Pool]:
    """All v4 pools for the pair, ranked by live liquidity (deepest first).

    Two discovery paths, merged and deduped:
    - Standard hookless tiers by poolId + StateView liquidity — scan-free, works
      on every chain, covers mainstream pairs (the only feasible path on large
      chains like mainnet/Base/Arbitrum).
    - A full PoolManager Initialize log scan — only on small chains, to also
      catch hooked/custom-tier pools (e.g. Robinhood meme pools).

    Never infers a pool from fee tier alone; every candidate's liquidity is read
    on-chain and zero-liquidity pools are dropped.
    """
    if chain_id not in UNISWAP_V4_POOL_MANAGER:
        return []
    currency0, currency1 = _sorted_currencies(token_a, token_b)
    state_view = UNISWAP_V4_STATE_VIEW[chain_id]

    pools: list[V4Pool] = []
    seen: set[str] = set()

    async with web3_from_chain_id(chain_id) as web3:
        # Path 1: standard hookless tiers (all chains).
        for fee, tick_spacing in _STANDARD_TIERS:
            key = PoolKey(currency0, currency1, fee, tick_spacing, NATIVE_ADDRESS)
            if key.pool_id in seen:
                continue
            seen.add(key.pool_id)
            liquidity = await _pool_liquidity(web3, state_view, key.pool_id)
            if liquidity > 0:
                pools.append(V4Pool(key=key, liquidity=liquidity))

        # Path 2: full Initialize scan for hooked/custom pools (small chains).
        if chain_id in _FULL_LOG_SCAN_CHAINS:
            logs = await web3.eth.get_logs(
                {
                    "address": to_checksum_address(UNISWAP_V4_POOL_MANAGER[chain_id]),
                    "fromBlock": 0,
                    "toBlock": "latest",
                    "topics": [
                        _INITIALIZE_TOPIC,
                        None,
                        "0x" + "0" * 24 + currency0[2:].lower(),
                        "0x" + "0" * 24 + currency1[2:].lower(),
                    ],
                }
            )
            for log in logs:
                fee, tick_spacing, hooks, _sqrt, _tick = abi_decode(
                    ["uint24", "int24", "address", "uint160", "int24"],
                    bytes(log["data"]),
                )
                key = PoolKey(currency0, currency1, int(fee), int(tick_spacing), hooks)
                if key.pool_id in seen:
                    continue
                seen.add(key.pool_id)
                liquidity = await _pool_liquidity(web3, state_view, key.pool_id)
                if liquidity > 0:
                    pools.append(V4Pool(key=key, liquidity=liquidity))

    pools.sort(key=lambda p: p.liquidity, reverse=True)
    return pools


async def best_pool(chain_id: int, token_a: str, token_b: str) -> V4Pool | None:
    pools = await find_pools(chain_id, token_a, token_b)
    return pools[0] if pools else None


async def quote_exact_in(
    chain_id: int, pool: PoolKey, token_in: str, amount_in: int
) -> int:
    """Exact-in output for a single-hop v4 swap via V4Quoter (eth_call, no state)."""
    if chain_id not in UNISWAP_V4_QUOTER:
        raise ValueError(f"No Uniswap v4 quoter configured for chain {chain_id}")
    zero_for_one = _normalize_currency(token_in) == pool.currency0
    encoded_args = abi_encode(
        ["((address,address,uint24,int24,address),bool,uint128,bytes)"],
        [(pool.as_tuple(), zero_for_one, int(amount_in), b"")],
    )
    async with web3_from_chain_id(chain_id) as web3:
        result = await _rpc_call(
            web3,
            UNISWAP_V4_QUOTER[chain_id],
            _QUOTE_EXACT_IN_SELECTOR + encoded_args.hex(),
        )
    amount_out, _gas = abi_decode(["uint256", "uint256"], bytes.fromhex(result))
    return int(amount_out)


def _encode_swap_inputs(
    pool: PoolKey,
    zero_for_one: bool,
    amount_in: int,
    min_amount_out: int,
    input_currency: str,
    output_currency: str,
) -> bytes:
    """v4 SWAP_EXACT_IN_SINGLE + SETTLE_ALL + TAKE_ALL, packed for V4_SWAP."""
    actions = bytes([_V4_SWAP_EXACT_IN_SINGLE, _V4_SETTLE_ALL, _V4_TAKE_ALL])

    swap_params = abi_encode(
        ["((address,address,uint24,int24,address),bool,uint128,uint128,bytes)"],
        [(pool.as_tuple(), zero_for_one, int(amount_in), int(min_amount_out), b"")],
    )
    settle_params = abi_encode(
        ["address", "uint256"], [to_checksum_address(input_currency), int(amount_in)]
    )
    take_params = abi_encode(
        ["address", "uint256"],
        [to_checksum_address(output_currency), int(min_amount_out)],
    )
    return abi_encode(
        ["bytes", "bytes[]"], [actions, [swap_params, settle_params, take_params]]
    )


class UniswapV4SwapMixin:
    """Adds v4 exact-in swaps to the Uniswap adapter.

    Expects the host to provide `self.chain_id`, `self.owner`, and
    `self.sign_callback` (the base adapter does).
    """

    chain_id: int
    owner: str
    sign_callback: Any

    async def v4_find_pools(self, token_a: str, token_b: str) -> tuple[bool, Any]:
        try:
            pools = await find_pools(self.chain_id, token_a, token_b)
            return True, [
                {
                    "pool_id": p.key.pool_id,
                    "currency0": p.key.currency0,
                    "currency1": p.key.currency1,
                    "fee": p.key.fee,
                    "tick_spacing": p.key.tick_spacing,
                    "hooks": p.key.hooks,
                    "liquidity": p.liquidity,
                }
                for p in pools
            ]
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def v4_quote(
        self, token_in: str, token_out: str, amount_in: int
    ) -> tuple[bool, Any]:
        try:
            pool = await best_pool(self.chain_id, token_in, token_out)
            if pool is None:
                return False, "No v4 pool with liquidity for this pair"
            amount_out = await quote_exact_in(
                self.chain_id, pool.key, token_in, int(amount_in)
            )
            return True, {
                "amount_out": amount_out,
                "pool_id": pool.key.pool_id,
                "fee": pool.key.fee,
                "liquidity": pool.liquidity,
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def v4_swap_exact_in(
        self,
        *,
        token_in: str,
        token_out: str,
        amount_in: int,
        slippage_bps: int = 50,
        deadline_seconds: int = 600,
    ) -> tuple[bool, Any]:
        """Execute an exact-in v4 swap through the Universal Router.

        Native input rides as msg.value; ERC-20 input is approved to Permit2
        and Permit2-approved to the router (the v4 settle path pulls via
        Permit2). Output min is the quote minus `slippage_bps`.
        """
        try:
            chain_id = self.chain_id
            if chain_id not in UNISWAP_V4_UNIVERSAL_ROUTER:
                return False, f"Uniswap v4 not configured for chain {chain_id}"

            pool = await best_pool(chain_id, token_in, token_out)
            if pool is None:
                return False, "No v4 pool with liquidity for this pair"

            amount_out = await quote_exact_in(
                chain_id, pool.key, token_in, int(amount_in)
            )
            if amount_out <= 0:
                return False, "Quoter returned zero output"
            min_out = amount_out * (10_000 - int(slippage_bps)) // 10_000

            input_currency = _normalize_currency(token_in)
            output_currency = _normalize_currency(token_out)
            zero_for_one = input_currency == pool.key.currency0
            router = UNISWAP_V4_UNIVERSAL_ROUTER[chain_id]
            native_in = input_currency == NATIVE_ADDRESS

            if not native_in:
                # v4 settles ERC-20 inputs through Permit2: approve token→Permit2
                # (unlimited, idempotent) then Permit2→router for this amount.
                await ensure_allowance(
                    token_address=input_currency,
                    owner=self.owner,
                    spender=UNISWAP_PERMIT2,
                    amount=int(amount_in),
                    chain_id=chain_id,
                    signing_callback=self.sign_callback,
                )
                await self._permit2_approve(input_currency, router)

            swap_input = _encode_swap_inputs(
                pool.key,
                zero_for_one,
                int(amount_in),
                min_out,
                input_currency,
                output_currency,
            )
            deadline = await self._chain_deadline(chain_id, deadline_seconds)

            async with web3_from_chain_id(chain_id) as web3:
                contract = web3.eth.contract(
                    address=to_checksum_address(router), abi=UNIVERSAL_ROUTER_ABI
                )
                data = contract.encode_abi(
                    "execute", [bytes([_UR_V4_SWAP]), [swap_input], deadline]
                )
                nonce = await web3.eth.get_transaction_count(
                    to_checksum_address(self.owner)
                )
            tx = {
                "chainId": chain_id,
                "from": to_checksum_address(self.owner),
                "to": to_checksum_address(router),
                "data": data,
                "value": int(amount_in) if native_in else 0,
                "nonce": nonce,
            }
            tx_hash = await send_transaction(tx, self.sign_callback)
            return True, {
                "tx_hash": tx_hash,
                "pool_id": pool.key.pool_id,
                "amount_in": int(amount_in),
                "expected_out": amount_out,
                "min_out": min_out,
            }
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    async def _permit2_approve(self, token: str, spender: str) -> None:
        async with web3_from_chain_id(self.chain_id) as web3:
            contract = web3.eth.contract(
                address=to_checksum_address(UNISWAP_PERMIT2), abi=PERMIT2_ABI
            )
            data = contract.encode_abi(
                "approve",
                [
                    to_checksum_address(token),
                    to_checksum_address(spender),
                    _UINT160_MAX,
                    _UINT48_MAX,
                ],
            )
            nonce = await web3.eth.get_transaction_count(
                to_checksum_address(self.owner)
            )
        tx = {
            "chainId": self.chain_id,
            "from": to_checksum_address(self.owner),
            "to": to_checksum_address(UNISWAP_PERMIT2),
            "data": data,
            "value": 0,
            "nonce": nonce,
        }
        await send_transaction(tx, self.sign_callback)

    @staticmethod
    async def _chain_deadline(chain_id: int, deadline_seconds: int) -> int:
        async with web3_from_chain_id(chain_id) as web3:
            block = await web3.eth.get_block("latest")
        return int(block["timestamp"]) + int(deadline_seconds)
