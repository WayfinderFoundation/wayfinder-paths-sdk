from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest

from wayfinder_paths.jobs.research import (
    library_signal_on_bars,
    library_signal_warmup_bars,
    scan_signals,
)
from wayfinder_paths.jobs.signal_library import (
    SIGNAL_DSL,
    compile_signal_expression,
    signal_defs,
)
from wayfinder_paths.jobs.signal_population import (
    POPULATION_LIMIT_CAP,
    population_defs,
)
from wayfinder_paths.jobs.workspace_signals import validate_workspace_signals
from wayfinder_paths.tests.test_signal_library import _bars, _wavy_closes

_NAME_RE = re.compile(r"^[a-z0-9_]{1,48}$")


def test_population_is_deterministic_bounded_and_disjoint_from_the_library() -> None:
    defs = population_defs(limit=300)
    assert [spec.name for spec in defs] == [spec.name for spec in population_defs()]
    assert 200 <= len(defs) <= 300
    assert len(population_defs(limit=50)) == 50
    assert len(population_defs(limit=10_000)) <= POPULATION_LIMIT_CAP
    names = [spec.name for spec in defs]
    assert len(set(names)) == len(names)
    assert all(_NAME_RE.match(name) for name in names), [
        name for name in names if not _NAME_RE.match(name)
    ]
    assert not set(names) & set(signal_defs())
    assert all(spec.family == "population" and spec.expression for spec in defs)
    # Parameter variants lead; the seed reorders only the composed pairs.
    singles = [name for name in names if "_and_" not in name]
    assert names[: len(singles)] == singles
    assert [spec.name for spec in population_defs(seed=1)] != names
    assert [
        spec.name for spec in population_defs(seed=1) if "_and_" not in spec.name
    ] == singles


def test_population_defs_are_causal_boolean_and_round_trip_their_expression() -> None:
    probe = _bars(_wavy_closes(400))
    defs = population_defs(limit=300)
    for start in range(0, len(defs), 12):
        validate_workspace_signals(defs[start : start + 12], probe)
    for spec in defs[::5]:
        rebuilt = compile_signal_expression(
            name=spec.name,
            family=spec.family,
            description=spec.description,
            min_bars=spec.min_bars,
            expression=str(spec.expression),
        )
        pd.testing.assert_series_equal(
            spec.build(probe).fillna(False).astype(bool),
            rebuilt.build(probe).fillna(False).astype(bool),
        )
    # The DSL is what a worker pastes: the same expression through eval over
    # SIGNAL_DSL is the same column.
    spec = next(s for s in defs if s.name == "pop_rsi14_le_25")
    pasted = eval(f"lambda f: ({spec.expression})", {"__builtins__": {}, **SIGNAL_DSL})
    assert (
        pasted(probe)
        .fillna(False)
        .astype(bool)
        .equals(spec.build(probe).fillna(False).astype(bool))
    )


def test_population_defs_resolve_by_object_in_the_library_helpers() -> None:
    bars = _bars(_wavy_closes(300))
    spec = next(s for s in population_defs() if s.name == "pop_new_low_8")
    with pytest.raises(ValueError, match="unknown library signal"):
        library_signal_on_bars(bars, "pop_new_low_8", "1h", bar_seconds=3600)
    column = library_signal_on_bars(bars, spec, "1h", bar_seconds=3600)
    assert column.dtype == bool and column.any()
    assert library_signal_warmup_bars(spec, "4h", bar_seconds=3600) == (8 + 2 + 2) * 4


