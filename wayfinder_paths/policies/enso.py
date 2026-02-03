from wayfinder_paths.core.constants.contracts import ENSO_ROUTER
from wayfinder_paths.policies.util import allow_functions


async def enso_swap():
    return await allow_functions(
        policy_name="Allow Enso Swap",
        abi_chain_id=8453,
        address=ENSO_ROUTER,
        function_names=[
            "routeMulti",
            "routeSingle",
            "safeRouteMulti",
            "safeRouteSingle",
        ],
    )
