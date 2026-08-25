"""The improver as a versioned artifact (L3 foundation).

Search policy previously lived as scattered literals: staleness thresholds in
worker prompt prose, ideation cadence module constants, probation caps in
probation.py, evidence-tier numbers in research_priors.md. That made the
improver invisible to the system it runs — a policy change was a code diff
nobody's artifacts recorded, and "which improver produced this proposal?" was
unanswerable.

U1 captures today's implicit policy as data. The spec loads from
``improver.yaml`` at the job root (agent-READABLE, changed only through a
``kind="improver_change"`` proposal the owner approves — application.py owns
the write). Its revision — the file hash, or ``U1-defaults`` when no file
exists — is stamped on every artifact the loop produces (journal rows,
proposals, experiments, trials, verdicts, archive entries, probation legs),
alongside the governance revision. Descendant-productivity evaluation of
improver revisions (L3 meta-campaigns) builds on these stamps later.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

IMPROVER_FILENAME = "improver.yaml"
DEFAULT_IMPROVER_REVISION = "U1-defaults"

DEFAULT_IMPROVER: dict[str, Any] = {
    "version": 1,
    "lineage": "U1",
    # Idle-research mandate thresholds (worker prompt + evolution ledger).
    "staleness": {"experiment_days": 3.0, "wakes_since_proposal": 100},
    # Forced ideation-expedition cadence. 20h (not 24h) so a 30m wake cadence
    # cannot alias it to every-other-day.
    "ideation": {"due_hours": 20, "overdue_hours": 48},
    # N consecutive neutral/hurt verdicts on same-family refinements => jump
    # basins (new family / universe-scan / archived branch / sizing axis).
    "stuck_rule": {"same_family_non_wins": 2},
    "probation": {
        "max_active_legs": 2,
        "max_size_fraction": 0.5,
        # Paper entry tier: a candidate "not clearly worse" than baseline
        # (net_return within max(pct, frac*|baseline|) on the same window,
        # with a sane backtest trade count) may open a PAPER leg without
        # beating baseline and without owner approval — probation is the
        # containment; graduation to live keeps the full strict gate.
        "paper_max_active_legs": 3,
        "paper_regression_budget_pct": 0.02,
        "paper_regression_budget_frac": 0.25,
        "paper_min_backtest_trades": 10,
        # Flat-zero retirement floor: past this many closed forward trades,
        # a paper leg with negative net PnL is retired mechanically.
        "paper_floor_min_trades": 5,
    },
    # Evidence tiers rendered into the research-priors prompt; the gate
    # machinery keeps its own enforcement — these are the search policy's
    # triage numbers, one source of truth for the prose.
    "tiers": {
        "tier1": {"max_q": 0.10, "folds": "3/4"},
        "tier2": {
            "max_q": 0.20,
            "folds": "2/4",
            "regime_max_q": 0.15,
            "regime_min_n": 20,
            "recent_window_max_q": 0.10,
        },
    },
    # Island allocation (consumed by the deterministic scheduler): U1 encodes
    # today's implicit behavior — exploit-heavy, with a protected floor so
    # exploration can never be starved to zero. exploration_floor = minimum
    # combined weight share of islands other than exploit/adjacent.
    "islands": {
        "weights": {
            "exploit": 0.40,
            "adjacent": 0.20,
            "divergent": 0.15,
            "diversification": 0.10,
            "falsifier": 0.10,
            "historian": 0.05,
        },
        "exploration_floor": 0.25,
    },
    # Isolated open-ended code evolution. The rollout is deliberately limited
    # to the named lab job; other jobs see no campaign state or extra work.
    "evolution": {
        "enabled": True,
        "allowed_job_ids": ["majors-5m-lab"],
        "campaign_hours": 4,
        "cooldown_hours": 24,
        "generated_programs": 12,
        "full_dev_survivors": 4,
        "inner_optuna_finalists": 2,
        "inner_optuna_trials": 20,
        "sealed_audits": 2,
        "split": {"train": 0.70, "validation": 0.15, "audit": 0.15},
        "parent_mix": {
            "incumbent": 0.30,
            "qd_elite": 0.30,
            "crossover": 0.20,
            "de_novo": 0.20,
        },
        "min_structural_fraction": 0.50,
        "max_parameter_fraction": 0.25,
    },
}


@dataclass(frozen=True)
class ImproverSpec:
    revision: str
    source: str  # "defaults" | "file"
    policy: dict[str, Any]

    @classmethod
    def load(cls, root: Path) -> ImproverSpec:
        path = Path(root) / IMPROVER_FILENAME
        if not path.exists():
            return cls(
                revision=DEFAULT_IMPROVER_REVISION,
                source="defaults",
                policy=dict(DEFAULT_IMPROVER),
            )
        # The file is only ever written by a governed apply that validated the
        # payload — a malformed file is an incident, not a degrade-to-defaults.
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"improver spec is not a mapping: {path}")
        return cls(
            revision=improver_revision(root),
            source="file",
            policy=merge_over_defaults(loaded),
        )

    @property
    def staleness_experiment_days(self) -> float:
        return float(self.policy["staleness"]["experiment_days"])

    @property
    def staleness_wakes(self) -> int:
        return int(self.policy["staleness"]["wakes_since_proposal"])

    @property
    def ideation_due_s(self) -> int:
        return int(float(self.policy["ideation"]["due_hours"]) * 3600)

    @property
    def ideation_overdue_s(self) -> int:
        return int(float(self.policy["ideation"]["overdue_hours"]) * 3600)

    @property
    def stuck_same_family_non_wins(self) -> int:
        return int(self.policy["stuck_rule"]["same_family_non_wins"])

    @property
    def probation_max_active_legs(self) -> int:
        return int(self.policy["probation"]["max_active_legs"])

    @property
    def probation_max_size_fraction(self) -> float:
        return float(self.policy["probation"]["max_size_fraction"])

    @property
    def paper_max_active_legs(self) -> int:
        return int(self.policy["probation"]["paper_max_active_legs"])

    @property
    def paper_regression_budget_pct(self) -> float:
        return float(self.policy["probation"]["paper_regression_budget_pct"])

    @property
    def paper_regression_budget_frac(self) -> float:
        return float(self.policy["probation"]["paper_regression_budget_frac"])

    @property
    def paper_min_backtest_trades(self) -> int:
        return int(self.policy["probation"]["paper_min_backtest_trades"])

    @property
    def paper_floor_min_trades(self) -> int:
        return int(self.policy["probation"]["paper_floor_min_trades"])

    @property
    def island_weights(self) -> dict[str, float]:
        return {
            str(k): float(v)
            for k, v in (self.policy["islands"]["weights"] or {}).items()
        }

    @property
    def exploration_floor(self) -> float:
        return float(self.policy["islands"]["exploration_floor"])

    @property
    def evolution(self) -> dict[str, Any]:
        return dict(self.policy["evolution"])

    def evolution_enabled_for(self, job_id: str) -> bool:
        evolution = self.policy["evolution"]
        allowed = {str(item) for item in evolution.get("allowed_job_ids") or []}
        return bool(evolution.get("enabled")) and job_id in allowed

    def tier(self, name: str) -> dict[str, Any]:
        return dict(self.policy["tiers"][name])


def merge_over_defaults(loaded: dict[str, Any]) -> dict[str, Any]:
    from wayfinder_paths.jobs.constitution import _merge

    return _merge(DEFAULT_IMPROVER, loaded)


def improver_revision(root: Path) -> str:
    path = Path(root) / IMPROVER_FILENAME
    if not path.exists():
        return DEFAULT_IMPROVER_REVISION
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def governance_revision_for_root(root: Path) -> str | None:
    """The revision of the standard this artifact was produced under —
    composite governance-plane hash when migrated, legacy constitution hash
    otherwise, None for jobs with no standard installed."""
    from wayfinder_paths.jobs.constitution import (
        CONSTITUTION_FILENAME,
        _governance_dir_for_job_root,
        constitution_revision,
    )

    gov_dir = _governance_dir_for_job_root(Path(root))
    if gov_dir is not None:
        from wayfinder_paths.jobs.governance import composite_revision

        return composite_revision(gov_dir)
    return constitution_revision(Path(root) / CONSTITUTION_FILENAME)


def revision_stamp(root: Path) -> dict[str, str | None]:
    """The two provenance fields every artifact carries: which improver
    produced it, under which governance standard. File-hash based — no YAML
    parse — so it is cheap enough for every journal append."""
    return {
        "improver_revision": improver_revision(root),
        "governance_revision": governance_revision_for_root(root),
    }
