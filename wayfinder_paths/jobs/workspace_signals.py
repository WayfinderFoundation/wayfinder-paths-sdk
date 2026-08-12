"""Per-job workspace signals: agent-composed triggers swept by `signal-scan`.

A job may declare extra `SignalDef`s in `workspace/src/signals.py`:

    from wayfinder_paths.jobs.signal_library import SignalDef

    WORKSPACE_SIGNALS = (
        SignalDef(
            "funding_neg_new_high_5",
            "workspace",
            "fresh 5-bar high while funding is negative",
            9,
            lambda f: (f["close"] > f["close"].shift(1).rolling(5).max())
            & (f["funding"] < 0),
        ),
    )

`signal_scan_job` sweeps these ALONGSIDE the canonical library under the same
pooled Benjamini-Hochberg family, event decimation, fold-stability gate, and
reserved holdout — that shared discipline is the whole point. Serial one-off
`signal-check`s on hand-rolled columns have no multiple-testing control;
declaring the composition here puts it back under one.

The contract is a list of named defs — NOT a frame-level hook — because
`holdout-check` must recompute one signal by name on the full frame,
`min_bars` gates warmup junk per def, and the cap/validation need per-def
identity. Validation is fail-loud: one bad def aborts the scan, because a
silently shrunk family would falsify the declared BH denominator (the agent
that wrote the file is awake in the same session and can fix it).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from wayfinder_paths.jobs.execution.primitives import _load_module_from_path
from wayfinder_paths.jobs.signal_library import SignalDef, signal_defs

WORKSPACE_SIGNALS_ATTR = "WORKSPACE_SIGNALS"
WORKSPACE_SIGNALS_RELPATH = "workspace/src/signals.py"
# Every def added to a scan inflates the BH denominator for the WHOLE family
# — breadth costs power for everything else in the same scan. The cap keeps
# one wake's composition budget honest; raising it is a deliberate decision,
# not a workaround.
WORKSPACE_SIGNAL_CAP = 12

_NAME_RE = re.compile(r"^[a-z0-9_]{1,48}$")
_CAUSALITY_TRUNCATE = 50


@dataclass(frozen=True)
class WorkspaceSignalSet:
    defs: tuple[SignalDef, ...]
    path: Path
    # sha1 of the file bytes: scan/holdout provenance. The ledger records it
    # so "rename the def and relaunch" is visible in audit as the same (or
    # changed) code rather than a fresh trial family.
    sha: str


def load_workspace_signals(root: Path) -> WorkspaceSignalSet | None:
    """Load `workspace/src/signals.py` for a job root, or None when absent.

    Absent file → canonical-only scan, silently (most jobs never compose).
    A present-but-broken file raises: the declared family must equal the
    executed family.
    """
    path = root / WORKSPACE_SIGNALS_RELPATH
    if not path.exists():
        return None
    module = _load_module_from_path(path)
    raw = getattr(module, WORKSPACE_SIGNALS_ATTR, None)
    if raw is None:
        raise ValueError(
            f"{path} exists but defines no {WORKSPACE_SIGNALS_ATTR}; "
            "remove the file or declare the tuple"
        )
    defs = tuple(raw)
    for entry in defs:
        if not isinstance(entry, SignalDef):
            raise ValueError(
                f"{WORKSPACE_SIGNALS_ATTR} entries must be SignalDef, "
                f"got {type(entry).__name__}"
            )
    sha = hashlib.sha1(path.read_bytes()).hexdigest()[:12]
    return WorkspaceSignalSet(defs=defs, path=path, sha=sha)


def validate_workspace_signals(
    defs: Sequence[SignalDef],
    probe: pd.DataFrame,
    *,
    truncate: int = _CAUSALITY_TRUNCATE,
) -> None:
    """Fail-loud validation of workspace defs against a real probe frame.

    Checks: cap, name shape/uniqueness, disjointness from the canonical
    library (the entire ledger-hash-collision defense — `_trial_hash` keys on
    the signal NAME), boolean output of input length, and an automated
    causality gate: building on a truncated frame must reproduce the full
    frame's prefix. That catches centered rolling windows and full-frame
    normalization; it cannot catch back-dated feature rows — the append-only
    feature store owns that discipline.
    """
    problems: list[str] = []
    if not defs:
        problems.append("WORKSPACE_SIGNALS is empty — remove the file instead")
    if len(defs) > WORKSPACE_SIGNAL_CAP:
        problems.append(
            f"{len(defs)} defs exceed the cap of {WORKSPACE_SIGNAL_CAP}; every "
            "def raises the promote bar for the whole scan family — trim to "
            "the strongest hypotheses"
        )
    canonical = set(signal_defs())
    seen: set[str] = set()
    for spec in defs:
        if not _NAME_RE.match(spec.name):
            problems.append(f"{spec.name!r}: name must match {_NAME_RE.pattern}")
        if spec.name in canonical:
            problems.append(f"{spec.name!r}: collides with a canonical library signal")
        if spec.name in seen:
            problems.append(f"{spec.name!r}: duplicate name")
        seen.add(spec.name)
    if problems:
        raise ValueError("invalid workspace signals: " + "; ".join(problems))
    if len(probe) <= truncate:
        raise ValueError(
            f"probe frame too short ({len(probe)} rows) to validate "
            f"workspace signals (need > {truncate})"
        )
    for spec in defs:
        try:
            full = spec.build(probe)
        except Exception as exc:
            raise ValueError(f"{spec.name!r}: build raised {exc!r}") from exc
        if len(full) != len(probe):
            problems.append(
                f"{spec.name!r}: output length {len(full)} != input {len(probe)}"
            )
            continue
        coerced = full.fillna(False)
        if not set(pd.unique(coerced)) <= {True, False}:
            problems.append(
                f"{spec.name!r}: output must be boolean (got dtype "
                f"{full.dtype}); floats silently truthy-coerce"
            )
            continue
        truncated = spec.build(probe.iloc[:-truncate]).fillna(False).astype(bool)
        prefix = coerced.astype(bool).iloc[: len(truncated)]
        if not truncated.reset_index(drop=True).equals(prefix.reset_index(drop=True)):
            problems.append(
                f"{spec.name!r}: non-causal — truncating the frame changed "
                "earlier values (centered window or full-frame statistic?)"
            )
            continue
        # Truncation only disagrees at the boundary row, which can coincide
        # by luck (a rare-firing lookahead is mostly False either way). The
        # stronger gate: violently perturb the TAIL both directions — any
        # builder reading future rows cannot reproduce the original prefix
        # under both x10 and x0.1 tails.
        base_prefix = coerced.astype(bool).iloc[:-truncate].reset_index(drop=True)
        numeric = [
            c
            for c in probe.columns
            if c not in ("timestamp", "symbol")
            and pd.api.types.is_numeric_dtype(probe[c])
        ]
        for scale in (10.0, 0.1):
            perturbed = probe.copy()
            perturbed.loc[perturbed.index[-truncate:], numeric] = (
                perturbed.loc[perturbed.index[-truncate:], numeric] * scale
            )
            shifted = (
                spec.build(perturbed)
                .fillna(False)
                .astype(bool)
                .iloc[:-truncate]
                .reset_index(drop=True)
            )
            if not shifted.equals(base_prefix):
                problems.append(
                    f"{spec.name!r}: non-causal — perturbing future bars "
                    "changed earlier values (uses shift(-1) or a "
                    "full-frame statistic?)"
                )
                break
    if problems:
        raise ValueError("invalid workspace signals: " + "; ".join(problems))
