from hyperliquid.info import Info
from hyperliquid.utils import constants

INFO = Info(constants.MAINNET_API_URL, skip_ws=True)
PERP_DEXES = [""] + [i["name"] for i in INFO.perp_dexs() if i is not None]
INFO = Info(constants.MAINNET_API_URL, skip_ws=True, perp_dexs=PERP_DEXES)
