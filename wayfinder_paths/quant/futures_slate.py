"""Canned futures/outrights pipeline: tournament winner, group winner, reach-final etc.

A futures field (e.g. the 48 World Cup trophy candidates) is a single N-outcome market
per vendor. Each vendor's quotes carry heavy vig spread across the whole field, so the
fair probability of one outcome is its implied probability normalized by the SUM of the
vendor's implied probabilities over the entire field — never the raw single quote.
This module fetches a futures market through the gateway, de-vigs per vendor, takes the
median across vendors per outcome, and renders the field.

Two-stage EV design (same as prop/game slates): the fair probabilities here are derived
from SPORTSBOOK quotes — informational only. The executable stage prices each candidate
against its Polymarket market: ``market_edge(fair_p, polymarket_price)``.

CLI:
    poetry run python -m wayfinder_paths.quant.futures_slate \
        --sport worldcup --market-type outright --out .wayfinder_runs/sports
"""

from __future__ import annotations

import asyncio
import csv
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wayfinder_paths.quant import sports_props as sp
from wayfinder_paths.quant.sports_gateway import (
    GatewayPacer,
    fetch_paginated_rows,
)

_MAX_PAGES = 12


@dataclass
class FuturesOutcome:
    subject: str
    abbreviation: str
    fair_p: float  # median de-vigged probability across vendors
    raw_implied_p: float  # median RAW implied (with vig) — gap to fair_p shows the vig
    best_american: float | None  # most favorable price for the bettor
    best_vendor: str
    n_vendors: int


@dataclass
class FuturesResult:
    sport: str
    market_type: str
    market_name: str
    vendors: list[str]
    outcomes: list[FuturesOutcome]  # sorted by fair_p desc
    overround: float  # mean per-vendor sum of raw implied probs (book margin indicator)
    flags: list[str] = field(default_factory=list)
    note: str = (
        "fair_p is the vendor-de-vigged SPORTSBOOK field — informational only. "
        "Executable EV must be priced per candidate on Polymarket: "
        "market_edge(fair_p, polymarket_price)."
    )


def _implied(row: dict[str, Any]) -> float | None:
    dec = row.get("decimal_odds")
    try:
        if dec:
            return 1.0 / float(dec)
        if row.get("american_odds"):
            return sp.american_to_implied(float(row["american_odds"]))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return None


def score_futures(
    rows: list[dict[str, Any]], *, market_type: str, market_name: str | None = None
) -> FuturesResult:
    """De-vig one futures field: per-vendor normalization, then median per outcome."""
    picked = [
        r
        for r in rows
        if str(r.get("market_type")) == market_type
        and (market_name is None or str(r.get("market_name")) == market_name)
    ]
    names = sorted({str(r.get("market_name")) for r in picked})
    flags: list[str] = []
    if market_name is None and len(names) > 1:
        # A market_type can span sub-markets (group_winner has one field per group).
        # De-vigging across them would be wrong — require a market_name.
        raise ValueError(
            f"market_type {market_type!r} spans {len(names)} markets ({names[:6]}...); "
            "pass market_name to pick one."
        )

    by_vendor: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in picked:
        subject = r.get("subject") or {}
        key = str(subject.get("name") or subject.get("id"))
        by_vendor[str(r.get("vendor"))][key] = r

    fair_by_subject: dict[str, list[float]] = defaultdict(list)
    raw_by_subject: dict[str, list[float]] = defaultdict(list)
    best: dict[str, tuple[float, str]] = {}
    overrounds: list[float] = []
    for vendor, subjects in by_vendor.items():
        implied: dict[str, float] = {}
        for k, row in subjects.items():
            value = _implied(row)
            if value is not None and value > 0:
                implied[k] = value
        total = sum(implied.values())
        if total <= 0:
            continue
        overrounds.append(total)
        for k, p_raw in implied.items():
            fair_by_subject[k].append(p_raw / total)
            raw_by_subject[k].append(p_raw)
            american = subjects[k].get("american_odds")
            if american is not None:
                cur = best.get(k)
                # higher american = better payout for the bettor on the same outcome
                if cur is None or float(american) > cur[0]:
                    best[k] = (float(american), vendor)

    outcomes = []
    fair_total = 0.0
    for k, fairs in fair_by_subject.items():
        subject_row = next(
            (
                r.get("subject") or {}
                for r in picked
                if str((r.get("subject") or {}).get("name")) == k
            ),
            {},
        )
        fair = statistics.median(fairs)
        fair_total += fair
        outcomes.append(
            FuturesOutcome(
                subject=k,
                abbreviation=str(subject_row.get("abbreviation") or "")[:4],
                fair_p=fair,
                raw_implied_p=statistics.median(raw_by_subject[k]),
                best_american=best.get(k, (None, ""))[0],
                best_vendor=best.get(k, (None, ""))[1],
                n_vendors=len(fairs),
            )
        )
    # medians across vendors don't sum to exactly 1 — renormalize the field
    if fair_total > 0:
        for outcome in outcomes:
            outcome.fair_p = outcome.fair_p / fair_total
    outcomes.sort(key=lambda o: o.fair_p, reverse=True)

    return FuturesResult(
        sport="",
        market_type=market_type,
        market_name=market_name or (names[0] if names else ""),
        vendors=sorted(by_vendor),
        outcomes=outcomes,
        overround=round(statistics.mean(overrounds), 4) if overrounds else 0.0,
        flags=flags,
    )


