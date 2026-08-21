"""Mechanical research-coverage certificates and exhaustion verdicts.

The certificate is reconstructed from controller-owned ledgers. Agent prose is
not evidence: an absent, blocked, invalid, or underpowered cell remains a gap
and can never be counted as a negative result.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from itertools import product
from statistics import median
from typing import Any

from wayfinder_paths.jobs.execution.experiments import experiment_semantic_hash
from wayfinder_paths.jobs.models import utc_now_iso
from wayfinder_paths.jobs.research import read_scan_ledger
from wayfinder_paths.jobs.store import JobStore

CELL_OUTCOMES = {"negative", "positive", "near_miss"}
CRITICAL_CELL_STATUSES = {
    "blocked_infrastructure",
    "invalid_harness",
    "underpowered",
    "not_run",
}
CELL_COUNT_KEYS = (
    "completed_valid",
    "negative",
    "positive",
    "near_miss",
    "blocked_infrastructure",
    "invalid_harness",
    "underpowered",
    "not_run",
)
_CANDIDATE_VERDICTS = {"probation", "promote"}
_PROBATION_ENTRY_EVENTS = {
    "paper_probation_opened",
    "probation_leg_opened",
    "paper_probation_entry_refused",
}
_SCAN_LEDGER_PATH = "results/research/signal_scan/ledger.jsonl"
_EXPERIMENTS_PATH = "results/backtest/experiments.jsonl"
_LEGACY_SCAN_WRITE_WINDOW_SECONDS = 600


def _cell_key(row: Mapping[str, Any]) -> tuple[str, str, str, int, str]:
    return (
        str(row.get("symbol") or ""),
        str(row.get("signal") or ""),
        str(row.get("timeframe") or ""),
        int(row.get("horizon") or 0),
        str(row.get("regime") or "base"),
    )


def _coordinate(key: tuple[str, str, str, int, str]) -> dict[str, Any]:
    symbol, signal, timeframe, horizon, regime = key
    return {
        "symbol": symbol,
        "signal": signal,
        "timeframe": timeframe,
        "horizon": horizon,
        "regime": regime,
    }


def _cell_id(key: tuple[str, str, str, int, str]) -> str:
    return ":".join(str(part) for part in key)


def _timestamp(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def _at_or_after(value: Any, baseline: Any) -> bool:
    baseline_ts = _timestamp(baseline)
    value_ts = _timestamp(value)
    return baseline_ts is None or (value_ts is not None and value_ts >= baseline_ts)


def _rows_for_scan(
    rows: list[dict[str, Any]], meta: dict[str, Any]
) -> list[dict[str, Any]]:
    scan_id = str(meta.get("scan_id") or "")
    if scan_id:
        exact = [
            row
            for row in rows
            if row.get("kind") in {"scan_test", "scan_cell"}
            and str(row.get("scan_id") or "") == scan_id
        ]
        if exact:
            return exact

    # Legacy rows predate scan_id on cells. Prefer the contiguous rows after
    # the declaration; compacted ledgers fall back to coordinate filtering.
    meta_index = next(
        (index for index, row in enumerate(rows) if row is meta),
        None,
    )
    if meta_index is not None:
        contiguous: list[dict[str, Any]] = []
        meta_ts = _timestamp(meta.get("ts") or meta.get("scan_id"))
        for row in rows[meta_index + 1 :]:
            if row.get("kind") == "scan_meta":
                break
            if row.get("kind") in {"scan_test", "scan_cell"}:
                if scan_id:
                    # PR-3 cells carry scan_id. Untagged PR-2 cells are
                    # accepted only when they were written beside this meta;
                    # after ledger compaction, unrelated latest cells sit at
                    # the tail and must not be attributed to an old scan.
                    row_ts = _timestamp(row.get("ts"))
                    write_delay = (
                        (row_ts - meta_ts).total_seconds()
                        if meta_ts is not None and row_ts is not None
                        else None
                    )
                    if (
                        row.get("scan_id")
                        or write_delay is None
                        or not 0 <= write_delay <= _LEGACY_SCAN_WRITE_WINDOW_SECONDS
                    ):
                        continue
                contiguous.append(row)
        if contiguous:
            return contiguous
    if scan_id:
        return []

    symbols = {str(value) for value in meta.get("symbols") or []}
    timeframes = {str(value) for value in meta.get("timeframes") or []}
    filtered = [
        row
        for row in rows
        if row.get("kind") in {"scan_test", "scan_cell"}
        and (not symbols or str(row.get("symbol") or "") in symbols)
        and (not timeframes or str(row.get("timeframe") or "") in timeframes)
    ]
    latest: dict[tuple[str, str, str, int, str], dict[str, Any]] = {}
    for row in filtered:
        latest[_cell_key(row)] = row
    return list(latest.values())


def _selected_scan(
    rows: list[dict[str, Any]], lane: str, refs: list[str]
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    metas = [row for row in rows if row.get("kind") == "scan_meta"]
    if not metas:
        return None, []
    ref_text = " ".join(refs)
    matches = [
        row
        for row in metas
        if str(row.get("campaign") or "").strip().casefold() == lane.strip().casefold()
        or (row.get("scan_id") and str(row["scan_id"]) in ref_text)
    ]
    meta = (matches or metas)[-1]
    return meta, _rows_for_scan(rows, meta)


def _declared_keys(
    meta: Mapping[str, Any] | None, observed: list[dict[str, Any]]
) -> set[tuple[str, str, str, int, str]]:
    if meta is None:
        return {_cell_key(row) for row in observed}
    symbols = [str(value) for value in meta.get("symbols") or []]
    signals = [str(value) for value in meta.get("declared_signals") or []]
    if not signals:
        signals = sorted(
            {str(row.get("signal") or "") for row in observed if row.get("signal")}
        )
    timeframes = [str(value) for value in meta.get("timeframes") or []]
    regimes = [str(value) for value in meta.get("declared_regimes") or ["base"]]
    horizons_by_symbol = meta.get("horizons") or {}
    declared: set[tuple[str, str, str, int, str]] = set()
    for symbol in symbols:
        raw_by_tf = (
            horizons_by_symbol.get(symbol, {})
            if isinstance(horizons_by_symbol, Mapping)
            else {}
        )
        for timeframe in timeframes:
            raw_horizons = (
                raw_by_tf.get(timeframe, []) if isinstance(raw_by_tf, Mapping) else []
            )
            for horizon in raw_horizons:
                for signal in signals:
                    for regime in regimes:
                        declared.add((symbol, signal, timeframe, int(horizon), regime))
    return declared or {_cell_key(row) for row in observed}


def _minimum_detectable_edge(row: Mapping[str, Any]) -> float | None:
    value = row.get("min_detectable_edge_bps")
    if isinstance(value, (int, float)):
        return float(value)
    # Legacy PR-2 rows have enough information to recover SEM:
    # abs(t_gross) - t_net = round_trip_cost / SEM.
    try:
        gross_t = abs(float(row.get("t") or row.get("t_stat_vs_drift") or 0.0))
        t_net = float(row["t_net"])
        cost_bps = float(row["round_trip_cost_bps"])
    except (KeyError, TypeError, ValueError):
        return None
    denominator = gross_t - t_net
    if denominator <= 0 or cost_bps <= 0:
        return None
    return 2.0 * (cost_bps / denominator)


def _classify_cell(row: Mapping[str, Any]) -> str:
    explicit = str(row.get("status") or "")
    if explicit in CRITICAL_CELL_STATUSES:
        return explicit
    verdict = str(row.get("verdict") or "")
    if verdict == "promote":
        return "positive"
    if verdict in {"probation", "candidate"}:
        return "near_miss"
    return "negative"


def _coverage_gap_rows(
    journal: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resolved = {
        str(row.get("gap_id"))
        for row in journal
        if row.get("type") == "research_coverage_gap_resolved" and row.get("gap_id")
    }
    return [
        row
        for row in journal
        if row.get("type") == "research_coverage_gap"
        and str(row.get("gap_id") or "") not in resolved
        and str(row.get("status") or "") in CRITICAL_CELL_STATUSES
    ]


def _requirement_satisfied(
    requirement: Mapping[str, Any],
    *,
    note_ts: str,
    scan_rows: list[dict[str, Any]],
    holdouts: list[dict[str, Any]],
    journal: list[dict[str, Any]],
) -> bool:
    kind = str(requirement.get("kind") or "")
    if kind == "signal_scan":
        symbols = {str(value) for value in requirement.get("symbols") or []}
        timeframes = {str(value) for value in requirement.get("timeframes") or []}
        signals = {str(value) for value in requirement.get("signals") or []}
        metas = [row for row in scan_rows if row.get("kind") == "scan_meta"]
        for meta in metas:
            if not _at_or_after(meta.get("ts") or meta.get("scan_id"), note_ts):
                continue
            if not symbols.issubset({str(v) for v in meta.get("symbols") or []}):
                continue
            if not timeframes.issubset({str(v) for v in meta.get("timeframes") or []}):
                continue
            measured = [
                row
                for row in _rows_for_scan(scan_rows, meta)
                if row.get("kind") == "scan_test"
            ]
            available_signals = {
                str(value) for value in meta.get("declared_signals") or []
            } | {str(row.get("signal")) for row in measured if row.get("signal")}
            if signals and not signals.issubset(available_signals):
                continue
            required_symbols = symbols or {
                str(value) for value in meta.get("symbols") or []
            }
            required_timeframes = timeframes or {
                str(value) for value in meta.get("timeframes") or []
            }
            required_signals: set[str | None] = set(signals)
            if not required_signals:
                required_signals.add(None)
            if (
                measured
                and required_symbols
                and required_timeframes
                and all(
                    any(
                        row.get("symbol") == symbol
                        and row.get("timeframe") == timeframe
                        and (signal is None or row.get("signal") == signal)
                        for row in measured
                    )
                    for symbol, timeframe, signal in product(
                        required_symbols, required_timeframes, required_signals
                    )
                )
            ):
                return True
    if kind == "holdout_check":
        return any(
            _at_or_after(row.get("ts"), note_ts)
            and row.get("verdict") in {"confirmed", "failed"}
            and (
                not requirement.get("symbol")
                or row.get("symbol") == requirement.get("symbol")
            )
            and (
                not requirement.get("signal")
                or row.get("signal") == requirement.get("signal")
            )
            and (
                not requirement.get("timeframe")
                or row.get("timeframe") == requirement.get("timeframe")
            )
            and (
                not requirement.get("horizon")
                or int(row.get("horizon") or 0) == int(requirement.get("horizon") or 0)
            )
            for row in holdouts
        )
    if kind == "paper_probation":
        return any(
            row.get("type") in _PROBATION_ENTRY_EVENTS
            and _at_or_after(row.get("ts"), note_ts)
            and (
                not requirement.get("symbol")
                or row.get("symbol") == requirement.get("symbol")
            )
            and (
                not requirement.get("signal")
                or row.get("signal") == requirement.get("signal")
            )
            for row in journal
        )
    if kind == "rank_check":
        required_horizons = {int(value) for value in requirement.get("horizons") or []}
        return any(
            row.get("type") == "rank_check_completed"
            and _at_or_after(row.get("ts"), note_ts)
            and (
                not requirement.get("column")
                or row.get("column") == requirement.get("column")
            )
            and required_horizons.issubset(
                {int(value) for value in row.get("horizons") or []}
            )
            and row.get("artifact")
            for row in journal
        )
    return False


def _required_experiments(
    journal: list[dict[str, Any]], scan_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    holdouts = [row for row in scan_rows if row.get("kind") == "holdout_check"]
    requirements: dict[str, dict[str, Any]] = {}
    for note in journal:
        if note.get("type") != "operator_note":
            continue
        note_ts = str(note.get("ts") or "")
        for raw in note.get("required_experiments") or []:
            if not isinstance(raw, Mapping):
                continue
            requirement = dict(raw)
            requirement_id = str(requirement.get("id") or "").strip()
            if not requirement_id:
                continue
            requirement["id"] = requirement_id
            requirement["declared_at"] = note_ts
            requirement["satisfied"] = _requirement_satisfied(
                requirement,
                note_ts=note_ts,
                scan_rows=scan_rows,
                holdouts=holdouts,
                journal=journal,
            )
            if requirement["satisfied"]:
                requirement["status"] = "completed"
            else:
                status = str(requirement.get("status") or "not_run")
                requirement["status"] = (
                    status if status in CRITICAL_CELL_STATUSES else "not_run"
                )
            requirements[requirement_id] = requirement
    return list(requirements.values())


def _candidate_followups(
    cells: list[dict[str, Any]],
    holdouts: list[dict[str, Any]],
    journal: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates = [
        cell for cell in cells if str(cell.get("verdict") or "") in _CANDIDATE_VERDICTS
    ]
    candidate_symbols = [str(cell.get("symbol") or "") for cell in candidates]
    followups: list[dict[str, Any]] = []
    for cell in candidates:
        matches = [
            row
            for row in holdouts
            if row.get("symbol") == cell.get("symbol")
            and row.get("signal") == cell.get("signal")
            and row.get("timeframe") == cell.get("timeframe")
            and int(row.get("horizon") or 0) == int(cell.get("horizon") or 0)
            and _at_or_after(row.get("ts"), cell.get("ts"))
        ]
        latest = matches[-1] if matches else None
        holdout_verdict = str((latest or {}).get("verdict") or "")
        entry_event = None
        symbol = str(cell.get("symbol") or "")
        for event in journal:
            if event.get("type") not in _PROBATION_ENTRY_EVENTS or not _at_or_after(
                event.get("ts"), cell.get("ts")
            ):
                continue
            exact = all(
                not event.get(key) or event.get(key) == cell.get(key)
                for key in ("symbol", "signal", "timeframe", "horizon")
            )
            # Legacy paper-probation events recorded only symbol + leg. A
            # symbol-only event is unambiguous only when this scan has one
            # candidate for that symbol.
            symbol_only = (
                event.get("symbol") == symbol
                and not any(
                    event.get(key) for key in ("signal", "timeframe", "horizon")
                )
                and candidate_symbols.count(symbol) == 1
            )
            if event.get("type") == "paper_probation_entry_refused":
                # A refusal may refute a scan candidate only when it names the
                # exact coordinate and carries a durable evidence reference.
                # The entry API also accepts explicit comparison numbers, so
                # treating a legacy symbol-only refusal as candidate evidence
                # would let one arbitrary call erase every lead for a symbol.
                refusal_matches = all(
                    event.get(key) == cell.get(key)
                    for key in ("symbol", "signal", "timeframe", "horizon")
                ) and bool(event.get("proposal_id") or event.get("artifact"))
                if refusal_matches:
                    entry_event = event
            elif exact and (event.get("signal") or symbol_only):
                entry_event = event
        lifecycle_outcome = None
        if entry_event is not None:
            outcomes = [
                event
                for event in journal
                if event.get("type")
                in {"probation_leg_graduated", "probation_leg_killed"}
                and event.get("leg") == entry_event.get("leg")
                and _at_or_after(event.get("ts"), entry_event.get("ts"))
            ]
            lifecycle_outcome = outcomes[-1].get("type") if outcomes else None

        refused = (entry_event or {}).get("type") == "paper_probation_entry_refused"
        # A matured probation verdict is newer deployment evidence and
        # outranks the holdout that admitted the candidate. Otherwise a
        # holdout-confirmed leg that is later killed remains unresolved
        # forever. A mechanical entry refusal is likewise completed negative
        # evidence: no leg was deployed, but the candidate was not parked.
        if lifecycle_outcome == "probation_leg_graduated":
            state = "confirmed_open"
        elif lifecycle_outcome == "probation_leg_killed" or refused:
            state = "refuted"
        elif holdout_verdict == "confirmed":
            state = "confirmed_open"
        elif holdout_verdict == "failed":
            state = "refuted"
        elif entry_event is not None:
            state = "probation_active"
        elif latest is None:
            state = "parked"
        else:
            state = "holdout_underpowered"
        followups.append(
            {
                **{
                    key: cell.get(key)
                    for key in ("symbol", "signal", "timeframe", "horizon", "regime")
                },
                "scan_verdict": cell.get("verdict"),
                "state": state,
                "holdout_verdict": holdout_verdict or None,
                "probation_leg": (entry_event or {}).get("leg"),
            }
        )
    return followups


def build_coverage_certificate(
    job_id: str,
    lane: str,
    *,
    store: JobStore | None = None,
    refs: list[str] | None = None,
) -> dict[str, Any]:
    """Build a structured coverage certificate for one claimed lane."""
    store = store or JobStore()
    root = store.job_dir(job_id)
    scan_rows = read_scan_ledger(root)
    journal = store.read_jsonl(job_id, "journal.jsonl")
    experiments = store.read_jsonl(job_id, _EXPERIMENTS_PATH)
    meta, observed = _selected_scan(scan_rows, lane, refs or [])
    declared = _declared_keys(meta, observed)

    latest_cells: dict[tuple[str, str, str, int, str], dict[str, Any]] = {}
    for row in observed:
        latest_cells[_cell_key(row)] = row
    for gap in _coverage_gap_rows(journal):
        if all(gap.get(key) is not None for key in ("symbol", "signal", "timeframe")):
            latest_cells[_cell_key(gap)] = gap

    cells: list[dict[str, Any]] = []
    for key in sorted(declared | set(latest_cells)):
        cell_row = latest_cells.get(key)
        if cell_row is None:
            cells.append(
                {
                    **_coordinate(key),
                    "status": "not_run",
                    "reason": "declared_cell_not_recorded",
                }
            )
            continue
        status = _classify_cell(cell_row)
        cells.append(
            {
                **_coordinate(key),
                "status": status,
                "reason": cell_row.get("reason"),
                "ts": cell_row.get("ts"),
                "family": cell_row.get("family"),
                "library": cell_row.get("library"),
                "verdict": cell_row.get("verdict"),
                "n": cell_row.get("n"),
                "t": cell_row.get("t") or cell_row.get("t_stat_vs_drift"),
                "t_net": cell_row.get("t_net"),
                "edge_net_bps": cell_row.get("edge_net_bps"),
                "round_trip_cost_bps": cell_row.get("round_trip_cost_bps"),
                "min_detectable_edge_bps": _minimum_detectable_edge(cell_row),
            }
        )
    for control in (meta or {}).get("incumbent_controls") or []:
        key = _cell_key({**control, "regime": "base"})
        if any(_cell_key(cell) == key for cell in cells):
            continue
        cells.append(
            {
                **_coordinate(key),
                "status": "not_run",
                "reason": control.get("reason") or "incumbent_control_not_measured",
                "incumbent_control": True,
            }
        )

    counts = dict.fromkeys(CELL_COUNT_KEYS, 0)
    for cell in cells:
        status = str(cell["status"])
        counts[status] += 1
        if status in CELL_OUTCOMES:
            counts["completed_valid"] += 1

    selected_trials = {
        _cell_key(cell)[:4]
        for cell in cells
        if cell.get("symbol") and cell.get("signal") and cell.get("timeframe")
    }
    holdouts = [
        row
        for row in scan_rows
        if row.get("kind") == "holdout_check" and _cell_key(row)[:4] in selected_trials
    ]
    candidate_followups = _candidate_followups(cells, holdouts, journal)
    parked = [row for row in candidate_followups if row["state"] == "parked"]
    unresolved = [
        row
        for row in candidate_followups
        if row["state"]
        in {"parked", "confirmed_open", "holdout_underpowered", "probation_active"}
    ]
    negative_followups = sum(row["state"] == "refuted" for row in candidate_followups)

    seen_experiments: set[str] = set()
    duplicate_experiments = 0
    invalid_experiments = 0
    for experiment in experiments:
        semantic_hash = experiment_semantic_hash(experiment)
        if semantic_hash in seen_experiments:
            duplicate_experiments += 1
        else:
            seen_experiments.add(semantic_hash)
        if int(experiment.get("invalid_count") or 0) > 0:
            invalid_experiments += 1

    requirements = _required_experiments(journal, scan_rows)
    incumbent_controls: list[dict[str, Any]] = []
    for control in (meta or {}).get("incumbent_controls") or []:
        key = _cell_key({**control, "regime": "base"})
        incumbent_cell = next((item for item in cells if _cell_key(item) == key), None)
        incumbent_controls.append(
            {
                **dict(control),
                "status": (
                    "not_run"
                    if incumbent_cell is None
                    or incumbent_cell.get("status") in CRITICAL_CELL_STATUSES
                    else "pass"
                    if incumbent_cell.get("verdict") == "promote"
                    else "fail"
                ),
                "result": incumbent_cell,
            }
        )

    detectable = [
        float(cell["min_detectable_edge_bps"])
        for cell in cells
        if isinstance(cell.get("min_detectable_edge_bps"), (int, float))
    ]
    cost_values = [
        float(cell["round_trip_cost_bps"])
        for cell in cells
        if isinstance(cell.get("round_trip_cost_bps"), (int, float))
    ]
    signal_families = dict((meta or {}).get("signal_families") or {})
    if not signal_families:
        signal_families = {
            str(cell.get("signal")): str(cell.get("family") or cell.get("library"))
            for cell in cells
            if cell.get("signal")
        }
    completed_cells = [
        {
            key: cell.get(key)
            for key in ("symbol", "signal", "timeframe", "horizon", "regime")
        }
        for cell in cells
        if cell["status"] in CELL_OUTCOMES
    ]
    return {
        "certificate_version": 1,
        "job_id": job_id,
        "lane": lane,
        "generated_at": utc_now_iso(),
        "lane_definition": {
            "source": "latest_matching_scan" if meta else "no_scan_declaration",
            "scan_id": (meta or {}).get("scan_id"),
            "assets": list((meta or {}).get("symbols") or []),
            "timeframes": list((meta or {}).get("timeframes") or []),
            "signals": list((meta or {}).get("declared_signals") or signal_families),
            "feature_families": sorted(set(signal_families.values())),
            "data_revision": (meta or {}).get("data_revision")
            or {"cutoff_ts": (meta or {}).get("cutoff_ts")},
            "cost_revision": (meta or {}).get("cost_revision"),
            "workspace_signals_sha": (meta or {}).get("workspace_signals_sha"),
        },
        "cell_counts": counts,
        "cells": cells,
        "completed_scope": completed_cells,
        "negative_evidence_count": counts["negative"] + negative_followups,
        "incumbent_controls": {
            "declared": len(incumbent_controls),
            "passing": sum(row["status"] == "pass" for row in incumbent_controls),
            "cells": incumbent_controls,
        },
        "detectable_edge": {
            "minimum_bps": min(detectable) if detectable else None,
            "median_bps": median(detectable) if detectable else None,
            "maximum_bps": max(detectable) if detectable else None,
            "round_trip_cost_bps": sorted(set(cost_values)),
            "cells_where_cost_exceeds_detectable_edge": sum(
                isinstance(cell.get("round_trip_cost_bps"), (int, float))
                and isinstance(cell.get("min_detectable_edge_bps"), (int, float))
                and float(cell["round_trip_cost_bps"])
                >= float(cell["min_detectable_edge_bps"])
                for cell in cells
            ),
        },
        "multiplicity": {
            "family_size": (meta or {}).get("bh_family_size"),
            "minimum_family_size": (meta or {}).get("bh_min_family_size"),
            "method": (meta or {}).get("multiplicity_method"),
            "family_mode": (meta or {}).get("bh_family_mode"),
        },
        "holdout_spends": {
            "total": len(holdouts),
            "unique": len({str(row.get("hash") or "") for row in holdouts}),
            "repeats": len(holdouts)
            - len({str(row.get("hash") or "") for row in holdouts}),
            "rows": holdouts,
        },
        "candidate_followups": candidate_followups,
        "parked_candidates": parked,
        "unresolved_candidates": unresolved,
        "required_experiments": requirements,
        "experiments": {
            "total": len(experiments),
            "semantic_unique": len(seen_experiments),
            "duplicates": duplicate_experiments,
            "infra_invalid": invalid_experiments,
        },
        "sources": {
            "scan_ledger": _SCAN_LEDGER_PATH,
            "experiments": _EXPERIMENTS_PATH,
            "journal": "journal.jsonl",
        },
    }


def audit_exhaustion_claim(
    store: JobStore, job_id: str, claim: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a deterministic pass/narrow/reject verdict for a claim."""
    certificate = build_coverage_certificate(
        job_id,
        str(claim.get("lane") or ""),
        store=store,
        refs=[str(value) for value in claim.get("refs") or []],
    )
    cells = certificate["cells"]
    critical_cells = [
        cell for cell in cells if cell.get("status") in CRITICAL_CELL_STATUSES
    ]
    unmet_requirements = [
        row for row in certificate["required_experiments"] if not row["satisfied"]
    ]
    unresolved_candidates = certificate["unresolved_candidates"]
    mandated: list[dict[str, Any]] = []
    for candidate in unresolved_candidates:
        candidate_id = _cell_id(_cell_key(candidate))
        mandated.append(
            {
                "id": f"candidate-followup:{candidate_id}",
                "kind": "candidate_followup",
                **candidate,
            }
        )
    for cell in critical_cells:
        cell_id = _cell_id(_cell_key(cell))
        mandated.append(
            {
                "id": f"coverage-cell:{cell_id}",
                "kind": "signal_scan_cell",
                **{
                    key: cell.get(key)
                    for key in (
                        "symbol",
                        "signal",
                        "timeframe",
                        "horizon",
                        "regime",
                        "status",
                        "reason",
                    )
                },
            }
        )
    mandated.extend(dict(row) for row in unmet_requirements)

    reasons: list[str] = []
    if claim.get("provenance") == "agent-self-rejected":
        reasons.append("agent_self_rejection_cannot_settle")
    if unresolved_candidates:
        reasons.append("probation_grade_candidates_unresolved")
    if not certificate["cell_counts"]["completed_valid"]:
        reasons.append("no_completed_valid_cells")
    if not certificate["negative_evidence_count"]:
        reasons.append("no_valid_negative_evidence")
    if critical_cells or unmet_requirements:
        reasons.append("critical_coverage_gaps")

    if (
        claim.get("provenance") == "agent-self-rejected"
        or unresolved_candidates
        or not certificate["cell_counts"]["completed_valid"]
        or not certificate["negative_evidence_count"]
    ):
        verdict = "reject"
    elif critical_cells or unmet_requirements:
        verdict = "narrow"
    else:
        verdict = "pass"
    return {
        "verdict": verdict,
        "reason_codes": reasons,
        "audited_scope": {
            "lane": claim.get("lane"),
            "cells": certificate["completed_scope"],
            "scope_reduced": verdict == "narrow",
        },
        "required_next_experiments": mandated,
        "certificate": certificate,
    }
