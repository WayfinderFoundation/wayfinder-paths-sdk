"""Live loose ends from the majors-5m-lab journal audit: client singletons
dying across event loops (counterfactual dark for 5 days), transient venue
fetch blips waking the agent as reconcile mismatches, and live-mode
misconfiguration that predates the operator-stamp guardrail."""

from __future__ import annotations

import asyncio
import json

import httpx

from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient
from wayfinder_paths.jobs.models import WayfinderJob
from wayfinder_paths.jobs.store import JobStore


def test_client_rebuilds_when_event_loop_changes(monkeypatch) -> None:
    built: list[httpx.AsyncClient] = []
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))

    def build(self) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=transport)
        built.append(client)
        return client

    monkeypatch.setattr(WayfinderClient, "_build_client", build)
    client = WayfinderClient()

    async def request() -> None:
        await client._authed_request("GET", "https://example.test/x")

    # Two asyncio.run calls = two distinct loops (the counterfactual seam).
    asyncio.run(request())
    first = client.client
    asyncio.run(request())
    assert client.client is not first  # rebuilt for the new loop

    # Same loop, repeated requests: no churn after the first rebuild.
    async def twice() -> None:
        await client._authed_request("GET", "https://example.test/x")
        before = client.client
        await client._authed_request("GET", "https://example.test/x")
        await client._authed_request("GET", "https://example.test/x")
        assert client.client is before

    asyncio.run(twice())


class _FlakyBroker:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def fetch_state(self, symbols):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("Could not fetch Hyperliquid state")
        from wayfinder_paths.jobs.execution.venues import VenueState

        return VenueState(
            positions={}, open_orders=[], balances={}, source="test", fetched_at=None
        )


def test_reconcile_retries_transient_fetch_failures(monkeypatch) -> None:
    from wayfinder_paths.jobs.execution import driver as driver_module
    from wayfinder_paths.jobs.execution.driver import _reconcile
    from wayfinder_paths.jobs.execution.engine import EngineState

    monkeypatch.setattr(driver_module.asyncio, "sleep", _instant_sleep, raising=False)
    flaky = _FlakyBroker(failures=2)
    snapshot, notes = asyncio.run(
        _reconcile(
            mode="live",
            state=EngineState(),
            brokers={"hyperliquid": flaky},
            symbols=["HYPE"],
            state_file_existed=True,
        )
    )
    assert snapshot.status == "valid"  # two blips absorbed
    assert flaky.calls == 3
    assert not [n for n in notes if n.get("kind") == "reconcile_fetch_failed"]

    dead = _FlakyBroker(failures=99)
    snapshot, notes = asyncio.run(
        _reconcile(
            mode="live",
            state=EngineState(),
            brokers={"hyperliquid": dead},
            symbols=["HYPE"],
            state_file_existed=True,
        )
    )
    assert snapshot.status == "ambiguous"  # persistent failure still bites
    assert dead.calls == 3
    assert notes[0]["kind"] == "reconcile_fetch_failed"


async def _instant_sleep(_seconds: float) -> None:
    return None


def test_watchdog_flags_unstamped_and_walletless_live_mode(tmp_path) -> None:
    from wayfinder_paths.jobs.watchdog import _audit_live_mode

    store = JobStore(repo_root=tmp_path)
    job = WayfinderJob.new(
        "audit-demo",
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    job.script_loop.mode = "live"
    store.save(job)

    event = _audit_live_mode(store, job)
    assert sorted(event["flags"]) == [
        "live_wallet_label_missing",
        "unstamped_live_mode",
    ]
    rows = [
        json.loads(line)
        for line in (store.job_dir(job.id) / "journal.jsonl").read_text().splitlines()
    ]
    audits = [r for r in rows if r["type"] == "live_mode_audit"]
    assert len(audits) == 1

    # Unchanged flags on the next pass: no duplicate journal spam.
    assert _audit_live_mode(store, job) is None
    rows = (store.job_dir(job.id) / "journal.jsonl").read_text().splitlines()
    assert sum('"live_mode_audit"' in line for line in rows) == 1

    # Owner stamp + explicit wallet clears the flags (journaled once as clear).
    store.write_json(
        job.id, "state/operator.json", {"script_mode": {"set_by": "owner"}}
    )
    job.execution_params["wallet_label"] = "majors-wallet"
    store.save(job)
    assert _audit_live_mode(store, job) is None  # cleared -> no recovery event
    rows = (store.job_dir(job.id) / "journal.jsonl").read_text().splitlines()
    cleared = [json.loads(line) for line in rows if '"live_mode_audit"' in line]
    assert cleared[-1]["cleared"] is True

    # Paper jobs never flag.
    paper = WayfinderJob.new(
        "paper-demo",
        script="workspace/src/strategy.py",
        agent_mode="intervene",
        execution_contract="jobs_v1",
    )
    store.save(paper)
    assert _audit_live_mode(store, paper) is None
