"""WOB governance-trap suite: every production defense must refuse its
adversarial input. A red trap is a live governance hole."""

from __future__ import annotations

from pathlib import Path

import pytest

from wayfinder_paths.jobs.benchmarks.stress import TRAPS, run_stress_suite


def test_full_stress_suite_holds(tmp_path: Path) -> None:
    report = run_stress_suite(tmp_path)
    breached = {
        name: r["detail"]
        for name, r in report["results"].items()
        if not r["held"]
    }
    assert report["grade"] == "GOVERNANCE_VALID", f"breached traps: {breached}"
    assert report["breached"] == 0
    assert report["held"] == len(TRAPS)


@pytest.mark.parametrize("trap_name", list(TRAPS))
def test_each_trap_holds(trap_name: str, tmp_path: Path) -> None:
    result = TRAPS[trap_name](tmp_path)
    assert result["held"], f"{trap_name} breached: {result['detail']}"
    assert result["defense"]  # every trap names the defense it exercises
