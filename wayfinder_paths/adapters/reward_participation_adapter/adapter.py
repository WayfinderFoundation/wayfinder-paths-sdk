import time
from typing import Any

import httpx

from wayfinder_paths.core.adapters.BaseAdapter import BaseAdapter
from wayfinder_paths.core.clients.ParticipationReadClient import ParticipationReadClient
from wayfinder_paths.jobs.participation import (
    Observation,
    ParticipationConfig,
    Reward,
    WorkItem,
)

REVIEW_REVISION = "research-2026-10-01"
SOURCES = {
    "flop": "https://flop.finance/intro/agent/",
    "risex": "https://developer.rise.trade/reference/pointsservice_getwalletpoints",
    "perptools": "https://docs.perptools.ai/docs/points/overview",
    "imd": "https://imd.fun/docs/",
}


class RewardParticipationAdapter(BaseAdapter):
    adapter_type: str = "REWARD_PARTICIPATION"

    # Deliberately a class capability, not a user-editable enable flag. No write
    # adapter is shipped until signing, fill/nonce recovery and hedging are tested.
    supports_submit = False

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        client: ParticipationReadClient | None = None,
        risex_token: str | None = None,
        imd_seat_id: int | None = None,
    ) -> None:
        super().__init__("reward_participation_adapter", config)
        self.client = client or ParticipationReadClient()
        self.risex_token = risex_token
        self.imd_seat_id = imd_seat_id

    async def close(self) -> None:
        await self.client.close()

    async def observe(self) -> tuple[bool, dict[str, Any] | str]:
        cfg = ParticipationConfig.model_validate(self.config)
        observed = Observation(
            protocol=cfg.protocol,
            program=cfg.program,
            rule_revision=REVIEW_REVISION,
            observed_at=time.time(),
            readiness="blocked",
            evidence=[SOURCES[cfg.protocol]],
        )
        try:
            if cfg.protocol == "flop":
                observed.reason = "Published draft only: verify deployed agent API, models, faucet, settlement and reward eligibility before activation"
            elif cfg.protocol == "perptools":
                observed.reason = "Verify Orderly broker attribution and EVM wallet-to-points mapping; points are not Tickets. No AI Arena deposits"
            elif cfg.protocol == "risex":
                network = await self.client.risex_config()
                observed.metrics["chain_id"] = network["chain"]["chain_id"]
                observed.reason = "Read-only: signed order/nonce recovery and cross-venue hedge integration not enabled"
                if self.risex_token and cfg.account:
                    points = await self.client.risex_points(
                        cfg.account, token=self.risex_token
                    )
                    history = await self.client.risex_points_history(
                        cfg.account, token=self.risex_token
                    )
                    fees = await self.client.risex_fees(token=self.risex_token)
                    observed.rewards = [
                        Reward(
                            unit="RISE_POINTS",
                            amount=points["total_points"],
                            status="confirmed",
                            scope="lifetime",
                            observed_at=observed.observed_at,
                            evidence=f"https://api.rise.trade/v1/points/{cfg.account}",
                        )
                    ]
                    observed.metrics.update(
                        {
                            "maker_bps": fees["maker_bps"],
                            "taker_bps": fees["taker_bps"],
                            "distributions": len(history.get("entries", [])),
                        }
                    )
                    observed.metric_units.update(
                        maker_bps="bps", taker_bps="bps", distributions="distributions"
                    )
                else:
                    observed.reason += (
                        "; supply account-scoped RISEX_JWT for points and actual fees"
                    )
                observed.readiness = "observe_only"
            else:
                version = await self.client.imd_version()
                observed.metrics["deployment"] = str(version["commit"])
                observed.reason = "Read-only: no seat purchase, paid requests, device pairing or worker execution"
                if self.imd_seat_id is not None and cfg.account:
                    seat = await self.client.imd_seat(
                        self.imd_seat_id, account=cfg.account
                    )
                    for key in (
                        "attempts",
                        "accepted",
                        "rejected",
                        "failed",
                        "pending",
                    ):
                        observed.metrics[key] = seat.get(key)
                        observed.metric_units[key] = "jobs"
                    earnings = await self.client.imd_earnings(cfg.account)
                    observed.metrics["earnings_rows_latest_page"] = earnings.get(
                        "count"
                    )
                    observed.evidence.append(
                        f"https://api.imd.fun/wallets/{cfg.account}/earnings"
                    )
                    # The earnings schema includes token allocations, not a USD
                    # balance. Leave rewards unknown until settlement is verified.
                observed.readiness = "observe_only"
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            # No response bodies/headers or credential-bearing exceptions in logs.
            return (
                False,
                f"{cfg.protocol} observation unavailable ({type(exc).__name__})",
            )
        return True, observed.model_dump(mode="json")

    async def protect(self, *, dry_run: bool) -> tuple[bool, str]:
        # This adapter never creates a position. It is NOT a live trading
        # watchdog; do not attach externally-opened positions to this adapter.
        return True, "read-only adapter; no managed exposure"

    async def submit(self, item: WorkItem, *, operation_id: str) -> tuple[bool, str]:
        raise NotImplementedError("Live reward participation is not enabled")

    async def reconcile(
        self, *, operation_id: str, request_id: str | None
    ) -> tuple[bool, str]:
        return (
            False,
            "No verified submission interface; operator reconciliation required",
        )
