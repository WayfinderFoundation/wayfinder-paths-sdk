from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from wayfinder_paths.core.config import (
    get_api_base_url,
    get_api_key,
    get_opencode_instance_id,
)


class ScheduledJobsClient:
    """Sync HTTP client for pushing job/run data to vault-backend."""

    def __init__(self) -> None:
        self._client = httpx.Client(timeout=httpx.Timeout(10), follow_redirects=True)

    def _base_url(self) -> str:
        return (
            f"{get_api_base_url()}/opencode/instances/{get_opencode_instance_id()}/jobs"
        )

    def _headers(self) -> dict[str, str]:
        hdrs: dict[str, str] = {"Content-Type": "application/json"}
        api_key = get_api_key()
        if api_key:
            hdrs["X-API-KEY"] = api_key
        return hdrs

    def bulk_sync(self, jobs: list[dict[str, Any]]) -> None:
        try:
            resp = self._client.post(
                f"{self._base_url()}/sync/",
                json={"jobs": jobs},
                headers=self._headers(),
            )
            resp.raise_for_status()
        except Exception:
            logger.opt(exception=True).warning("Failed to bulk-sync jobs to backend")

    def report_run(self, job_name: str, run_data: dict[str, Any]) -> None:
        try:
            resp = self._client.post(
                f"{self._base_url()}/{job_name}/runs/",
                json=run_data,
                headers=self._headers(),
            )
            resp.raise_for_status()
        except Exception:
            logger.opt(exception=True).warning(
                f"Failed to report run for {job_name} to backend"
            )


SCHEDULED_JOBS_CLIENT = ScheduledJobsClient()
