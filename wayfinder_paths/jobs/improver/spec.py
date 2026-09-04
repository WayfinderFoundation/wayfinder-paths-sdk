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
    # Isolated open-ended code evolution. Keep the default canary-scoped;
    # fleet rollout is an explicit job-local policy change after canary health.
    "evolution": {
        "enabled": True,
        "allowed_job_ids": ["majors-5m-lab"],
        "excluded_job_ids": [],
        "campaign_hours": 4,
        "start_interval_hours": 24,
        # DeepSeek has announced 2x pricing during 09:00-12:00 and
        # 14:00-18:00 Beijing time. Keep this in UTC so host DST cannot move it.
        # The guard leaves one hourly worker interval for the final prompt to
        # finish before peak pricing starts.
        "pricing_schedule": {
            "provider": "deepseek",
            "blocked_windows_utc": [["01:00", "04:00"], ["06:00", "10:00"]],
            "campaign_guard_minutes": 60,
        },
        # Backward-compatible fallback for job-local policies written before
        # start_interval_hours was introduced.
        "cooldown_hours": 12,
        # Investigation-first evolution: eight distinct ideas, each with a
        # bounded local repair loop.  ``generated_programs`` remains the
        # compatibility name consumed by v1 campaign manifests and activity.
        "generated_programs": 8,
        "investigation_design_enabled": True,
        # Candidates may declare one or two engine-owned portfolio regime
        # cells, and campaign design must then include the current cell's
        # opposite.  Off until the bench A/B certifies it; the canary job
        # enables it through its own improver policy.
        "regime_specialist_enabled": False,
        "max_attempts_per_idea": 3,
        "max_quick_attempts": 24,
        # Screen every slot once, then spend the remaining fixed budget on the
        # few candidates showing causal progress.  ``screen_before_repair``
        # False restores the depth-first order for the bench control arm.
        "screen_before_repair": True,
        "focus_candidates": 3,
        "focus_attempts_per_candidate": 6,
        # Cost-bleed diagnosis: fees per 30 days above the incumbent's rate
        # times this multiple, or above the absolute floor, on a losing screen.
        "cost_bleed_fee_multiple": 3.0,
        "cost_bleed_fee_pct_of_capital_30d": 0.10,
        "max_fills_per_day_multiple": 3.0,
        # Cost arithmetic first: a trade must capture this multiple of the
        # round-trip cost gross, and the cadence ceiling is the fills/day that
        # keep fees plus slippage under this share of capital per 30 days.
        "cost_hurdle_multiple": 1.5,
        "max_cost_pct_of_capital_30d": 0.02,
        # Quick screen generalization: two disjoint train slices, each must be
        # positive with a paired block-bootstrap LCB > 0 at a confidence that
        # rises with every repair (each repair is another look at the slice).
        "screen_slices": 2,
        "screen_confidence_base": 0.70,
        "screen_slice_max_loss": 0.02,
        # A campaign that finds nothing while the incumbent lost to cash
        # recommends retiring it to cash (the bench applies, production
        # proposes to the owner).
        "retire_to_flat_when_incumbent_negative": True,
        # No escalation: the screen filters, full development certifies.
        "screen_confidence_step": 0.0,
        # Incumbent failure modes (two bounded sims at campaign start) point the
        # design at the days and regimes where the incumbent loses.
        "incumbent_failure_modes": True,
        # Deterministic local search around the incumbent for parameter slots
        # (probe live knobs, tune on the recent slice, verify on the earlier).
        # Off in production until prepare-time cost is measured on a box.
        "incumbent_neighborhood_search": False,
        "incumbent_neighborhood_trials": 6,
        "incumbent_neighborhood_timeout_seconds": 180,
        "incumbent_neighborhood_span": 0.3,
        # Complexity budget: comparisons (gates) may not exceed the larger of
        # the floor and the multiple of the incumbent's own count.
        "complexity_floor_comparisons": 24,
        "complexity_multiple": 1.5,
        # Signal-first seeding: library event studies on the two screen slices;
        # signals significant on both feed the design prompt. An A/B arm
        # variable: off by default, on in the treatment arm.
        # On by default: grounded free-form slots must build on what survives.
        "signal_first_seeding": True,
        "signal_first_limit": 10,
        "signal_first_min_t_net": 2.0,
        # Power and family-corrected significance, not cadence: a per-day
        # density floor rejected every slow horizon.
        "signal_first_min_events": 40,
        "signal_first_max_q": 0.20,
        "signal_first_slice_min_t": 1.0,
        "signal_first_condition_features": ["macro_regime", "leader_state"],
        "signal_scan_min_events": 30,
        "wildcard_slots": 2,
        "elite_min_validation_trades": 8,
        "elite_participation_target_trades": 12,
        "full_dev_survivors": 4,
        "inner_optuna_min_finalists": 1,
        "inner_optuna_finalists": 2,
        "inner_optuna_trials": 20,
        "inner_optuna_preview_trials": 3,
        "inner_optuna_preview_bars": 2_000,
        "inner_optuna_preview_timeout_seconds": 300,
        # Experimental cheap preflight for parameter candidates. Keep off in
        # production until its full-process A/B measures both compute saved
        # and any false rejection of useful sparse behavior.
        "behavior_preview_enabled": False,
        # Sequential replay of the quick window's tail with persistent state
        # for structural candidates: diagnostic on a first attempt, the gate
        # for a no-trade repair (the replay must move before another screen).
        "sequence_preview_enabled": True,
        "sequence_preview_bars": 2_000,
        "inner_optuna_train_bars": 10_000,
        "inner_optuna_timeout_seconds": 1_800,
        "proposal_finalists": 1,
        "finalist_risk_normalization": True,
        "finalist_risk_margin": 0.9,
        "split": {"train": 0.80, "validation": 0.20},
        "parent_mix": {
            "incumbent": 0.30,
            "qd_elite": 0.30,
            "crossover": 0.20,
            "de_novo": 0.20,
        },
        "min_structural_fraction": 0.50,
        "max_parameter_fraction": 0.25,
        "paper_experiment": {
            "enabled": True,
            "duration_days": 14,
            "bar_interval": "5m",
            "qualification_days": 7,
            "proposal_hours": 24,
            "compute_duty_fraction": 0.20,
            "completion_duty_fraction": 0.25,
            "confidence": 0.90,
        },
        "probation": {
            "max_active": 3,
            "max_queued": 3,
            "burn_in_hours": 24,
            "min_paired_days": 7,
            "max_paired_days": 14,
            "confidence": 0.90,
            "min_effect_utility": 0.001,
            "min_candidate_trades": 3,
        },
        "research_seed_slots": 2,
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
        excluded = {str(item) for item in evolution.get("excluded_job_ids") or []}
        if not evolution.get("enabled") or job_id in excluded:
            return False
        if allowed and job_id not in allowed:
            return False
        return True

    def evolution_eligibility(self, root: Path, job_id: str) -> dict[str, Any]:
        """Cheap fleet gate for runnable jobs with a canonical local dataset."""
        if not self.evolution_enabled_for(job_id):
            return {"eligible": False, "reasons": ["disabled_or_excluded"]}
        reasons: list[str] = []
        try:
            raw = yaml.safe_load((Path(root) / "job.yaml").read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            raw = None
        if not isinstance(raw, dict):
            return {"eligible": False, "reasons": ["job_yaml_unreadable"]}
        if raw.get("execution_contract") != "jobs_v1":
            reasons.append("execution_contract_not_jobs_v1")
        script_loop = raw.get("script_loop") or {}
        agent_loop = raw.get("agent_loop") or {}
        if not isinstance(script_loop, dict) or not script_loop.get("enabled"):
            reasons.append("script_loop_disabled")
        if (
            not isinstance(agent_loop, dict)
            or not agent_loop.get("enabled")
            or str(agent_loop.get("mode") or "off") not in {"intervene", "auto"}
        ):
            reasons.append("agent_loop_not_intervene_or_auto")
        dataset = Path(root) / "results" / "backtest" / "input_bars.json"
        if not dataset.is_file():
            reasons.append("canonical_dataset_missing")
        return {"eligible": not reasons, "reasons": reasons}

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
