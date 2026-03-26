from wayfinder_paths.policies.evm import any_evm_transaction
from wayfinder_paths.policies.hyperliquid import (
    any_hyperliquid_l1_payload,
    any_hyperliquid_user_payload,
)


def anything() -> list[dict]:
    return [
        any_evm_transaction(),
        any_hyperliquid_l1_payload(),
        any_hyperliquid_user_payload(),
    ]
