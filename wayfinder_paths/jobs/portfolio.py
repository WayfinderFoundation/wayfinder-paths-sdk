"""Fleet-level portfolio awareness (P2-lite).

Every job optimizes its own book blind: two jobs long the same majors are
one bet wearing two names, and neither wake can see it. This module builds
one deterministic cross-job report from the forward books — pairwise daily
PnL correlation, shared-symbol overlap, gross notional by symbol — written
to ``.wayfinder/portfolio/report.json`` on the watchdog cadence. Each wake
reads its own slice (``portfolio`` context block) and the island scheduler
raises the diversification weight when a job's average correlation to the
rest of the fleet runs high.

Pure stdlib math on trade rows — no pandas, no venue calls; the report is a
function of on-disk state like everything else the improver reads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.store import JobStore

PORTFOLIO_REPORT_RELPATH = Path(".wayfinder") / "portfolio" / "report.json"
_MIN_SHARED_DAYS = 5
_RECENT_TRADES = 2000  # per job: bounds the scan on multi-MB books
HIGH_CORRELATION = 0.7


def portfolio_report_path(store: JobStore) -> Path:
    return store.repo_root / PORTFOLIO_REPORT_RELPATH


def build_portfolio_report(store: JobStore) -> dict[str, Any]:
    books: dict[str, dict[str, float]] = {}
    symbols: dict[str, dict[str, float]] = {}
    for job in store.list_jobs():
        daily, gross = _forward_book(store.job_dir(job.id))
        if daily:
            books[job.id] = daily
            symbols[job.id] = gross

    correlations: dict[str, dict[str, float]] = {job_id: {} for job_id in books}
    for a in books:
        for b in books:
            if a >= b:
                continue
            r = _pearson_on_shared_days(books[a], books[b])
            if r is not None:
                correlations[a][b] = r
                correlations[b][a] = r

    shared_symbols: dict[str, list[str]] = {}
    for job_id, gross in symbols.items():
        for symbol in gross:
            shared_symbols.setdefault(symbol, []).append(job_id)
    shared_symbols = {
        symbol: sorted(job_ids)
        for symbol, job_ids in shared_symbols.items()
        if len(job_ids) > 1
    }

    jobs_block = {}
    for job_id in books:
        pairwise = correlations[job_id]
        jobs_block[job_id] = {
            "avg_correlation": (
                round(sum(pairwise.values()) / len(pairwise), 4) if pairwise else None
            ),
            "correlations": {k: round(v, 4) for k, v in sorted(pairwise.items())},
            "gross_notional_by_symbol": {
                k: round(v, 2) for k, v in sorted(symbols[job_id].items())
            },
            "daily_pnl_days": len(books[job_id]),
        }

    return {
        "jobs": jobs_block,
        "shared_symbols": shared_symbols,
        "min_shared_days": _MIN_SHARED_DAYS,
        "generated_at": utc_now_iso(),
    }


def write_portfolio_report(store: JobStore) -> Path:
    report = build_portfolio_report(store)
    path = portfolio_report_path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def portfolio_block(store: JobStore, job_id: str) -> dict[str, Any] | None:
    """This job's slice of the fleet report for the wake context. None when
    no report exists or the job has no forward book yet. Never raises."""
    try:
        path = portfolio_report_path(store)
        if not path.exists():
            return None
        report = json.loads(path.read_text(encoding="utf-8"))
        me = (report.get("jobs") or {}).get(job_id)
        if not me:
            return None
        shared = {
            symbol: [other for other in job_ids if other != job_id]
            for symbol, job_ids in (report.get("shared_symbols") or {}).items()
            if job_id in job_ids
        }
        avg = me.get("avg_correlation")
        block: dict[str, Any] = {
            "avg_correlation_to_fleet": avg,
            "correlations": me.get("correlations") or {},
            "shared_symbols": shared,
            "generated_at": report.get("generated_at"),
            "_basis": (
                "Fleet view over every job's forward book (daily net_pnl "
                "correlation + symbol overlap), computed mechanically on the "
                "watchdog cadence. Cite it when choosing candidates."
            ),
        }
        if avg is not None and float(avg) > HIGH_CORRELATION:
            block["diversification_directive"] = (
                f"Average correlation to the rest of the fleet is {avg} — your "
                "candidates are largely redundant with other jobs' books. The "
                "diversification island weight is raised; prefer candidates "
                "whose behavior descriptors and symbols diverge from the "
                "shared exposure above."
            )
        return block
    except Exception:  # noqa: BLE001 — telemetry never breaks a wake
        return None


def _forward_book(root: Path) -> tuple[dict[str, float], dict[str, float]]:
    """(daily net_pnl by close date, gross notional by symbol) from the
    forward trade book; empty dicts when there is no book."""
    trades_path = root / "results" / "forward" / "trades.jsonl"
    if not trades_path.exists():
        return {}, {}
    daily: dict[str, float] = {}
    gross: dict[str, float] = {}
    lines = trades_path.read_text(encoding="utf-8").splitlines()[-_RECENT_TRADES:]
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        day = str(row.get("closed_at") or "")[:10]
        if not day:
            continue
        try:
            pnl = float(row.get("net_pnl") or 0.0)
            notional = abs(float(row.get("size") or 0.0)) * float(
                row.get("price") or 0.0
            )
        except (TypeError, ValueError):
            continue
        daily[day] = daily.get(day, 0.0) + pnl
        symbol = str(row.get("symbol") or "?")
        gross[symbol] = gross.get(symbol, 0.0) + notional
    return daily, gross


def _pearson_on_shared_days(a: dict[str, float], b: dict[str, float]) -> float | None:
    days = sorted(set(a) & set(b))
    if len(days) < _MIN_SHARED_DAYS:
        return None
    xs = [a[day] for day in days]
    ys = [b[day] for day in days]
    n = len(days)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=False))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return cov / (var_x**0.5 * var_y**0.5)
