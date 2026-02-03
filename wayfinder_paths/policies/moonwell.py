from wayfinder_paths.core.constants.contracts import (
    BASE_WETH,
    MOONWELL_COMPTROLLER,
    MOONWELL_M_USDC,
    MOONWELL_M_WETH,
    MOONWELL_M_WSTETH,
)
from wayfinder_paths.core.constants.moonwell_abi import COMPTROLLER_ABI
from wayfinder_paths.policies.util import allow_functions

WETH = BASE_WETH
M_USDC = MOONWELL_M_USDC
M_WETH = MOONWELL_M_WETH
M_WSTETH = MOONWELL_M_WSTETH
COMPTROLLER = MOONWELL_COMPTROLLER


async def weth_deposit():
    return await allow_functions(
        policy_name="Allow WETH Deposit",
        abi_chain_id=8453,
        address=WETH,
        function_names=["deposit"],
    )


async def musdc_mint_or_approve_or_redeem():
    return await allow_functions(
        policy_name="Allow MUSDC Mint or Approve or Redeem",
        abi_chain_id=8453,
        address=M_USDC,
        function_names=["mint", "approve", "redeem"],
    )


async def mweth_approve_or_borrow_or_repay():
    return await allow_functions(
        policy_name="Allow MWETH Approve or Borrow or Repay",
        abi_chain_id=8453,
        address=M_WETH,
        function_names=["approve", "borrow", "repayBorrow"],
    )


async def mwsteth_approve_or_mint_or_redeem():
    return await allow_functions(
        policy_name="Allow MWSTETH Approve or Mint or Redeem",
        abi_chain_id=8453,
        address=M_WSTETH,
        function_names=["approve", "mint", "redeem"],
    )


async def moonwell_comptroller_enter_markets_or_claim_rewards():
    return await allow_functions(
        policy_name="Allow lend and claimReward",
        abi_chain_id=8453,
        address=COMPTROLLER,
        function_names=["enterMarkets", "claimReward"],
        manual_abi=COMPTROLLER_ABI,
    )
