MAX_SLIPPAGE_BPS = 1_000
MAX_UNISWAP_FEE = 10_000
MAX_PRICE_IMPACT_BPS = 300


def effective_slippage_bps(slippage_bps: int) -> int:
    return min(max(0, int(slippage_bps)), MAX_SLIPPAGE_BPS)


def calculate_price_impact_bps(
    amount_in: int,
    amount_out: int,
    probe_amount_in: int,
    probe_amount_out: int,
) -> int:
    if min(amount_in, amount_out, probe_amount_in, probe_amount_out) <= 0:
        return 10_000
    expected_scaled = int(probe_amount_out) * int(amount_in)
    actual_scaled = int(amount_out) * int(probe_amount_in)
    if actual_scaled >= expected_scaled:
        return 0
    return min(10_000, (expected_scaled - actual_scaled) * 10_000 // expected_scaled)
