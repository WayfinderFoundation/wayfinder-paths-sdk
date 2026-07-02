from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from wayfinder_paths.core.config import (
    get_api_base_url,
    get_api_key,
    get_opencode_instance_id,
    is_opencode_instance,
)
from wayfinder_paths.jobs.backtest_artifacts import summarize_backtest_artifacts
from wayfinder_paths.jobs.forward import load_forward_snapshot
from wayfinder_paths.jobs.gating import evaluate_live_gate
from wayfinder_paths.jobs.store import JobStore


class WayfinderJobsClient:
    def __init__(self) -> None:
        self._client = httpx.Client(timeout=httpx.Timeout(10), follow_redirects=True)

    def _base_url(self) -> str | None:
        if not is_opencode_instance():
            return None
        instance_id = get_opencode_instance_id()
        if not instance_id:
            return None
        return f"{get_api_base_url()}/opencode/instances/{instance_id}/wayfinder-jobs"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = get_api_key()
        if api_key:
            headers["X-API-KEY"] = api_key
        return headers

    def sync(self, jobs: list[dict[str, Any]]) -> None:
        base_url = self._base_url()
        if not base_url:
            return
        try:
            resp = self._client.post(
                f"{base_url}/sync/",
                json={"jobs": jobs},
                headers=self._headers(),
            )
            resp.raise_for_status()
        except Exception:
            logger.opt(exception=True).warning(
                "Failed to sync Wayfinder jobs to backend"
            )


WAYFINDER_JOBS_CLIENT = WayfinderJobsClient()


def snapshot_job(job_id: str, *, store: JobStore | None = None) -> dict[str, Any]:
    store = store or JobStore()
    job = store.load(job_id)
    scorecard = store.read_json(job_id, "scorecard.json", default={}) or {}
    runner_links = store.read_json(job_id, "runner_links.json", default={}) or {}
    latest_monitor = store.read_json(
        job_id, "reports/monitor/latest.json", default=None
    )
    latest_intervene = store.read_json(
        job_id,
        "reports/intervene/latest.json",
        default=store.read_json(job_id, "reports/improve/latest.json", default=None),
    )
    latest_auto = store.read_json(
        job_id,
        "reports/auto/latest.json",
        default=store.read_json(job_id, "reports/decide/latest.json", default=None),
    )
    validation = (
        store.read_json(job_id, "reports/validation/latest.json", default={}) or {}
    )
    return {
        "job": job.to_dict(),
        "scorecard": scorecard,
        "backtest": summarize_backtest_artifacts(job_id, store=store),
        "forward": load_forward_snapshot(job_id, store=store, limit=25),
        "runner_links": runner_links,
        "proposals": store.proposals(job_id),
        "proposal_queue": store.proposal_queue(job_id),
        "reports": {
            "monitor": latest_monitor,
            "intervene": latest_intervene,
            "auto": latest_auto,
            "reconcile": store.read_json(
                job_id, "reports/reconcile/latest.json", default=None
            ),
        },
        "execution_contract": job.execution_contract,
        "validation": _bounded_validation(validation),
        "gate": evaluate_live_gate(job_id, store=store),
    }


def _bounded_validation(report: dict[str, Any]) -> dict[str, Any]:
    if not report:
        return {}
    return {
        "status": report.get("status"),
        "revision": report.get("revision"),
        "failed_checks": [
            check.get("name")
            for check in report.get("checks") or []
            if not check.get("passed")
        ],
    }


def sync_all_jobs(*, store: JobStore | None = None) -> None:
    store = store or JobStore()
    snapshots = [snapshot_job(job.id, store=store) for job in store.list_jobs()]
    WAYFINDER_JOBS_CLIENT.sync(snapshots)
