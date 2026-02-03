from wayfinder_paths.core.constants.chains import CHAIN_EXPLORER_URLS


def get_etherscan_transaction_link(chain_id: int, tx_hash: str) -> str | None:
    base_url = CHAIN_EXPLORER_URLS.get(chain_id)
    if not base_url:
        return None
    return f"{base_url}tx/{tx_hash}"