def test_population_joins_the_scan_family_and_finds_a_narrower_window() -> None:
    # Twelve-bar cycles: a dip that is a 3-bar low but not a 5-bar low is
    # followed by a bounce; the cycle's 5-bar low is followed by nothing.
    # The population's 3-bar variant sees the bounce on half its events; the
    # library's 5-bar low never does (its forward returns have no variance,
    # so the scan cannot even measure it).
    cycle = [
        100.0,
        102.0,
        104.0,
        103.0,
        101.5,
        104.0,
        104.0,
        104.0,
        104.0,
        104.0,
        99.0,
        99.0,
    ]
    bars = _bars(cycle * 75)
    narrow = next(s for s in population_defs() if s.name == "pop_new_low_3")
    result = scan_signals(
        bars,
        [1],
        bar_seconds=3600,
        timeframes=["1h"],
        holdout_fraction=0.0,
        min_events=10,
        extra_signals=[narrow],
        include_canonical=True,
    )
    rows = {row["signal"]: row for row in result["_all_rows"]}
    assert rows["pop_new_low_3"]["library"] == "population"
    assert rows["pop_new_low_3"]["n"] == 150
    assert rows["pop_new_low_3"]["t_stat_vs_drift"] > 5
    library_t = rows["new_low_5"]["t_stat_vs_drift"] if "new_low_5" in rows else 0.0
    assert abs(library_t) < 2
    # One family: the population row pays the same multiple-testing bill.
    assert result["bh_family"]["size"] == len(result["_all_rows"])
    assert np.isfinite(rows["pop_new_low_3"]["q_value"])


def test_signal_expressions_are_confined_to_the_dsl() -> None:
    from wayfinder_paths.jobs.signal_library import validate_signal_expression

    def rejected(expression: str, fragment: str) -> None:
        with pytest.raises(ValueError, match=fragment):
            compile_signal_expression(
                name="ws_probe",
                family="test",
                description="",
                min_bars=2,
                expression=expression,
            )

    # No modules, no attribute traversal, no dunders, no imports, no
    # callables that take code, no subscripts beyond a column of f.
    rejected("pd.io.common.os.getcwd() == ''", "not allowed|unknown name 'pd'")
    rejected("np.log(close(f)) > 0", "not allowed|unknown name 'np'")
    rejected("pd", "unknown name 'pd'")
    rejected("close(f).__class__", "attribute '__class__' is not allowed")
    rejected("close(f).apply(print) > 0", "attribute 'apply' is not allowed")
    rejected("__import__('os')", "unknown name '__import__'")
    rejected("(lambda: 1)()", "Lambda is not allowed")
    rejected("[x for x in close(f)]", "ListComp is not allowed")
    rejected("close(f)[0] > 0", "subscripts are limited")
    rejected("f.close > 0", "attribute 'close' is not allowed")
    rejected("close(f) >", "does not parse")
    rejected("rsi_extreme(f, **{'level': 30})", "keyword expansion")
    rejected("close(f) > 1\nclose(f) > 2", "one non-empty line")
    # The DSL itself is wide: helpers, arithmetic, store columns, methods.
    accepted = [
        "rsi_extreme(f, 25, -1) & (f['macro_regime'] == -1.0)",
        "fresh(bb_extreme(f, -2.0)) | new_extreme(f, 8, -1).shift(1, fill_value=False)",
        "close(f) < sma(close(f), 20) - 3 * atr(f, 14)",
        "log(close(f) / close(f).shift(24)) < -0.05",
        "where(sign(close(f).diff()) > 0, True, False) & weekend(f)",
        "abs(close(f).pct_change()) > close(f).pct_change().rolling(20).std() * 2",
        "session_window(f, 9 * 60 + 30, 10 * 60 + 30) & (f['volume'] > 0)",
    ]
    for expression in accepted:
        validate_signal_expression(expression)
        spec = compile_signal_expression(
            name="ws_ok",
            family="test",
            description="",
            min_bars=30,
            expression=expression,
        )
        assert spec.expression == expression
    probe = _bars(_wavy_closes(200))
    probe["macro_regime"] = -1.0
    column = compile_signal_expression(
        name="ws_ok", family="test", description="", min_bars=30, expression=accepted[0]
    ).build(probe)
    assert column.dtype == bool and len(column) == len(probe)
