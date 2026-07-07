"""Live N x N BRAP route-completeness grid over every supported chain's native token.

One node per supported chain: that chain's native token (the 0xEeee sentinel).
The grid is the full product of nodes — every native routed to every other
native, same-chain and cross-chain. Each off-diagonal cell hits BRAP for real and
must return at least one route ("a solution for every hop"). The same-chain
diagonal is native->native identity, so it is auto-complete and never quoted.

Gated `local` + `requires_config`: it hits the live backend and needs an API key.
Run with output:

    WAYFINDER_API_KEY=wk_... poetry run pytest -s \
        wayfinder_paths/tests/test_brap_native_grid_live.py

Knobs (env): WAYFINDER_GRID_AMOUNT_RAW, WAYFINDER_GRID_WALLET,
WAYFINDER_GRID_LATENCY_BUDGET_S, WAYFINDER_GRID_CONCURRENCY.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass

import pytest

from wayfinder_paths.core.clients.BRAPClient import BRAP_CLIENT
from wayfinder_paths.core.config import get_api_key
from wayfinder_paths.core.constants.chains import CHAIN_ID_TO_CODE, SUPPORTED_CHAINS
from wayfinder_paths.core.constants.contracts import NATIVE_TOKEN_SENTINEL
from wayfinder_paths.mcp.tools.quotes import _unwrap_brap_quote_response

GRID_CHAINS = SUPPORTED_CHAINS

# Native is 18-decimal on every supported EVM chain; a mid-size probe so a route
# clears per-provider dust minimums. 0.05 native default.
GRID_AMOUNT_RAW = os.environ.get("WAYFINDER_GRID_AMOUNT_RAW") or str(5 * 10**16)

# Quote is a pricing call (execute checks balance separately), so any well-formed
# address works as the quoting wallet.
GRID_WALLET = (
    os.environ.get("WAYFINDER_GRID_WALLET")
    or "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
)

# Per-hop wall-clock budget. A live BRAP round-trip is seconds, so the budget is
# seconds (the "4-5" in the ask), overridable.
LATENCY_BUDGET_S = float(os.environ.get("WAYFINDER_GRID_LATENCY_BUDGET_S") or "5.0")

# Backend throttles at 120 req/min/key; keep the burst under that.
GRID_CONCURRENCY = int(os.environ.get("WAYFINDER_GRID_CONCURRENCY") or "6")


@dataclass
class Cell:
    from_chain: int
    to_chain: int
    ok: bool
    identity: bool
    quote_count: int
    elapsed: float
    error: str | None


def _code(chain_id: int) -> str:
    return CHAIN_ID_TO_CODE[chain_id]


async def _solve_cell(sem: asyncio.Semaphore, from_chain: int, to_chain: int) -> Cell:
    if from_chain == to_chain:
        return Cell(from_chain, to_chain, True, True, 0, 0.0, None)

    async with sem:
        start = time.perf_counter()
        try:
            data = await BRAP_CLIENT.get_quote(
                from_token=NATIVE_TOKEN_SENTINEL,
                to_token=NATIVE_TOKEN_SENTINEL,
                from_chain=from_chain,
                to_chain=to_chain,
                from_wallet=GRID_WALLET,
                from_amount=GRID_AMOUNT_RAW,
            )
            elapsed = time.perf_counter() - start
            _all_quotes, best, count = _unwrap_brap_quote_response(data)
            has_solution = best is not None or count > 0
            return Cell(from_chain, to_chain, has_solution, False, count, elapsed, None)
        except Exception as exc:  # noqa: BLE001 - a raising cell is a failed cell
            elapsed = time.perf_counter() - start
            return Cell(from_chain, to_chain, False, False, 0, elapsed, str(exc))


def _render_matrix(cells: dict[tuple[int, int], Cell]) -> str:
    labels = [_code(c)[:4] for c in GRID_CHAINS]
    width = max(len(x) for x in labels)
    header = " " * (width + 1) + " ".join(x.rjust(width) for x in labels)
    lines = [header]
    for from_chain, row_label in zip(GRID_CHAINS, labels, strict=True):
        marks = []
        for to_chain in GRID_CHAINS:
            cell = cells[(from_chain, to_chain)]
            mark = "." if cell.identity else ("O" if cell.ok else "X")
            marks.append(mark.rjust(width))
        lines.append(row_label.rjust(width) + " " + " ".join(marks))
    return "\n".join(lines)


def _summary(cells: list[Cell]) -> str:
    routed = [c for c in cells if not c.identity]
    ok = [c for c in routed if c.ok]
    lat = sorted(c.elapsed for c in ok)
    stats = ""
    if lat:
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        stats = (
            f" | latency s min={lat[0]:.2f} "
            f"mean={sum(lat) / len(lat):.2f} p95={p95:.2f} max={lat[-1]:.2f}"
        )
    return f"routed {len(ok)}/{len(routed)} hops (+{len(cells) - len(routed)} identity){stats}"


@pytest.mark.local
@pytest.mark.requires_config
async def test_brap_native_wrap_grid_live() -> None:
    if not get_api_key():
        pytest.skip("live BRAP grid needs WAYFINDER_API_KEY or config.json api_key")

    sem = asyncio.Semaphore(GRID_CONCURRENCY)
    pairs = [(f, t) for f in GRID_CHAINS for t in GRID_CHAINS]
    results = await asyncio.gather(*(_solve_cell(sem, f, t) for f, t in pairs))
    cells = {(c.from_chain, c.to_chain): c for c in results}

    print("\n" + _render_matrix(cells) + "\n" + _summary(results))

    missing = [c for c in results if not c.identity and not c.ok]
    slow = [
        c for c in results if c.ok and not c.identity and c.elapsed > LATENCY_BUDGET_S
    ]

    if missing:
        detail = "\n".join(
            f"  {_code(c.from_chain)} -> {_code(c.to_chain)}: {c.error or 'no route'}"
            for c in missing
        )
        pytest.fail(f"{len(missing)} hop(s) with no route:\n{detail}")

    if slow:
        detail = "\n".join(
            f"  {_code(c.from_chain)} -> {_code(c.to_chain)}: {c.elapsed:.2f}s"
            for c in slow
        )
        pytest.fail(f"{len(slow)} hop(s) over {LATENCY_BUDGET_S}s budget:\n{detail}")
