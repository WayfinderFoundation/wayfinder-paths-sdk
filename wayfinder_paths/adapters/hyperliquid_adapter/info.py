from hyperliquid.info import Info
from hyperliquid.utils import constants

_INFO: Info | None = None


def get_info() -> Info:
    global _INFO
    if _INFO is None:
        info = Info(constants.MAINNET_API_URL, skip_ws=True)
        perp_dexes = [""] + [i["name"] for i in info.perp_dexs() if i is not None]
        _INFO = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=perp_dexes)
    return _INFO
