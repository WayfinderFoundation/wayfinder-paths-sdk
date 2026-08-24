from __future__ import annotations

import subprocess
import sys

from wayfinder_paths.runner.daemon import JOB_NICE, _deprioritize


def test_deprioritize_sets_lowest_nice() -> None:
    """A subprocess launched with _deprioritize as preexec_fn runs at JOB_NICE;
    a plain one stays at 0 — so scheduled jobs yield to interactive work."""
    read_nice = "import os;print(os.getpriority(os.PRIO_PROCESS,0))"
    niced = subprocess.check_output(
        [sys.executable, "-c", read_nice], preexec_fn=_deprioritize
    )
    control = subprocess.check_output([sys.executable, "-c", read_nice])
    assert int(niced.decode().strip()) == JOB_NICE
    assert int(control.decode().strip()) == 0
