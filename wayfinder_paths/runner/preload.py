"""Forkserver preload for warm jobs_v1 ticks.

Imported once inside the forkserver process (see `runner/warm_spawn.py`), so
every forked tick starts with pandas and the jobs execution stack already in
memory instead of cold-importing the full SDK per tick. Imports are
best-effort: a broken heavy import must degrade to slower forks (the child
re-imports and surfaces the real error in its own log), never kill the
forkserver — the daemon would then fall back to cold `subprocess.Popen` ticks
anyway.
"""

from __future__ import annotations

import gc

for _module_name in (
    "pandas",
    "wayfinder_paths.jobs.execution.primitives",
    "wayfinder_paths.jobs.execution.engine",
    "wayfinder_paths.jobs.execution.driver",
):
    try:
        __import__(_module_name)
    except Exception:  # noqa: BLE001 — degrade to a cold import in the child
        pass

# Move everything imported so far into the permanent generation: forked ticks
# share these pages copy-on-write, and freezing keeps the child's first GC from
# touching (and un-sharing) them.
gc.freeze()
