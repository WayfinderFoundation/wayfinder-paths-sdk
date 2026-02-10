from hyperliquid.info import Info
from hyperliquid.utils import constants

_INFO: Info | None = None
_PERP_DEXES: list[str] = [""]


def get_info() -> Info:
    global _INFO, _PERP_DEXES
    if _INFO is None:
        info = Info(constants.MAINNET_API_URL, skip_ws=True)
        _PERP_DEXES = [""] + [i["name"] for i in info.perp_dexs() if i is not None]
        _INFO = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=_PERP_DEXES)
    return _INFO


def get_perp_dexes() -> list[str]:
    get_info()  # ensure initialized
    return _PERP_DEXES
