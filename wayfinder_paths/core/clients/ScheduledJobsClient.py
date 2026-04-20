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
    """Sync client for pushing job/run data to vault-backend. Used by the runner daemon."""

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

    def sync_job(self, job_name: str, data: dict[str, Any]) -> None:
        try:
            self._client.put(
                f"{self._base_url()}/{job_name}/", json=data, headers=self._headers()
            )
        except Exception:
            logger.opt(exception=True).warning(
                f"Failed to sync job {job_name} to backend"
            )

    def delete_job(self, job_name: str) -> None:
        try:
            self._client.delete(
                f"{self._base_url()}/{job_name}/", headers=self._headers()
            )
        except Exception:
            logger.opt(exception=True).warning(
                f"Failed to delete job {job_name} from backend"
            )

    def report_run(self, job_name: str, run_data: dict[str, Any]) -> None:
        try:
            self._client.post(
                f"{self._base_url()}/{job_name}/runs/",
                json=run_data,
                headers=self._headers(),
            )
        except Exception:
            logger.opt(exception=True).warning(
                f"Failed to report run for {job_name} to backend"
            )


SCHEDULED_JOBS_CLIENT = ScheduledJobsClient()
