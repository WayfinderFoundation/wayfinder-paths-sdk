from __future__ import annotations

import json
import shutil
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.failures import classify_failure
from wayfinder_paths.jobs.forward import default_forward_summary
from wayfinder_paths.jobs.models import (
    ApplicationStatus,
    WayfinderJob,
    safe_job_id,
    utc_now_iso,
)
from wayfinder_paths.runner.monitor_state import atomic_write_json
from wayfinder_paths.runner.paths import find_repo_root

APPLICATION_STATUSES = {
    "not_requested",
    "queued",
    "applying",
    "applied",
    "failed",
    "canceled",
}
PROPOSAL_STATUSES = {"pending", "approved", "rejected"}
REJECTION_KINDS = {"process", "substantive"}
# Reasons that mark a rejection as process housekeeping (mechanics of the
# pipeline) rather than a substantive verdict on the change itself.
_PROCESS_REJECTION_MARKERS = (
    "superseded",
    "re-stage",
    "restage",
    "oom",
    "infrastructure",
    "red gate",
)
SUCCESSOR_EXPECTED_PATH = "state/successor_expected.json"


class JobStore:
    def __init__(self, *, repo_root: Path | None = None) -> None:
        self.repo_root = (repo_root or find_repo_root()).resolve()
        self.jobs_dir = self.repo_root / ".wayfinder" / "jobs"
        self.runs_jobs_dir = self.repo_root / ".wayfinder_runs" / "jobs"

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / safe_job_id(job_id)

    def job_yaml_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.yaml"

    def resolve_script_entrypoint(
        self,
        job_id: str,
        job_data: Mapping[str, Any],
        *,
        candidate_dir: str | Path | None = None,
    ) -> Path | None:
        script_loop = job_data.get("script_loop")
        match script_loop:
            case Mapping() if script_loop.get("enabled"):
                pass
            case _:
                return None
        raw = str(script_loop.get("entrypoint") or "").strip()
        if not raw:
            return None

        root = self.job_dir(job_id)
        active_workspace = root / "workspace"
        candidate_root = Path(candidate_dir) if candidate_dir else None
        candidate_workspace = candidate_root / "workspace" if candidate_root else None
        target_workspace = candidate_workspace or active_workspace
        path = Path(raw)

        if path.is_absolute():
            if candidate_workspace is None:
                return path
            resolved = path.resolve()
            for workspace in (active_workspace, candidate_workspace):
                base = workspace.resolve()
                if resolved.is_relative_to(base):
                    return candidate_workspace / resolved.relative_to(base)
            return path

        parts = path.parts
        if ".wayfinder" in parts and "workspace" in parts:
            workspace_index = parts.index("workspace")
            return target_workspace.joinpath(*parts[workspace_index + 1 :])
        if parts and parts[0] == "workspace":
            return (candidate_root or root) / path
        return self.repo_root / path

    def init_layout(self, job: WayfinderJob) -> Path:
        root = self.job_dir(job.id)
        for relative in [
            "workspace/src",
            "workspace/config",
            "versions",
            "results/backtest",
            "results/forward",
            "proposals",
            "applications",
            "reports/monitor",
            "reports/intervene",
            "reports/auto",
            "reports/apply",
            "reports/validation",
            "sessions",
        ]:
            (root / relative).mkdir(parents=True, exist_ok=True)
        self.runs_jobs_dir.mkdir(parents=True, exist_ok=True)
        memory_md = root / "memory.md"
        if not memory_md.exists():
            memory_md.write_text(
                f"# {job.name} Job Memory\n\n"
                "Goal:\n"
                f"{job.goal or 'No goal recorded yet.'}\n\n"
                "Current rule:\n"
                "- Active revision is the source of truth.\n"
                "- Script runs should write structured results and emit chat only on meaningful transitions.\n"
                "- Intervene-mode agent changes require user approval before activation.\n"
                "- Auto-mode agent decisions must respect the job's configured live limits.\n\n"
                "Known lessons:\n"
                "- None yet.\n\n"
                "Current concern:\n"
                "- None yet.\n",
                encoding="utf-8",
            )
        self._write_json_if_missing(
            root / "memory.json",
            {
                "job_id": job.id,
                "updated_at": utc_now_iso(),
                "lessons": [],
                "constraints": [],
                "current_concern": None,
            },
        )
        self._write_json_if_missing(
            root / "scorecard.json",
            {
                "job_id": job.id,
                "health": "unknown",
                "last_script_run_at": None,
                "last_agent_check_at": None,
                "pending_proposals": 0,
            },
        )
        self._write_json_if_missing(root / "runner_links.json", {"jobs": []})
        self._write_jsonl_if_missing(root / "journal.jsonl")
        self._write_jsonl_if_missing(root / "versions" / "revisions.jsonl")
        self._write_jsonl_if_missing(root / "results" / "forward" / "runs.jsonl")
        self._write_jsonl_if_missing(root / "results" / "forward" / "trades.jsonl")
        self._write_jsonl_if_missing(root / "results" / "forward" / "orders.jsonl")
        self._write_jsonl_if_missing(root / "results" / "forward" / "fills.jsonl")
        self._write_json_if_missing(
            root / "results" / "forward" / "summary.json",
            default_forward_summary(job.id, inception_at=job.created_at),
        )
        self._write_json_if_missing(
            root / "versions" / "active.json",
            {
                "job_id": job.id,
                "active_revision": job.versioning.get("active_revision"),
                "active_label": job.versioning.get("active_label"),
            },
        )
        return root

    def save(self, job: WayfinderJob) -> Path:
        root = self.init_layout(job)
        path = root / "job.yaml"
        job.touch()
        path.write_text(
            yaml.safe_dump(job.to_dict(), sort_keys=False), encoding="utf-8"
        )
        return path

    def create_job(self, job: WayfinderJob) -> Path:
        """Creation path: layout + workspace entrypoint scaffolding + save.

        Only creation calls this (CLI/MCP create). `save()` stays
        scaffold-free because it runs on every mutation and must never move
        entrypoints out from under a running job.
        """
        self.init_layout(job)
        self.scaffold_workspace_entrypoint(job)
        return self.save(job)

    def scaffold_workspace_entrypoint(self, job: WayfinderJob) -> dict[str, Any]:
        """Force the script entrypoint under <job>/workspace/src.

        Revisions hash only workspace/* + job.yaml and proposals stage only
        workspace/, so strategy code anywhere else can never be versioned or
        promoted. An existing file outside the workspace is copied in; a
        not-yet-written script just gets its entrypoint rewritten to the
        workspace path (no stub — execution_script_exists should stay honest).
        """
        if not (job.script_loop.enabled and job.script_loop.entrypoint):
            return {"entrypoint": job.script_loop.entrypoint, "scaffolded": False}
        root = self.job_dir(job.id)
        workspace = (root / "workspace").resolve()
        resolved = self.resolve_script_entrypoint(job.id, job.to_dict())
        if resolved is not None and resolved.resolve().is_relative_to(workspace):
            return {"entrypoint": job.script_loop.entrypoint, "scaffolded": False}

        basename = Path(job.script_loop.entrypoint).name or "strategy.py"
        target_rel = f"workspace/src/{basename}"
        copied_from: str | None = None
        if resolved is not None and resolved.is_file():
            (root / "workspace" / "src").mkdir(parents=True, exist_ok=True)
            shutil.copy2(resolved, root / target_rel)
            copied_from = str(resolved)
        job.script_loop.entrypoint = target_rel
        self.append_journal(
            job.id,
            {
                "type": "entrypoint_scaffolded",
                "entrypoint": target_rel,
                "copied_from": copied_from,
            },
        )
        return {
            "entrypoint": target_rel,
            "copied_from": copied_from,
            "scaffolded": True,
        }

    def load(self, job_id: str) -> WayfinderJob:
        path = self.job_yaml_path(job_id)
        if not path.exists():
            raise FileNotFoundError(f"Wayfinder job not found: {safe_job_id(job_id)}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        match data:
            case dict():
                return WayfinderJob.from_dict(data)
            case _:
                raise ValueError(f"Invalid job spec: {path}")

    def list_jobs(self) -> list[WayfinderJob]:
        if not self.jobs_dir.exists():
            return []
        jobs: list[WayfinderJob] = []
        for path in sorted(self.jobs_dir.glob("*/job.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                match data:
                    case dict():
                        jobs.append(WayfinderJob.from_dict(data))
            except Exception:
                continue
        return jobs

    def read_json(self, job_id: str, relative: str, default: Any = None) -> Any:
        path = self.job_dir(job_id) / relative
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def write_json(self, job_id: str, relative: str, data: Any) -> Path:
        path = self.job_dir(job_id) / relative
        atomic_write_json(path, data)
        return path

    def read_jsonl(
        self, job_id: str, relative: str, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Read a job-owned JSONL artifact without letting one bad row hide
        the rest of an append-only ledger."""
        path = self.job_dir(job_id) / relative
        if not path.exists():
            return []
        if limit is None:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        else:
            bounded = max(int(limit), 0)
            if bounded == 0:
                return []
            # Stream append-only ledgers so a bounded consumer does not first
            # materialize the entire (potentially multi-MB) file in memory.
            with path.open(encoding="utf-8", errors="replace") as handle:
                lines = list(deque(handle, maxlen=bounded))
        rows: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def append_journal(self, job_id: str, event: dict[str, Any]) -> None:
        path = self.job_dir(job_id) / "journal.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Central provenance stamp: every journal row records which improver
        # revision and governance standard it was produced under. Stamping
        # must never break journaling.
        stamp: dict[str, Any] = {}
        try:
            from wayfinder_paths.jobs.improver.spec import revision_stamp

            stamp = revision_stamp(self.job_dir(job_id))
        except Exception:  # noqa: BLE001
            pass
        path.open("a", encoding="utf-8").write(
            json.dumps({"ts": utc_now_iso(), **stamp, **event}, sort_keys=True) + "\n"
        )

    def proposals(self, job_id: str) -> list[dict[str, Any]]:
        proposals: list[dict[str, Any]] = []
        for path in sorted((self.job_dir(job_id) / "proposals").glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            match data:
                case dict():
                    proposals.append(self._normalize_proposal(data))
        return proposals

    def proposal_queue(self, job_id: str) -> dict[str, list[dict[str, Any]]]:
        queue: dict[str, list[dict[str, Any]]] = {
            "pending": [],
            "queued": [],
            "applying": [],
            "applied": [],
            "failed": [],
            "rejected": [],
        }
        for proposal in self.proposals(job_id):
            status = proposal["status"]
            application_status = proposal["application"]["status"]
            summary = {
                "proposal_id": proposal.get("proposal_id"),
                "status": status,
                "application_status": application_status,
                "summary": (proposal.get("proposed_change") or {}).get("summary")
                or proposal.get("summary"),
            }
            if status == "pending":
                queue["pending"].append(summary)
            elif status == "rejected":
                queue["rejected"].append(summary)
            elif application_status in queue:
                queue[str(application_status)].append(summary)
        return queue

    def load_proposal(self, job_id: str, proposal_id: str) -> dict[str, Any]:
        path = self.job_dir(job_id) / "proposals" / f"{proposal_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Proposal not found: {proposal_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        match data:
            case dict():
                return self._normalize_proposal(data)
            case _:
                raise ValueError(f"Invalid proposal: {proposal_id}")

    def write_proposal(self, job_id: str, proposal: dict[str, Any]) -> Path:
        proposal = self._normalize_proposal(proposal)
        proposal_id = str(proposal.get("proposal_id") or "").strip()
        if not proposal_id:
            raise ValueError("proposal_id is required")
        path = self.job_dir(job_id) / "proposals" / f"{proposal_id}.json"
        atomic_write_json(path, proposal)
        return path

    def _require_scenarios(self, job_id: str) -> bool:
        """Scenario plans are mandatory only for jobs with an enabled script
        loop — research-only (agent_only) jobs cannot execute scenarios."""
        try:
            job = self.load(job_id)
        except Exception:
            return True
        return bool(job.script_loop.enabled)

    def _ensure_candidate_report_gate(
        self, job_id: str, proposal: dict[str, Any], *, allow_ungated: bool
    ) -> None:
        """jobs_v1 approvals require evidence: a candidate_report whose gate
        is live-ready (or, for research-only proposals, passed validation).
        This mirrors the backend approve gate SDK-side so on-machine approvals
        (including auto mode, which has no override) can't skip it."""
        if allow_ungated:
            return
        try:
            contract = self.load(job_id).execution_contract
        except Exception:
            return
        if contract != "jobs_v1":
            return  # legacy jobs are blocked upstream by the contract guard
        report = proposal.get("candidate_report") or {}
        if not report:
            raise ValueError(
                "proposal has no candidate_report; create proposals with "
                "`wayfinder job propose` (or approve with --allow-ungated)"
            )
        if not report.get("revision"):
            raise ValueError("candidate_report is missing its candidate revision")
        from wayfinder_paths.jobs.robustness import (
            required_robustness_acknowledgements,
        )

        required_warnings = required_robustness_acknowledgements(
            report.get("robustness")
        )
        acknowledged = set(proposal.get("robustness_warnings_acknowledged") or [])
        if missing := required_warnings - acknowledged:
            raise ValueError(
                f"candidate robustness warnings are not acknowledged: {sorted(missing)}"
            )
        validation_summary = report.get("validation_summary") or {}
        validation_status = validation_summary.get("status")
        if validation_status != "passed":
            failure_kind = validation_summary.get("failure_kind")
            kind_note = f" (failure_kind: {failure_kind})" if failure_kind else ""
            # Wording deliberately avoids INFRASTRUCTURE_PATTERNS terms: this
            # message rides along in rejection reasons, which classify_failure
            # re-reads — a pattern word here would flip evidence rejections
            # into refused-rejection loops.
            raise ValueError(
                f"candidate validation is not passed: {validation_status}"
                f"{kind_note} — if the failure_kind is infrastructure (a "
                "transient box condition, not a verdict on the change), run: "
                f"wayfinder job revalidate {safe_job_id(job_id)} "
                f"{proposal.get('proposal_id')} to re-run validation against "
                "the same staged candidate"
            )
        if report.get("mode") == "validation_only":
            self._ensure_candidate_matches_report(job_id, proposal, report)
            return
        gate = report.get("gate") or {}
        if gate.get("live_ready") is not True:
            reasons = "; ".join(gate.get("reasons") or ["unknown"])
            raise ValueError(f"candidate gate is not live-ready: {reasons}")
        economic = report.get("economic") or {}
        self._ensure_governance_gate(
            job_id, economic, proposal_id=str(proposal.get("proposal_id") or "")
        )
        self._ensure_candidate_matches_report(job_id, proposal, report)

    def _ensure_governance_gate(
        self, job_id: str, economic: dict[str, Any], *, proposal_id: str = ""
    ) -> None:
        """Fail-closed economic gating for live-capable jobs.

        The old semantics blocked only on an explicit ready=False under a
        blocking constitution — a crashed evaluator, missing constitution,
        or a constitution swapped between evaluation and approval all
        promoted freely (observed as the review's central fail-open finding).
        For a job whose script loop has entered paper/live:
        - blocking + ready is not True  -> ESCALATE (None = missing evidence)
        - governance changed since evaluation -> ESCALATE (re-run the report)
        - governance chain tampered -> ESCALATE
        Advisory jobs keep the old behavior (report, never block)."""
        from wayfinder_paths.jobs.constitution import load_constitution

        current = load_constitution(self.job_dir(job_id))
        live_capable = self._job_is_live_capable(job_id)

        chain_status = (current.get("governance") or {}).get("chain_status")
        if live_capable and chain_status == "tampered":
            raise ValueError(
                "ESCALATE: governance chain is tampered (uncommitted edit to "
                "a protected file) — owner must inspect and re-commit "
                "(wayfinder job governance-commit) before any promotion"
            )

        enforcement = str(
            current.get("enforcement") or economic.get("enforcement") or "advisory"
        )
        if enforcement != "blocking" or not live_capable:
            # Pre-live/advisory: old semantics — only an explicit False blocks.
            if (
                economic.get("enforcement") == "blocking"
                and economic.get("ready") is False
            ):
                reasons = "; ".join(economic.get("reasons") or ["unknown"])
                raise ValueError(f"candidate is not economic-ready: {reasons}")
            return

        # Live-capable + blocking: fail closed.
        evaluated_revision = economic.get("constitution_revision")
        if evaluated_revision and evaluated_revision != current.get("revision"):
            raise ValueError(
                "ESCALATE: governance changed since the candidate was "
                f"evaluated (report {evaluated_revision} vs current "
                f"{current.get('revision')}) — re-run the economic report"
            )
        if economic.get("ready") is not True:
            reasons = "; ".join(
                economic.get("reasons")
                or ["economic evidence unavailable (ready is not True)"]
            )
            # Recovery affordance: a frozen economic block whose failure text
            # is infrastructure-class (missing bars, lock, OOM) is a box
            # condition frozen at propose time, not economic evidence — name
            # the in-place fix instead of leaving an un-approvable pending
            # proposal with no exit (2026-08-24 production incident). The
            # reasons text already classifies as infrastructure, so the added
            # wording cannot flip an evidence rejection's classification.
            recovery = ""
            if proposal_id and classify_failure(reasons) == "infrastructure":
                recovery = (
                    " — the economic evaluation failed on a transient box "
                    "condition, not real evidence; run: wayfinder job "
                    f"revalidate {safe_job_id(job_id)} {proposal_id} to "
                    "re-run it against the same staged candidate"
                )
            raise ValueError(
                "ESCALATE: blocking governance requires economic_ready=True "
                f"for a live-capable job; got {economic.get('ready')!r}: "
                f"{reasons}{recovery}"
            )

    def _job_is_live_capable(self, job_id: str) -> bool:
        """A job that has entered paper/live operation: script loop enabled
        with any recorded forward runs. Never raises."""
        try:
            job = self.load(job_id)
            if not job.script_loop.enabled:
                return False
            summary = self.read_json(job_id, "results/forward/summary.json") or {}
            return int(((summary.get("runs") or {}).get("count")) or 0) > 0
        except Exception:
            return False

    def _ensure_candidate_matches_report(
        self, job_id: str, proposal: dict[str, Any], report: dict[str, Any]
    ) -> None:
        """Freshness guard: a candidate bundle edited after its report was
        generated no longer hashes to the validated revision. Reject at approve
        time so a corrupted candidate can never be promoted — the worker must
        re-propose. Mirrors the reuse check in `_prepare_candidate_workspace`
        (which otherwise silently recopies the active workspace, dropping the
        change). Only enforced when the candidate bundle is present on disk."""
        # Lazy imports: gating/application both import JobStore, so a
        # module-level import here would be circular.
        from wayfinder_paths.jobs.application import _candidate_dir_from_proposal
        from wayfinder_paths.jobs.gating import compute_workspace_revision

        recorded = str(report.get("revision") or "")
        if not recorded:
            return
        candidate_dir = _candidate_dir_from_proposal(self, job_id, proposal)
        if not candidate_dir.exists():
            return
        current = compute_workspace_revision(candidate_dir)
        if current != recorded:
            raise ValueError(
                "candidate changed since its report was generated "
                f"(report revision {recorded[:12]}, candidate now "
                f"{current[:12]}); re-propose to regenerate the report"
            )

    def approve_proposal(
        self, job_id: str, proposal_id: str, *, allow_ungated: bool = False
    ) -> dict[str, Any]:
        proposal = self.load_proposal(job_id, proposal_id)
        _validate_applicable_proposal(
            proposal, require_scenarios=self._require_scenarios(job_id)
        )
        self._ensure_candidate_report_gate(
            job_id, proposal, allow_ungated=allow_ungated
        )
        application = proposal["application"]
        application_status = application["status"]
        if proposal["status"] == "rejected":
            raise ValueError(f"Rejected proposal cannot be approved: {proposal_id}")
        if application_status == "applying":
            raise ValueError(f"Proposal is already applying: {proposal_id}")
        if application_status == "applied":
            return proposal
        proposal["status"] = "approved"
        proposal["approval"]["status"] = "approved"
        self._set_application_status(proposal, "queued")
        application.setdefault("requested_at", utc_now_iso())
        proposal["updated_at"] = utc_now_iso()
        self.write_proposal(job_id, proposal)
        self.append_journal(
            job_id,
            {
                "type": "proposal_apply_queued",
                "proposal_id": proposal_id,
                "application_status": "queued",
            },
        )
        self.refresh_scorecard(job_id)
        return proposal

    def queue_proposal_application(
        self, job_id: str, proposal_id: str
    ) -> dict[str, Any]:
        proposal = self.load_proposal(job_id, proposal_id)
        _validate_applicable_proposal(
            proposal, require_scenarios=self._require_scenarios(job_id)
        )
        application_status = proposal["application"]["status"]
        if proposal["status"] != "approved":
            raise ValueError(f"Proposal must be approved before apply: {proposal_id}")
        if application_status == "applied":
            return proposal
        if application_status == "applying":
            raise ValueError(f"Proposal is already applying: {proposal_id}")
        self._set_application_status(proposal, "queued")
        proposal["updated_at"] = utc_now_iso()
        self.write_proposal(job_id, proposal)
        self.append_journal(
            job_id,
            {
                "type": "proposal_apply_queued",
                "proposal_id": proposal_id,
                "application_status": "queued",
            },
        )
        self.refresh_scorecard(job_id)
        return proposal

    def reject_proposal(
        self,
        job_id: str,
        proposal_id: str,
        *,
        reason: str | None = None,
        rejected_by: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        proposal = self.load_proposal(job_id, proposal_id)
        application_status = proposal["application"]["status"]
        if application_status in {"applying", "applied"}:
            raise ValueError(
                f"Cannot reject proposal with application status {application_status}: "
                f"{proposal_id}"
            )
        by = rejected_by or "owner"
        # Durable restage: an agent cannot bury owner-approved work over a
        # transient box failure. When the latest re-stage/validation failure
        # is infrastructure-class (OOM, lock, timeout, missing dataset) OR a
        # fail-closed gate ESCALATE (the gate REFUSED TO EVALUATE — missing
        # evidence, governance drift — as opposed to evaluating the change
        # red), the proposal stays approved with restage_requested and the
        # watchdog retries — only the owner may abandon approved work. (Live
        # incidents: one OOM during a mechanical re-stage led the agent to
        # self-reject an owner-approved proposal; later a missing backtest
        # dataset made the governance gate ESCALATE during a mechanical
        # re-stage and the restage flow's own auto-reject buried the
        # owner-approved change again.)
        if proposal["status"] == "approved" and by != "owner":
            failure_text = _latest_failure_text(proposal, reason)
            is_infrastructure = classify_failure(failure_text) == "infrastructure"
            is_escalate = _contains_fail_closed_escalate(failure_text)
            if is_infrastructure or is_escalate:
                proposal["application"]["restage_requested"] = True
                proposal["updated_at"] = utc_now_iso()
                self.write_proposal(job_id, proposal)
                refusal_event: dict[str, Any] = {
                    "type": "proposal_reject_refused",
                    "proposal_id": proposal_id,
                    "rejected_by": by,
                    "failure_kind": (
                        "infrastructure" if is_infrastructure else "escalate"
                    ),
                }
                if is_escalate:
                    refusal_event["owner_review_required"] = (
                        f"a fail-closed gate ESCALATED instead of evaluating "
                        f"approved proposal {proposal_id} — the agent may not "
                        "translate that refusal-to-evaluate into a rejection. "
                        "The proposal stays approved with restage_requested; "
                        "the owner reviews or the box condition clears."
                    )
                self.append_journal(job_id, refusal_event)
                if is_infrastructure:
                    raise ValueError(
                        f"refusing agent rejection of approved proposal "
                        f"{proposal_id}: its latest re-stage/validation failure "
                        "is infrastructure-class (OOM/lock/timeout) — a "
                        "transient box condition, not evidence against the "
                        "change. The proposal stays approved with "
                        "restage_requested and the watchdog retries the "
                        "re-stage; only the owner may abandon approved work."
                    )
                raise ValueError(
                    f"refusing agent rejection of approved proposal "
                    f"{proposal_id}: its latest failure is a fail-closed gate "
                    "ESCALATE — the gate refused to evaluate (missing "
                    "evidence or governance drift), it did not evaluate the "
                    "change red. The proposal stays approved with "
                    "restage_requested and owner review required; only the "
                    "owner may abandon approved work."
                )
        if kind is not None and kind not in REJECTION_KINDS:
            raise ValueError(f"rejection kind must be one of {sorted(REJECTION_KINDS)}")
        rejection_kind = kind or _infer_rejection_kind(reason)
        proposal["status"] = "rejected"
        proposal["approval"]["status"] = "rejected"
        # Provenance is the difference between "the owner said no" (binding —
        # the worker must not re-propose an equivalent change without named
        # new evidence) and the worker's own superseded-draft housekeeping
        # (retry expected). Without it both rejections looked identical and
        # the worker re-proposed owner-vetoed changes. `kind` splits owner
        # rejections further: only kind=substantive binds as a veto;
        # kind=process (superseded / re-stage mechanics / red-gate
        # housekeeping) is an invitation to file a corrected successor.
        proposal["rejection"] = {
            "reason": reason,
            "by": by,
            "kind": rejection_kind,
            "ts": utc_now_iso(),
        }
        if by == "owner" and rejection_kind == "process":
            # A process rejection from the owner expects a successor
            # proposal; the watchdog wakes the agent if none appears.
            expected = self.read_json(job_id, SUCCESSOR_EXPECTED_PATH, default=[])
            if not isinstance(expected, list):
                expected = []
            expected.append(
                {"proposal_id": proposal_id, "ts": utc_now_iso(), "reason": reason}
            )
            self.write_json(job_id, SUCCESSOR_EXPECTED_PATH, expected)
        if application_status == "queued":
            self._set_application_status(proposal, "canceled")
        proposal["updated_at"] = utc_now_iso()
        self.write_proposal(job_id, proposal)
        from wayfinder_paths.jobs.remediation import handle_remediation_rejection

        handle_remediation_rejection(self, job_id, proposal)
        try:
            from wayfinder_paths.jobs.archive import set_candidate_status

            set_candidate_status(
                self,
                job_id,
                proposal_id,
                "refuted",
                evidence=f"rejected by {rejected_by or 'owner'}: {reason or ''}"[:160],
            )
        except Exception:  # noqa: BLE001 — archive bookkeeping never breaks reject
            pass
        self.append_journal(
            job_id,
            {
                "type": "proposal_rejected",
                "proposal_id": proposal_id,
                "application_status": proposal["application"]["status"],
                "rejected_by": proposal["rejection"]["by"],
                "kind": rejection_kind,
                "reason": reason,
            },
        )
        self.refresh_scorecard(job_id)
        return proposal

    def claim_proposal_application(
        self,
        job_id: str,
        proposal_id: str,
        *,
        paused_runner_jobs: list[dict[str, Any]] | None = None,
        candidate: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # claimable guard runs in application.claim_application before pause_job_loops
        proposal = self.load_proposal(job_id, proposal_id)
        application = proposal["application"]
        self._set_application_status(proposal, "applying")
        application["started_at"] = utc_now_iso()
        application["paused_runner_jobs"] = paused_runner_jobs or []
        if candidate:
            application.update(candidate)
        proposal["updated_at"] = utc_now_iso()
        self.write_proposal(job_id, proposal)
        self.append_journal(
            job_id,
            {
                "type": "proposal_apply_started",
                "proposal_id": proposal_id,
                "paused_runner_jobs": paused_runner_jobs or [],
            },
        )
        self.refresh_scorecard(job_id)
        return proposal

    def complete_proposal_application(
        self,
        job_id: str,
        proposal_id: str,
        *,
        status: ApplicationStatus,
        changed_files: list[str] | None = None,
        validation: dict[str, Any] | None = None,
        error: str | None = None,
        runner_responses: list[dict[str, Any]] | None = None,
        promoted_revision: str | None = None,
        rollback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"applied", "failed"}:
            raise ValueError(
                f"Application completion status must be applied or failed: {status}"
            )
        proposal = self.load_proposal(job_id, proposal_id)
        application = proposal["application"]
        application_status = application["status"]
        if application_status != "applying":
            raise ValueError(
                f"Proposal application is not applying: {proposal_id} "
                f"({application_status})"
            )
        self._set_application_status(proposal, status)
        application["finished_at"] = utc_now_iso()
        application["changed_files"] = changed_files or []
        application["validation"] = validation or {}
        application["error"] = error
        application["runner_responses"] = runner_responses or []
        application["promoted_revision"] = promoted_revision
        application["rollback"] = rollback
        proposal["updated_at"] = utc_now_iso()
        self.write_proposal(job_id, proposal)
        from wayfinder_paths.jobs.remediation import handle_remediation_application

        handle_remediation_application(self, job_id, proposal, status=status)
        self.append_journal(
            job_id,
            {
                "type": "proposal_apply_finished",
                "proposal_id": proposal_id,
                "application_status": status,
                "changed_files": changed_files or [],
                "error": error,
            },
        )
        self.refresh_scorecard(job_id)
        return proposal

    def record_proposal_application_validation(
        self, job_id: str, proposal_id: str, validation: dict[str, Any]
    ) -> dict[str, Any]:
        proposal = self.load_proposal(job_id, proposal_id)
        application = proposal["application"]
        attempts = application.setdefault("validation_attempts", [])
        match attempts:
            case list():
                pass
            case _:
                attempts = []
                application["validation_attempts"] = attempts
        checks = validation.get("checks")
        failed_checks = [
            str(check.get("name")) for check in checks or [] if not check.get("passed")
        ]
        attempts.append(
            {
                "ts": utc_now_iso(),
                "status": str(validation.get("status") or "unknown"),
                "failed_checks": failed_checks,
                "check_count": len(checks or []),
            }
        )
        application["latest_validation"] = validation
        proposal["updated_at"] = utc_now_iso()
        self.write_proposal(job_id, proposal)
        return proposal

    def refresh_scorecard(
        self, job_id: str, updates: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        scorecard = self.read_json(job_id, "scorecard.json", default={}) or {}
        scorecard.setdefault("job_id", safe_job_id(job_id))
        scorecard["updated_at"] = utc_now_iso()
        if updates:
            scorecard.update(updates)
        proposals = self.proposals(job_id)
        scorecard["pending_proposals"] = sum(
            1 for proposal in proposals if proposal["status"] == "pending"
        )
        scorecard["queued_proposal_applications"] = sum(
            1 for proposal in proposals if proposal["application"]["status"] == "queued"
        )
        scorecard["applying_proposal_applications"] = sum(
            1
            for proposal in proposals
            if proposal["application"]["status"] == "applying"
        )
        # circular import: exhaustion annotates via JobStore
        from wayfinder_paths.jobs.exhaustion import list_exhaustion_claims

        scorecard["pending_exhaustion_claims"] = len(
            list_exhaustion_claims(self, job_id, status="pending")
        )
        # circular import: evolution reporting reads through JobStore
        from wayfinder_paths.jobs.evolution_ledger import build_process_efficiency

        scorecard["process_efficiency"] = build_process_efficiency(self, job_id)
        self.write_json(job_id, "scorecard.json", scorecard)
        return scorecard

    def _write_json_if_missing(self, path: Path, data: Any) -> None:
        if not path.exists():
            atomic_write_json(path, data)

    def _write_jsonl_if_missing(self, path: Path) -> None:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def _normalize_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        proposal = dict(proposal)
        status = str(proposal.get("status") or "pending")
        if status not in PROPOSAL_STATUSES:
            status = "pending"
        proposal["status"] = status
        application = dict(proposal.get("application") or {})
        application_status = str(application.get("status") or "not_requested")
        if application_status not in APPLICATION_STATUSES:
            application_status = "not_requested"
        application["status"] = application_status
        proposal["application"] = application
        approval = dict(proposal.get("approval") or {})
        approval.setdefault("required", True)
        approval.setdefault("status", status)
        proposal["approval"] = approval
        proposal.setdefault("intent_contract", {})
        proposal.setdefault("scenario_plan", {"scenarios": []})
        return proposal

    def _set_application_status(
        self, proposal: dict[str, Any], status: ApplicationStatus
    ) -> None:
        application = proposal["application"]
        previous = application["status"]
        application["status"] = status
        if previous != status:
            application.setdefault("transitions", []).append(
                {
                    "from": previous,
                    "to": status,
                    "ts": utc_now_iso(),
                }
            )


def _infer_rejection_kind(reason: str | None) -> str:
    text = (reason or "").lower()
    if any(marker in text for marker in _PROCESS_REJECTION_MARKERS):
        return "process"
    return "substantive"


def _contains_fail_closed_escalate(text: str) -> bool:
    """Whether a failure text carries a fail-closed gate ESCALATE.

    Fail-closed gates that REFUSE TO EVALUATE (governance chain tampered,
    constitution drift, economic evidence unavailable) all prefix their
    message with "ESCALATE:" — see _ensure_governance_gate and
    application.claim_application. A refusal to evaluate is never evidence
    against the change; a gate that evaluated and came back red ("candidate
    is not economic-ready: ...") carries no ESCALATE prefix and still stops
    the line.
    """
    return "escalate:" in (text or "").lower()


def _latest_failure_text(proposal: Mapping[str, Any], reason: str | None) -> str:
    """Best-effort text of the proposal's most recent failure, for the
    infrastructure-vs-evidence classification on agent self-rejections."""
    application = proposal.get("application") or {}
    latest_validation = application.get("latest_validation") or {}
    parts: list[Any] = [
        reason,
        application.get("error"),
        application.get("restage_last_error"),
        latest_validation.get("error"),
        (application.get("validation") or {}).get("error"),
    ]
    for check in latest_validation.get("checks") or []:
        if isinstance(check, dict) and not check.get("passed"):
            parts.extend([check.get("name"), check.get("detail"), check.get("error")])
    summary = (proposal.get("candidate_report") or {}).get("validation_summary") or {}
    parts.extend(summary.get("failed_checks") or [])
    return " | ".join(str(part) for part in parts if part)


def _validate_applicable_proposal(
    proposal: dict[str, Any], *, require_scenarios: bool = True
) -> None:
    contract = proposal["intent_contract"]
    match contract:
        case dict() if contract:
            pass
        case _:
            raise ValueError("Proposal requires intent_contract before application")
    if not require_scenarios:
        # Research-only jobs (no enabled script loop) have nothing to replay
        # scenarios through; demanding them would make their proposals
        # permanently unapprovable.
        return
    scenario_plan = proposal["scenario_plan"]
    scenarios: Any
    match scenario_plan:
        case list():
            scenarios = scenario_plan
        case dict():
            scenarios = scenario_plan.get("scenarios")
        case _:
            scenarios = None
    match scenarios:
        case list() if scenarios:
            pass
        case _:
            raise ValueError(
                "Proposal requires scenario_plan.scenarios before application"
            )