async def fetch_futures_rows(
    sport: str, *, client: Any = None, pace_s: float = 1.0
) -> list[dict[str, Any]]:
    if client is None:
        from wayfinder_paths.core.clients.SportsClient import SPORTS_CLIENT

        client = SPORTS_CLIENT
    pacer = GatewayPacer(pace_s)
    return await fetch_paginated_rows(
        client,
        pacer,
        endpoint_id="data.futures.list",
        sport=sport,
        query={"per_page": 100},
        max_pages=_MAX_PAGES,
    )


def render_futures(result: FuturesResult, *, top: int = 20) -> str:
    lines = [
        f"FUTURES — {result.sport} {result.market_type}"
        + (f" ({result.market_name})" if result.market_name else ""),
        f"books: {', '.join(result.vendors) or 'none'} | field size: "
        f"{len(result.outcomes)} | mean overround: {result.overround:.3f} "
        f"({(result.overround - 1) * 100:+.1f}% vig across the field)",
        "",
        f"{'#':>3} {'team':<22} {'abbr':<5} {'fair_p':>7} {'raw_imp':>8} {'best':>7}  vendor",
    ]
    for i, o in enumerate(result.outcomes[:top], 1):
        best = f"{o.best_american:+.0f}" if o.best_american is not None else "-"
        lines.append(
            f"{i:>3} {o.subject:<22.22} {o.abbreviation:<5} {o.fair_p:>7.4f} "
            f"{o.raw_implied_p:>8.4f} {best:>7}  {o.best_vendor}"
        )
    if len(result.outcomes) > top:
        lines.append(f"  ... {len(result.outcomes) - top} more (see artifacts)")
    lines.append("")
    lines.append("NOTE: " + result.note)
    return "\n".join(lines)


def futures_rows_out(result: FuturesResult) -> list[dict[str, Any]]:
    return [
        {
            "subject": o.subject,
            "abbr": o.abbreviation,
            "fair_p": round(o.fair_p, 5),
            "raw_implied_p": round(o.raw_implied_p, 5),
            "best_american": o.best_american,
            "best_vendor": o.best_vendor,
            "n_vendors": o.n_vendors,
        }
        for o in result.outcomes
    ]


async def run_futures_slate(
    sport: str,
    *,
    market_type: str = "outright",
    market_name: str | None = None,
    client: Any = None,
    out_dir: str | Path | None = None,
    top: int = 20,
) -> tuple[FuturesResult, list[str]]:
    rows = await fetch_futures_rows(sport, client=client)
    result = score_futures(rows, market_type=market_type, market_name=market_name)
    result.sport = sport
    artifacts: list[str] = []
    if out_dir:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"futures_{sport}_{market_type}" + (
            f"_{market_name.replace(' ', '_').lower()}" if market_name else ""
        )
        out_rows = futures_rows_out(result)
        json_path = out / f"{stem}.json"
        json_path.write_text(
            json.dumps(
                {
                    "sport": sport,
                    "market_type": market_type,
                    "market_name": result.market_name,
                    "vendors": result.vendors,
                    "overround": result.overround,
                    "note": result.note,
                    "outcomes": out_rows,
                },
                indent=2,
                default=str,
            )
        )
        artifacts.append(str(json_path))
        csv_path = out / f"{stem}.csv"
        if out_rows:
            with csv_path.open("w", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
                writer.writeheader()
                writer.writerows(out_rows)
            artifacts.append(str(csv_path))
    return result, artifacts


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="De-vig a futures field (tournament winner, group winner, ...)."
    )
    parser.add_argument("--sport", required=True)
    parser.add_argument("--market-type", default="outright")
    parser.add_argument("--market-name", default=None)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--out", default=".wayfinder_runs/sports")
    args = parser.parse_args()

    result, artifacts = asyncio.run(
        run_futures_slate(
            args.sport,
            market_type=args.market_type,
            market_name=args.market_name,
            out_dir=args.out,
            top=args.top,
        )
    )
    print(render_futures(result, top=args.top))
    print()
    print("artifacts:", " ".join(artifacts) if artifacts else "(none)")


if __name__ == "__main__":
    _main()
