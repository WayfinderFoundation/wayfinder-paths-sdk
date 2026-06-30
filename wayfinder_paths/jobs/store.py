from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.forward import default_forward_summary
from wayfinder_paths.jobs.models import (
    ApplicationStatus,
    WayfinderJob,
    safe_job_id,
    utc_now_iso,
)
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


class JobStore:
    def __init__(self, *, repo_root: Path | None = None) -> None:
        self.repo_root = (repo_root or find_repo_root()).resolve()
        self.jobs_dir = self.repo_root / ".wayfinder" / "jobs"
        self.runs_jobs_dir = self.repo_root / ".wayfinder_runs" / "jobs"

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / safe_job_id(job_id)

    def job_yaml_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.yaml"

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
            "sessions",
        ]:
            (root / relative).mkdir(parents=True, exist_ok=True)
        self.runs_jobs_dir.mkdir(parents=True, exist_ok=True)
        self._write_if_missing(root / "memory.md", self._default_memory(job))
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
            default_forward_summary(job.id),
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

    def load(self, job_id: str) -> WayfinderJob:
        path = self.job_yaml_path(job_id)
        if not path.exists():
            raise FileNotFoundError(f"Wayfinder job not found: {safe_job_id(job_id)}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"Invalid job spec: {path}")
        return WayfinderJob.from_dict(data)

    def list_jobs(self) -> list[WayfinderJob]:
        if not self.jobs_dir.exists():
            return []
        jobs: list[WayfinderJob] = []
        for path in sorted(self.jobs_dir.glob("*/job.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return path

    def append_journal(self, job_id: str, event: dict[str, Any]) -> None:
        path = self.job_dir(job_id) / "journal.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.open("a", encoding="utf-8").write(
            json.dumps({"ts": utc_now_iso(), **event}, sort_keys=True) + "\n"
        )

    def proposal_files(self, job_id: str) -> list[Path]:
        return sorted((self.job_dir(job_id) / "proposals").glob("*.json"))

    def proposals(self, job_id: str) -> list[dict[str, Any]]:
        proposals: list[dict[str, Any]] = []
        for path in self.proposal_files(job_id):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
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
            status = proposal.get("status")
            application_status = (proposal.get("application") or {}).get("status")
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

    def set_proposal_status(
        self, job_id: str, proposal_id: str, status: str
    ) -> dict[str, Any]:
        if status not in PROPOSAL_STATUSES:
            raise ValueError(f"Invalid proposal status: {status}")
        data = self.load_proposal(job_id, proposal_id)
        data["status"] = status
        data.setdefault("approval", {})["status"] = status
        data["updated_at"] = utc_now_iso()
        self.write_proposal(job_id, data)
        self.refresh_scorecard(job_id)
        return data

    def load_proposal(self, job_id: str, proposal_id: str) -> dict[str, Any]:
        path = self._proposal_path(job_id, proposal_id)
        if not path.exists():
            raise FileNotFoundError(f"Proposal not found: {proposal_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Invalid proposal: {proposal_id}")
        return self._normalize_proposal(data)

    def write_proposal(self, job_id: str, proposal: dict[str, Any]) -> Path:
        proposal = self._normalize_proposal(proposal)
        proposal_id = str(proposal.get("proposal_id") or "").strip()
        if not proposal_id:
            raise ValueError("proposal_id is required")
        path = self._proposal_path(job_id, proposal_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(proposal, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def approve_proposal(self, job_id: str, proposal_id: str) -> dict[str, Any]:
        proposal = self.load_proposal(job_id, proposal_id)
        _validate_applicable_proposal(proposal)
        application = proposal.setdefault("application", {})
        application_status = str(application.get("status") or "not_requested")
        if proposal.get("status") == "rejected":
            raise ValueError(f"Rejected proposal cannot be approved: {proposal_id}")
        if application_status == "applying":
            raise ValueError(f"Proposal is already applying: {proposal_id}")
        if application_status == "applied":
            return proposal
        proposal["status"] = "approved"
        proposal.setdefault("approval", {})["status"] = "approved"
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
        _validate_applicable_proposal(proposal)
        application_status = str(
            (proposal.get("application") or {}).get("status") or "not_requested"
        )
        if proposal.get("status") != "approved":
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

    def reject_proposal(self, job_id: str, proposal_id: str) -> dict[str, Any]:
        proposal = self.load_proposal(job_id, proposal_id)
        application = proposal.setdefault("application", {})
        application_status = str(application.get("status") or "not_requested")
        if application_status in {"applying", "applied"}:
            raise ValueError(
                f"Cannot reject proposal with application status {application_status}: "
                f"{proposal_id}"
            )
        proposal["status"] = "rejected"
        proposal.setdefault("approval", {})["status"] = "rejected"
        if application_status == "queued":
            self._set_application_status(proposal, "canceled")
        proposal["updated_at"] = utc_now_iso()
        self.write_proposal(job_id, proposal)
        self.append_journal(
            job_id,
            {
                "type": "proposal_rejected",
                "proposal_id": proposal_id,
                "application_status": (proposal.get("application") or {}).get("status"),
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
        proposal = self.load_proposal(job_id, proposal_id)
        application_status = str(
            (proposal.get("application") or {}).get("status") or "not_requested"
        )
        if proposal.get("status") != "approved":
            raise ValueError(f"Proposal is not approved: {proposal_id}")
        if application_status not in {"queued", "failed"}:
            raise ValueError(
                f"Proposal application is not queued: {proposal_id} ({application_status})"
            )
        application = proposal.setdefault("application", {})
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
        application = proposal.setdefault("application", {})
        application_status = str(application.get("status") or "not_requested")
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
        application = proposal.setdefault("application", {})
        attempts = application.setdefault("validation_attempts", [])
        if not isinstance(attempts, list):
            attempts = []
            application["validation_attempts"] = attempts
        checks = validation.get("checks") if isinstance(validation, dict) else []
        failed_checks = [
            str(check.get("name"))
            for check in checks or []
            if isinstance(check, dict) and not check.get("passed")
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
        scorecard["pending_proposals"] = sum(
            1
            for proposal in self.proposals(job_id)
            if proposal.get("status") == "pending"
        )
        scorecard["queued_proposal_applications"] = sum(
            1
            for proposal in self.proposals(job_id)
            if (proposal.get("application") or {}).get("status") == "queued"
        )
        scorecard["applying_proposal_applications"] = sum(
            1
            for proposal in self.proposals(job_id)
            if (proposal.get("application") or {}).get("status") == "applying"
        )
        self.write_json(job_id, "scorecard.json", scorecard)
        return scorecard

    def _default_memory(self, job: WayfinderJob) -> str:
        return (
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
            "- None yet.\n"
        )

    def _write_if_missing(self, path: Path, text: str) -> None:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def _write_json_if_missing(self, path: Path, data: Any) -> None:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )

    def _write_jsonl_if_missing(self, path: Path) -> None:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")

    def _proposal_path(self, job_id: str, proposal_id: str) -> Path:
        return self.job_dir(job_id) / "proposals" / f"{proposal_id}.json"

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
        application = proposal.setdefault("application", {})
        previous = application.get("status")
        application["status"] = status
        if previous != status:
            application.setdefault("transitions", []).append(
                {
                    "from": previous or "not_requested",
                    "to": status,
                    "ts": utc_now_iso(),
                }
            )


def _validate_applicable_proposal(proposal: dict[str, Any]) -> None:
    contract = proposal.get("intent_contract")
    if not isinstance(contract, dict) or not contract:
        raise ValueError("Proposal requires intent_contract before application")
    scenario_plan = proposal.get("scenario_plan")
    scenarios = (
        scenario_plan
        if isinstance(scenario_plan, list)
        else (scenario_plan or {}).get("scenarios")
        if isinstance(scenario_plan, dict)
        else None
    )
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("Proposal requires scenario_plan.scenarios before application")
