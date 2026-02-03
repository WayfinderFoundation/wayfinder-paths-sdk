import math
import random

from wayfinder_paths.core.analytics.bootstrap import block_bootstrap_paths
from wayfinder_paths.core.analytics.stats import (
    percentile,
    rolling_min_sum,
    z_from_conf,
)


class TestBlockBootstrapPaths:
    def test_returns_empty_when_sims_zero(self):
        result = block_bootstrap_paths(
            [1.0, 2.0, 3.0], block_hours=2, sims=0, rng=random.Random(42)
        )
        assert result == []

    def test_returns_empty_when_series_empty(self):
        result = block_bootstrap_paths(block_hours=2, sims=10, rng=random.Random(42))
        assert result == []

    def test_returns_empty_when_base_len_one(self):
        result = block_bootstrap_paths(
            [1.0], block_hours=2, sims=10, rng=random.Random(42)
        )
        assert result == []

    def test_single_series(self):
        series = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = block_bootstrap_paths(
            series, block_hours=2, sims=5, rng=random.Random(42)
        )

        assert len(result) == 5
        for path in result:
            assert len(path) == 1
            assert len(path[0]) == 5

    def test_multiple_aligned_series(self):
        series_a = [1.0, 2.0, 3.0, 4.0, 5.0]
        series_b = [10.0, 20.0, 30.0, 40.0, 50.0]
        result = block_bootstrap_paths(
            series_a, series_b, block_hours=2, sims=3, rng=random.Random(42)
        )

        assert len(result) == 3
        for path in result:
            assert len(path) == 2
            assert len(path[0]) == 5
            assert len(path[1]) == 5

    def test_preserves_length(self):
        series = list(range(100))
        series_float = [float(x) for x in series]
        result = block_bootstrap_paths(
            series_float, block_hours=24, sims=10, rng=random.Random(42)
        )

        for path in result:
            assert len(path[0]) == 100

    def test_block_clamping(self):
        series = [1.0, 2.0, 3.0]

        # Block larger than series - should still work
        result = block_bootstrap_paths(
            series, block_hours=100, sims=2, rng=random.Random(42)
        )
        assert len(result) == 2

        # Block of zero - should clamp to 1
        result = block_bootstrap_paths(
            series, block_hours=0, sims=2, rng=random.Random(42)
        )
        assert len(result) == 2


class TestZFromConf:
    def test_95_confidence(self):
        z = z_from_conf(0.95)
        assert 1.95 < z < 1.97

    def test_99_confidence(self):
        z = z_from_conf(0.99)
        assert 2.57 < z < 2.58

    def test_90_confidence(self):
        z = z_from_conf(0.90)
        assert 1.64 < z < 1.66


class TestRollingMinSum:
    def test_basic(self):
        arr = [1, -2, 3, -4, 5]
        result = rolling_min_sum(arr, 2)
        # Windows: [1,-2]=-1, [-2,3]=1, [3,-4]=-1, [-4,5]=1
        assert result == -1

    def test_window_larger_than_arr(self):
        arr = [1.0, 2.0, 3.0]
        result = rolling_min_sum(arr, 10)
        assert result == 6.0

    def test_window_zero(self):
        arr = [1.0, 2.0, 3.0]
        result = rolling_min_sum(arr, 0)
        assert result == 0.0

    def test_all_negative(self):
        arr = [-1.0, -2.0, -3.0, -4.0]
        result = rolling_min_sum(arr, 2)
        # Windows: [-1,-2]=-3, [-2,-3]=-5, [-3,-4]=-7
        assert result == -7.0


class TestPercentile:
    def test_empty_returns_nan(self):
        result = percentile([], 0.5)
        assert math.isnan(result)

    def test_single_value(self):
        assert percentile([42.0], 0.0) == 42.0
        assert percentile([42.0], 0.5) == 42.0
        assert percentile([42.0], 1.0) == 42.0

    def test_median(self):
        sorted_values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = percentile(sorted_values, 0.5)
        assert result == 3.0

    def test_interpolation(self):
        sorted_values = [0.0, 10.0]
        # 25th percentile should interpolate to 2.5
        result = percentile(sorted_values, 0.25)
        assert result == 2.5

    def test_bounds_clamped(self):
        sorted_values = [1.0, 2.0, 3.0]
        assert percentile(sorted_values, -1.0) == 1.0
        assert percentile(sorted_values, 2.0) == 3.0
