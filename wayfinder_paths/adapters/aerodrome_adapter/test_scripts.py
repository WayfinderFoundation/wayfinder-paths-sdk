from scripts.protocols.aerodrome._common import ticks_for_percent_range
from scripts.protocols.aerodrome.slipstream_enter_position import (
    _select_pair_tokens,
)


def test_default_ticks_respect_spacing():
    tick_lower, tick_upper = ticks_for_percent_range(123, 10, 5.0)
    assert tick_lower < tick_upper
    assert tick_lower % 10 == 0
    assert tick_upper % 10 == 0


def test_select_pair_tokens_supports_eth_and_btc():
    eth_pair = _select_pair_tokens("eth")
    btc_pair = _select_pair_tokens("btc")

    assert eth_pair[0] != eth_pair[1]
    assert btc_pair[0] != btc_pair[1]
    assert eth_pair != btc_pair


def test_ticks_for_percent_range_respect_spacing():
    tick_lower, tick_upper = ticks_for_percent_range(205, 20, 4.0)
    assert tick_lower < tick_upper
    assert tick_lower % 20 == 0
    assert tick_upper % 20 == 0
