import time
from decimal import ROUND_DOWN, Decimal
from typing import Any

from hyperliquid.info import Info
from loguru import logger


def _get_sigfigs(price):
    num_str = str(price).strip().lower()
    if "e" in num_str:
        mantissa = num_str.split("e")[0]
        mantissa = mantissa.replace(".", "")
        mantissa = mantissa.strip("0")
        return len(mantissa)

    if "." in num_str:
        int_part, dec_part = num_str.split(".")
        int_part = int_part.lstrip("0")
        num_str = int_part + dec_part
        if dec_part:
            num_str = num_str.rstrip("0")
    else:
        num_str = num_str.rstrip("0")
    return len(num_str)


class Util:
    def __init__(self, info: Info):
        self.info: Info = info

    def get_hypecore_spot_assets(self):
        response = {}
        spot_meta_attr = self.info.spot_meta
        spot_meta = spot_meta_attr() if callable(spot_meta_attr) else spot_meta_attr
        for i in spot_meta["universe"]:
            base, quote = i["tokens"]
            base_info = spot_meta["tokens"][base]
            quote_info = spot_meta["tokens"][quote]
            name = f"{base_info['name']}/{quote_info['name']}"
            response[name] = i["index"] + 10000
        return response

    def get_hypecore_perpetual_assets(self):
        response = {}
        for k, v in self.info.coin_to_asset.items():
            # First 10_000 are default perp ids
            # Anything over 100_000 are HIP3 perp ids
            if 0 <= v < 10000 or 100000 <= v:
                response[k] = v

        return response

    def get_hypecore_asset_id(self, asset_name, is_perp):
        assets = (
            self.get_hypecore_spot_assets()
            if not is_perp
            else self.get_hypecore_perpetual_assets()
        )
        return assets.get(asset_name)

    async def get_hypecore_all_dex_mid_prices(self):
        # Backwards compatible wrapper: the Hyperliquid SDK now exposes `all_mids()`.
        return self.info.all_mids()

    async def get_hypecore_all_dex_meta_universe(self):
        # Backwards compatible wrapper: the Hyperliquid SDK now exposes `meta()`.
        return self.info.meta()

    def get_size_decimals_for_hypecore_asset(self, asset_id: int):
        return self.info.asset_to_sz_decimals[asset_id]

    def get_price_decimals_for_hypecore_asset(self, asset_id: int):
        is_spot = asset_id >= 10_000
        decimals = (
            6 if not is_spot else 8
        ) - self.get_size_decimals_for_hypecore_asset(asset_id)
        return decimals

    def get_valid_hypecore_order_size(self, asset_id: int, size: float):
        decimals = self.get_size_decimals_for_hypecore_asset(asset_id)
        step = Decimal(10) ** -decimals
        value = Decimal(str(size)).quantize(step, rounding=ROUND_DOWN)
        return float(value)

    def get_valid_hypecore_price_size(self, asset_id: int, price: float):
        decimals = self.get_price_decimals_for_hypecore_asset(asset_id)
        actual_decimals = max(str(price)[::-1].find("."), 0)

        sigfigs = _get_sigfigs(price)
        if sigfigs > 5 and actual_decimals:
            price = float(f"{price:.5g}")

        if actual_decimals > decimals:
            price = max(10**-decimals, round(price, decimals))
        return price

    def _reformat_perp_user_state(self, perp_user_state: dict) -> dict:
        asset_positions = perp_user_state.get("assetPositions", [])
        for pos in asset_positions:
            position = pos.get("position", {})
            # Fix funding direction: negative = earned, positive = paid
            if "cumFunding" in position:
                old_funding = position.pop("cumFunding")
                position["cumFundingEarned"] = {
                    k: str(float(v)) for k, v in old_funding.items()
                }
        return perp_user_state

    async def get_hypecore_user(self, address):
        perp_user_state = self.info.user_state(address)
        spot_user_state = self.info.spot_user_state(address)
        open_orders = self.info.open_orders(address)
        formatted_perp_state = self._reformat_perp_user_state(perp_user_state)
        state = {
            "perp_user_state": formatted_perp_state,
            "spot_user_state": spot_user_state,
            "open_orders": open_orders,
        }
        logger.info(state)
        return state

    def get_perp_margin_amount(self, state):
        return float(state["perp_user_state"]["marginSummary"]["accountValue"])

    def get_spot_usdc_amount(self, state):
        for i in state["spot_user_state"]["balances"]:
            if i["coin"] == "USDC":
                return float(i["total"])
        return 0.0

    def get_margin_utilization(self, state):
        account_value = float(state["perp_user_state"]["marginSummary"]["accountValue"])
        total_margin_used = float(
            state["perp_user_state"]["marginSummary"]["totalMarginUsed"]
        )
        return total_margin_used / account_value if account_value > 0 else 0.0

    async def get_spot_account_value(
        self,
        state,
        ignore_dust=False,
        dust_threshold: float = 1.0,
        mid_prices: dict[str, Any] | None = None,
    ):
        if mid_prices is None:
            mid_prices = await self.get_hypecore_all_dex_mid_prices()

        total_spot = 0.0
        for i in state["spot_user_state"]["balances"]:
            asset_name = i["coin"]
            mid_price = 0.0
            if asset_name == "USDC":
                mid_price = 1.0
            else:
                asset_id = self.get_hypecore_asset_id(
                    f"{asset_name}/USDC", is_perp=False
                )
                mid_price_id = self.info.asset_to_coin[asset_id]
                raw_price = mid_prices.get(mid_price_id)
                mid_price = float(raw_price) if raw_price is not None else 0.0
            value = mid_price * float(i["total"])
            if ignore_dust and value < dust_threshold:
                continue
            total_spot += value
        return total_spot

    async def fetch_hypecore_user_fills(self, wallet: str):
        """Fetch all available trade fills (up to the most-recent 10,000) from HypeCore."""
        start, end = 0, int(time.time() * 1000)
        out = []
        while True:
            try:
                batch = self.info.user_fills_by_time(wallet, start, end, False)
            except Exception as e:
                logger.error(f"Failed to fetch fills via node/public/SDK: {e}")
                break
            if not batch or len(batch) == 0:
                break
            out.extend(batch)
            start = batch[-1]["time"] + 1
            if len(batch) < 2000:  # each page ≤ 2000 rows
                break
        return out

    async def get_hypecore_position(self, address, asset_name):
        perp_user_state = self.info.user_state(address)
        formatted_perp_user_state = self._reformat_perp_user_state(perp_user_state)

        for pos in formatted_perp_user_state.get("assetPositions", []):
            if pos["position"]["coin"] == asset_name:
                return pos

        return None

    @classmethod
    def parse_dollar_value(cls, response: dict) -> Decimal | None:
        if response.get("status", "") != "ok" or not len(
            resp := response.get("response", {})
        ):
            return None

        if resp.get("type", "") != "order" or not len(data := resp.get("data", {})):
            return None

        statuses = data.get("statuses", [])

        if not len(statuses):
            return None

        return sum(
            [
                Decimal(status["filled"]["totalSz"])
                * Decimal(status["filled"]["avgPx"])
                for status in statuses
                if "filled" in status
            ],
            Decimal(0),
        )

    @staticmethod
    def _sig_hex_to_hl_signature(sig_hex: str) -> dict[str, Any]:
        """Convert a 65-byte hex signature into Hyperliquid {r,s,v}."""
        if not isinstance(sig_hex, str) or not sig_hex.startswith("0x"):
            raise ValueError("Expected hex signature string starting with 0x")
        raw = bytes.fromhex(sig_hex[2:])
        if len(raw) != 65:
            raise ValueError(f"Expected 65-byte signature, got {len(raw)} bytes")

        r = raw[0:32]
        s = raw[32:64]
        v = raw[64]
        # Normalize v to 27/28 when needed.
        if v < 27:
            v += 27

        return {"r": f"0x{r.hex()}", "s": f"0x{s.hex()}", "v": int(v)}
