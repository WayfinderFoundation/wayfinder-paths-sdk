"""Selected starters must trade identically with live-sized indicator history.

Synthetic prices exercise execution compatibility, not historical profitability.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from wayfinder_paths.jobs.execution.primitives import (
    bar_interval_seconds,
    resolve_compute_window,
)
from wayfinder_paths.jobs.execution.simulator import PreparedExecutionDataset
from wayfinder_paths.jobs.execution.validation import forward_parity_replay
from wayfinder_paths.jobs.starters import (
    STARTER_DEFINITIONS,
    StarterDefinition,
    create_starter_job,
    starter_lookback_bars,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.tests.test_jobs_starters import _synthetic_rows
from wayfinder_paths.tests.test_starter_retirement import skip_fetch


@pytest.mark.parametrize(
    "definition",
    [item for item in STARTER_DEFINITIONS if item.selectable],
    ids=lambda item: item.id,
)
def test_selected_starter_trades_with_live_window(
    definition: StarterDefinition, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "wayfinder_paths.jobs.starters._spawn_starter_dataset_fetch", skip_fetch
    )
    store = JobStore(repo_root=tmp_path)
    create_starter_job(definition.id, store=store, compile_job=False)
    job = store.load(definition.id)
    module = importlib.import_module(definition.module)
    strategy = module.build_strategy(job.execution_params)
    window = resolve_compute_window(job.execution_params, strategy)
    assert window.size == starter_lookback_bars(definition)
    assert window.live_depth > strategy.warmup_bars

    seconds = int(bar_interval_seconds(definition.timeframe))
    # Sparse hourly entry rules need enough synthetic opportunities to make
    # the comparison non-vacuous. Daily-rebalanced sleeves activate in a week.
    days = 30 if definition.timeframe == "1h" else 7
    dataset = PreparedExecutionDataset.from_rows(
        _synthetic_rows(
            definition.symbols,
            2 * window.live_depth + days * 86_400 // seconds,
            minutes=seconds // 60,
            seed=len(definition.id),
        )
    )
    report = forward_parity_replay(
        module.build_strategy,
        dataset,
        job.execution_spec,
        job.execution_params,
        days=days,
    )
    assert report["status"] == "passed", report
    assert report["bars_compared"] == days * 86_400 // seconds
    # An all-hold comparison cannot establish entry/exits or sizing parity.
    assert report["intents_compared"] > 0, (definition.id, report)
