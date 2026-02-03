from decimal import Decimal

import pytest

from wayfinder_paths.adapters.hyperliquid_adapter.utils import (
    normalize_l2_book,
    round_size_for_asset,
    size_step,
    spot_index_from_asset_id,
    sz_decimals_for_asset,
    usd_depth_in_band,
)


class TestSpotIndexFromAssetId:
    def test_valid_spot_id(self):
        assert spot_index_from_asset_id(10000) == 0
        assert spot_index_from_asset_id(10001) == 1
        assert spot_index_from_asset_id(10107) == 107

    def test_rejects_perp_id(self):
        with pytest.raises(ValueError, match="Expected spot asset_id >= 10000"):
            spot_index_from_asset_id(0)

        with pytest.raises(ValueError, match="Expected spot asset_id >= 10000"):
            spot_index_from_asset_id(9999)


class TestNormalizeL2Book:
    def test_levels_format(self):
        raw = {
            "levels": [
                [{"px": "100.5", "sz": "10"}],
                [{"px": "101.0", "sz": "5"}],
            ],
            "midPx": "100.75",
        }
        result = normalize_l2_book(raw)

        assert result["bids"] == [(100.5, 10.0)]
        assert result["asks"] == [(101.0, 5.0)]
        assert result["midPx"] == 100.75

    def test_bids_asks_format(self):
        raw = {
            "bids": [{"px": "100.5", "sz": "10"}],
            "asks": [{"px": "101.0", "sz": "5"}],
        }
        result = normalize_l2_book(raw)

        assert result["bids"] == [(100.5, 10.0)]
        assert result["asks"] == [(101.0, 5.0)]

    def test_calculates_mid_from_bids_asks(self):
        raw = {
            "levels": [
                [{"px": "100.0", "sz": "10"}],
                [{"px": "102.0", "sz": "5"}],
            ],
        }
        result = normalize_l2_book(raw)

        assert result["midPx"] == 101.0

    def test_fallback_mid(self):
        raw = {"levels": [[], []]}
        result = normalize_l2_book(raw, fallback_mid=99.0)

        assert result["midPx"] == 99.0

    def test_invalid_levels_skipped(self):
        raw = {
            "levels": [
                [
                    {"px": "100.0", "sz": "10"},
                    {"px": "invalid", "sz": "5"},
                    {"px": "0", "sz": "5"},
                    {"px": "50", "sz": "0"},
                ],
                [{"px": "101.0", "sz": "5"}],
            ],
        }
        result = normalize_l2_book(raw)

        assert result["bids"] == [(100.0, 10.0)]

    def test_tuple_format_levels(self):
        raw = {
            "levels": [
                [[100.0, 10.0]],
                [[101.0, 5.0]],
            ],
        }
        result = normalize_l2_book(raw)

        assert result["bids"] == [(100.0, 10.0)]
        assert result["asks"] == [(101.0, 5.0)]


class TestUsdDepthInBand:
    def test_buy_side(self):
        book = {
            "bids": [(99.0, 10.0), (98.0, 20.0)],
            "asks": [(101.0, 5.0), (102.0, 10.0)],
            "midPx": 100.0,
        }
        # 100 bps = 1% band, so hi = 101.0
        depth, mid = usd_depth_in_band(book, band_bps=100, side="buy")

        assert mid == 100.0
        # Only asks <= 101.0 count: 101.0 * 5.0 = 505
        assert depth == 505.0

    def test_sell_side(self):
        book = {
            "bids": [(99.0, 10.0), (98.0, 20.0)],
            "asks": [(101.0, 5.0), (102.0, 10.0)],
            "midPx": 100.0,
        }
        # 100 bps = 1% band, so lo = 99.0
        depth, mid = usd_depth_in_band(book, band_bps=100, side="sell")

        assert mid == 100.0
        # Only bids >= 99.0 count: 99.0 * 10.0 = 990
        assert depth == 990.0

    def test_zero_mid(self):
        book = {"bids": [(99.0, 10.0)], "asks": [(101.0, 5.0)], "midPx": 0.0}
        depth, mid = usd_depth_in_band(book, band_bps=100, side="buy")

        assert depth == 0.0
        assert mid == 0.0


class TestSzDecimalsForAsset:
    def test_known_asset(self):
        asset_to_sz = {0: 4, 1: 3, 10000: 6}

        assert sz_decimals_for_asset(asset_to_sz, 0) == 4
        assert sz_decimals_for_asset(asset_to_sz, 1) == 3
        assert sz_decimals_for_asset(asset_to_sz, 10000) == 6

    def test_unknown_raises(self):
        asset_to_sz = {0: 4}

        with pytest.raises(ValueError, match="Unknown asset_id 999"):
            sz_decimals_for_asset(asset_to_sz, 999)


class TestSizeStep:
    def test_size_step_calculation(self):
        asset_to_sz = {0: 4, 1: 2}

        assert size_step(asset_to_sz, 0) == Decimal("0.0001")
        assert size_step(asset_to_sz, 1) == Decimal("0.01")


class TestRoundSizeForAsset:
    def test_floors_correctly(self):
        asset_to_sz = {0: 4}

        # 1.23456789 should floor to 1.2345
        result = round_size_for_asset(asset_to_sz, 0, 1.23456789)
        assert result == 1.2345

    def test_zero_returns_zero(self):
        asset_to_sz = {0: 4}

        assert round_size_for_asset(asset_to_sz, 0, 0.0) == 0.0
        assert round_size_for_asset(asset_to_sz, 0, -1.0) == 0.0

    def test_ensure_min_step(self):
        asset_to_sz = {0: 4}

        # Without ensure_min_step: 0.00001 floors to 0
        result = round_size_for_asset(asset_to_sz, 0, 0.00001, ensure_min_step=False)
        assert result == 0.0

        # With ensure_min_step: 0.00001 becomes 0.0001 (one step)
        result = round_size_for_asset(asset_to_sz, 0, 0.00001, ensure_min_step=True)
        assert result == 0.0001

    def test_preserves_precision(self):
        asset_to_sz = {0: 2}

        # 0.1 + 0.2 = 0.30000000000000004 in float
        result = round_size_for_asset(asset_to_sz, 0, 0.1 + 0.2)
        assert result == 0.30
