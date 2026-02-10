# Scenario testing checklist (before “live”)

## Minimum for a new/changed fund-moving flow

1. **Read-only validation first**
   - Confirm token addresses/decimals and chain IDs.
   - Fetch a quote/status/analyze before executing anything.

2. **Happy-path fork run**
   - Seed balances (native gas + required ERC20s).
   - Run the full sequence on a fork (e.g. approve → swap → lend; or deposit → update).

3. **Assertions (don’t just “broadcast”)**
   - Receipt `status=1` for every tx.
   - At least one state assertion per step:
     - balances moved as expected
     - position/health factor changed as expected
     - allowance set when required

4. **At least one failure scenario**
   - Too little balance, missing allowance, slippage too tight, wrong decimals.
   - Confirm the error is surfaced clearly and the flow stops safely.

5. **Only then: live execution**
   - Use small size.
   - Require explicit confirmation (e.g. `--confirm-live`).
   - Verify on-chain receipt success (not just tx hash).

## What “good” looks like

- The same script/strategy entrypoint works in both modes (fork vs live) with only a flag change.
- Seeded balances and RPC routing are handled outside the strategy logic (decorator/context), not in the strategy code.

