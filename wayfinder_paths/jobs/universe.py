"""Universe screening: candidate symbols for a job's strategy family.

The audit's core finding was agents optimizing toward "less bad" inside a
symbol universe that had shown no positive edge anywhere — with no lane to
question the universe itself. This op is that lane: screen the venue's perp
universe by liquidity, run the job's OWN signal library (with regime
conditioning) over each candidate's recent bars, and pool every row into one
BH family so the screen carries its own multiplicity honesty.

A screen is a SHORTLIST, not promotion evidence: it looks at top-volume
symbols only (survivorship) and its q-values price the whole sweep. An
admitted symbol must earn deployment through its own on-job scans and
probation forward — the artifact's `read` says exactly that.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from wayfinder_paths.jobs.ledger import append_ledger_row
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

UNIVERSE_SCAN_PATH = "results/research/universe_scan.json"
DEFAULT_TOP = 10
DEFAULT_MIN_VOLUME_USD = 5_000_000
DEFAULT_DAYS = 14
_BAR_INTERVAL = "5m"
_BAR_SECONDS = 300
_MAX_ROWS_PER_SYMBOL = 6
_READ = (
    "Universe SCREEN, not promotion evidence: top-volume symbols only "
    "(survivorship) with all rows pooled into ONE BH family for this sweep. "
    "Use it to shortlist symbol swaps: kill a current symbol only by citing "
    "its accumulated negative evidence (ledger/dossier), admit a candidate "
    "at probation sizing with pre-registered graduate/kill criteria, and "
    "stage BOTH edits (job.yaml symbols + strategy legs) in ONE proposal. "
    "An admitted symbol must then earn full size through its own on-job "
    "scans and probation forward — screen q-values do not transfer."
)


def universe_scan_job(
    job_id: str,
    *,
    top: int = DEFAULT_TOP,
    min_volume_usd: float = DEFAULT_MIN_VOLUME_USD,
    days: int = DEFAULT_DAYS,
    min_events: int = 20,
    store: JobStore | None = None,
    fetch_universe: Callable[[], list[dict[str, Any]]] | None = None,
    fetch_bars: Callable[[str, int], Any] | None = None,
) -> dict[str, Any]:
    import yaml

    from wayfinder_paths.jobs.research import apply_bh_verdicts, scan_signals

    store = store or JobStore()
    root = store.job_dir(job_id)
    job_data = yaml.safe_load((root / "job.yaml").read_text(encoding="utf-8")) or {}
    params = dict(job_data.get("execution_params") or {})
    spec = dict(job_data.get("execution_spec") or {})
    current = {
        str(symbol)
        for symbol in (
            params.get("symbols")
            or (spec.get("data_contract") or {}).get("symbols")
            or []
        )
    }

    listed = (fetch_universe or _fetch_hl_universe)()
    eligible = [
        entry
        for entry in listed
        if float(entry.get("volume_24h_usd") or 0) >= float(min_volume_usd)
        and str(entry.get("symbol")) not in current
        and not entry.get("delisted")
    ]
    eligible.sort(key=lambda entry: -float(entry.get("volume_24h_usd") or 0))
    skipped = len(eligible) - min(len(eligible), int(top))
    shortlist = eligible[: int(top)]

    fetch = fetch_bars or _fetch_hl_bars
    all_rows: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for entry in shortlist:
        symbol = str(entry["symbol"])
        frame = fetch(symbol, int(days))
        if frame is None or len(frame) < min_events * 4:
            candidates.append(
                {**entry, "scanned": False, "reason": "insufficient bars"}
            )
            continue
        result = scan_signals(
            frame,
            bar_seconds=_BAR_SECONDS,
            min_events=min_events,
            condition_regime=True,
            holdout_fraction=0.1,
        )
        rows = [
            {**row, "universe_symbol": symbol} for row in result.get("_all_rows") or []
        ]
        all_rows.extend(rows)
        candidates.append(
            {
                **entry,
                "scanned": True,
                "rows": len(rows),
                "current_regime": result.get("current_regime"),
            }
        )

    # ONE pooled BH family across the entire sweep — screening N symbols is
    # N times the tests, and the q-values must price that.
    apply_bh_verdicts(all_rows)
    for candidate in candidates:
        if not candidate.get("scanned"):
            continue
        symbol = candidate["symbol"]
        rows = [row for row in all_rows if row.get("universe_symbol") == symbol]
        interesting = [
            row for row in rows if row.get("verdict") in ("promote", "probation")
        ]
        interesting.sort(key=lambda row: -abs(float(row.get("t_stat_vs_drift") or 0)))
        candidate["promote"] = sum(
            1 for row in interesting if row["verdict"] == "promote"
        )
        candidate["probation"] = sum(
            1 for row in interesting if row["verdict"] == "probation"
        )
        candidate["best_rows"] = [
            {
                key: row.get(key)
                for key in (
                    "signal",
                    "timeframe",
                    "horizon",
                    "t_stat_vs_drift",
                    "q_value",
                    "n",
                    "verdict",
                    "regime",
                    "in_current_regime",
                )
            }
            for row in interesting[:_MAX_ROWS_PER_SYMBOL]
        ]

    artifact = {
        "generated_at": utc_now_iso(),
        "filters": {
            "top": int(top),
            "min_volume_usd": float(min_volume_usd),
            "days": int(days),
            "min_events": int(min_events),
            "excluded_current": sorted(current),
            "eligible_beyond_top": skipped,
        },
        "pooled_tests": len(all_rows),
        "candidates": candidates,
        "read": _READ,
    }
    store.write_json(job_id, UNIVERSE_SCAN_PATH, artifact)
    append_ledger_row(
        store,
        job_id,
        "candidates",
        {
            "family": "universe",
            "name": f"universe-scan-{utc_now_iso()[:10]}",
            "note": (
                f"screened {sum(1 for c in candidates if c.get('scanned'))} of "
                f"{len(shortlist)} shortlisted symbols ({len(all_rows)} pooled "
                f"tests); promote/probation rows per symbol in "
                f"{UNIVERSE_SCAN_PATH}"
            ),
        },
    )
    return artifact


def _fetch_hl_universe() -> list[dict[str, Any]]:
    import asyncio

    from wayfinder_paths.core.clients.HyperliquidInfoClient import (
        HyperliquidInfoClient,
    )

    async def _fetch() -> list[dict[str, Any]]:
        meta, ctxs = await HyperliquidInfoClient().post({"type": "metaAndAssetCtxs"})
        rows: list[dict[str, Any]] = []
        for asset, ctx in zip(meta.get("universe", []), ctxs, strict=False):
            rows.append(
                {
                    "symbol": str(asset.get("name")),
                    "volume_24h_usd": float(ctx.get("dayNtlVlm") or 0.0),
                    "funding_hourly": float(ctx.get("funding") or 0.0),
                    "open_interest": float(ctx.get("openInterest") or 0.0),
                    "max_leverage": asset.get("maxLeverage"),
                    "delisted": bool(asset.get("isDelisted")),
                }
            )
        return rows

    return asyncio.run(_fetch())


def _fetch_hl_bars(symbol: str, days: int) -> Any:
    import asyncio
    import datetime as dt

    import pandas as pd

    from wayfinder_paths.core.clients.HyperliquidInfoClient import (
        HyperliquidInfoClient,
    )

    end = dt.datetime.now(dt.UTC)
    start = end - dt.timedelta(days=days)

    async def _fetch() -> list[dict[str, Any]]:
        return await HyperliquidInfoClient().post(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol,
                    "interval": _BAR_INTERVAL,
                    "startTime": int(start.timestamp() * 1000),
                    "endTime": int(end.timestamp() * 1000),
                },
            }
        )

    candles = asyncio.run(_fetch()) or []
    rows = []
    for candle in candles:
        try:
            rows.append(
                {
                    "timestamp": pd.Timestamp(
                        int(candle["t"]), unit="ms", tz="UTC"
                    ).isoformat(),
                    "symbol": symbol,
                    "open": float(candle["o"]),
                    "high": float(candle["h"]),
                    "low": float(candle["l"]),
                    "close": float(candle["c"]),
                    "volume": float(candle.get("v") or 0.0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    if not rows:
        return None
    return pd.DataFrame(rows)


def load_universe_scan(store: JobStore, job_id: str) -> dict[str, Any] | None:
    doc = store.read_json(job_id, UNIVERSE_SCAN_PATH)
    return doc if isinstance(doc, dict) else None


__all__ = [
    "UNIVERSE_SCAN_PATH",
    "load_universe_scan",
    "universe_scan_job",
]
