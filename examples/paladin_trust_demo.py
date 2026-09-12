"""Demo: screen real Base tokens an agent would route a swap into.

Run it as-is to exercise the free OFAC screen:

    python examples/paladin_trust_demo.py

Set PALADIN_DEMO_PRIVATE_KEY (a funded Base wallet) to also exercise the
x402-paid full trust composition (~$0.001 USDC per token):

    PALADIN_DEMO_PRIVATE_KEY=0x... python examples/paladin_trust_demo.py
"""
from __future__ import annotations

import asyncio
import os

from wayfinder_paths.adapters.paladin_trust_adapter.adapter import PaladinTrustAdapter

# Real Base (chainId 8453) tokens. AERO/WELL/cbBTC are legit tokens the SDK's
# own adapters route into; NORMIE is a real token GoPlus flags as a honeypot.
TOKENS = {
    "AERO (Aerodrome)": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
    "WELL (Moonwell)": "0xA88594D404727625A9437C3f886C7643872296AE",
    "cbBTC": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
    "NORMIE (honeypot)": "0x7F12d13B34F5F4f0a9449c16Bcd42f0da47AF200",
}
# A live OFAC SDN-listed address (real entry on the U.S. Treasury list).
SANCTIONED_ADDRESS = "0x0330070fd38ec3bb94f58fa55d40368271e9e54a"
CLEAN_WALLET = "0xeA8C33d018760D034384e92D1B2a7cf0338834b4"


def _line(label: str, ok: bool, result: object) -> str:
    if not ok:
        return f"  {label:<22} ERROR: {result}"
    trust = result["trust"] if isinstance(result, dict) else {}
    rec = trust.get("recommendation", "?")
    signals = ", ".join(
        f"{f.get('source')}:{f.get('signal')}" for f in trust.get("factors", [])
    )
    return f"  {label:<22} {rec.upper():<6} [{signals}]"


async def main() -> None:
    config = {}
    private_key = os.environ.get("PALADIN_DEMO_PRIVATE_KEY")
    if private_key:
        config["private_key"] = private_key
    adapter = PaladinTrustAdapter(config)

    print("Free OFAC wallet screen (no payment):")
    ok, res = await adapter.screen_wallet_ofac(CLEAN_WALLET)
    print(_line("clean wallet", ok, res))
    ok, res = await adapter.screen_wallet_ofac(SANCTIONED_ADDRESS)
    print(_line("OFAC SDN address", ok, res))

    if not adapter.can_pay:
        print(
            "\nFull trust composition (check_token) skipped — set "
            "PALADIN_DEMO_PRIVATE_KEY to a funded Base wallet to run the "
            "x402-paid screen on the tokens below."
        )
        return

    print("\nFull trust composition via x402-paid check_token ($0.001/token):")
    for label, address in TOKENS.items():
        ok, res = await adapter.check_token(address, chain_id=8453)
        print(_line(label, ok, res))


if __name__ == "__main__":
    asyncio.run(main())
