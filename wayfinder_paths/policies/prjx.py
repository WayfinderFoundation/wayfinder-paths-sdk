from wayfinder_paths.core.constants.contracts import PRJX_NPM, PRJX_ROUTER
from wayfinder_paths.core.constants.projectx_abi import (
    PROJECTX_NPM_ABI,
    PROJECTX_ROUTER_ABI,
)
from wayfinder_paths.policies.util import allow_functions


async def prjx_swap():
    return await allow_functions(
        policy_name="Allow PRJX Swap",
        abi_chain_id=999,
        address=PRJX_ROUTER,
        function_names=[
            "exactInput",
            "exactInputSingle",
        ],
        manual_abi=PROJECTX_ROUTER_ABI,
    )


async def prjx_npm():
    return await allow_functions(
        policy_name="Allow PRJX NPM",
        abi_chain_id=999,
        address=PRJX_NPM,
        function_names=[
            "mint",
            "increaseLiquidity",
            "decreaseLiquidity",
            "collect",
            "burn",
        ],
        manual_abi=PROJECTX_NPM_ABI,
    )
