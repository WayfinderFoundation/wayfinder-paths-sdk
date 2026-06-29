from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.jobs.models import WayfinderJob, safe_job_id, utc_now_iso
from wayfinder_paths.runner.paths import find_repo_root


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
            "reports/monitor",
            "reports/improve",
            "reports/decide",
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
        path.write_text(yaml.safe_dump(job.to_dict(), sort_keys=False), encoding="utf-8")
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
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
                proposals.append(data)
        return proposals

    def set_proposal_status(self, job_id: str, proposal_id: str, status: str) -> dict[str, Any]:
        path = self.job_dir(job_id) / "proposals" / f"{proposal_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Proposal not found: {proposal_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = status
        data.setdefault("approval", {})["status"] = status
        data["updated_at"] = utc_now_iso()
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.refresh_scorecard(job_id)
        return data

    def refresh_scorecard(self, job_id: str, updates: dict[str, Any] | None = None) -> dict[str, Any]:
        scorecard = self.read_json(job_id, "scorecard.json", default={}) or {}
        scorecard.setdefault("job_id", safe_job_id(job_id))
        scorecard["updated_at"] = utc_now_iso()
        if updates:
            scorecard.update(updates)
        scorecard["pending_proposals"] = sum(
            1 for proposal in self.proposals(job_id) if proposal.get("status") == "pending"
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
            "- Agent changes require user approval before activation.\n\n"
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
