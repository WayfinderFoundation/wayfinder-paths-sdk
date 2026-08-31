"""Live loose ends from the majors-5m-lab journal audit: client singletons
dying across event loops (counterfactual dark for 5 days), transient venue
fetch blips waking the agent as reconcile mismatches, and live-mode
misconfiguration that predates the operator-stamp guardrail."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import httpx
import yaml

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


def test_owner_stamp_launder_trap_and_guards(tmp_path) -> None:
    from pathlib import Path

    from wayfinder_paths.jobs.benchmarks.stress import TRAPS

    outcome = TRAPS["owner_stamp_launder"](tmp_path)
    assert outcome["held"] is True, outcome

    # Defense-in-depth guards stay in place: the manifest denies owner
    # provenance claims and the wake prompt forbids them.
    manifest = Path(".opencode/agents/wayfinder-job-worker.md").read_text()
    assert '"*--by owner*": deny' in manifest
    from wayfinder_paths.jobs import worker as worker_module

    source = Path(worker_module.__file__).read_text()
    assert "OWNER PROVENANCE IS NEVER YOURS TO CLAIM" in source


AGENT_MANIFESTS = [
    ".opencode/agents/wayfinder-job-worker.md",
    ".opencode/agents/wayfinder-job-auto-worker.md",
    ".opencode/agents/wayfinder-strategy-lab.md",
]

# ORDER IS LOAD-BEARING (see test_external_directory_matcher_semantics for
# the ported opencode evaluator that proves it): opencode resolves rules
# last-match-wins over YAML insertion order, so the "*" catch-all deny must
# come FIRST (every later allow overrides it) and the governance deny LAST
# (it wins over any overlapping allow ever added above it).
EXPECTED_EXTERNAL_DIRECTORY = [
    ("*", "deny"),
    ("/wf/user_vault/wayfinder/**", "allow"),
    ("/wf/user_vault/scripts/**", "allow"),
    ("/wf/user_vault/exports/**", "allow"),
    ("/wf/user_vault/governance/**", "deny"),
]


def _external_directory_map(manifest_path: str) -> dict[str, str]:
    manifest = Path(manifest_path).read_text()
    frontmatter = yaml.safe_load(manifest.split("---\n")[1])
    return frontmatter["permission"]["external_directory"]


def test_external_directory_grants_cover_vault_deny_governance_and_catch_all() -> None:
    """opencode 1.18+ resolves the .wayfinder / .wayfinder_runs symlinks to
    /wf/user_vault/** and gates reads/writes behind the external_directory
    permission (default "ask" — an unanswerable prompt on headless wakes).
    The job agents must carry narrow allows for exactly the vault trees their
    edit grants already imply, keep the governance plane fail-closed with an
    explicit deny (never absorbed by a broad /wf/user_vault/** allow), and
    carry a "*" catch-all deny: on a headless worker ask == hang, so a path
    mistake outside the vault (observed: ../../workspace from the repo root
    resolving to /workspace) must fail fast with a legible error the agent can
    self-correct in the same wake.

    opencode's evaluator is LAST-MATCH-WINS over YAML insertion order — NOT
    specificity-based (packages/opencode/src/permission/index.ts evaluate()
    uses findLast; fromConfig() emits rules in Object.entries order). The
    catch-all therefore must be the FIRST key: when it was appended last
    (PR #681) it matched every request and silently denied all vault writes.
    This test pins the exact ordered items, not just the mapping — YAML load
    preserves key order into the dict, mirroring opencode's parse."""
    for path in AGENT_MANIFESTS:
        mapping = _external_directory_map(path)
        expected = list(EXPECTED_EXTERNAL_DIRECTORY)
        if path.endswith("wayfinder-job-worker.md"):
            expected.append(("/wf/user_vault/audit/**", "deny"))
        assert list(mapping.items()) == expected, path
        # A wholesale vault grant would cover governance/ and gut the
        # capability boundary — the allows must stay enumerated.
        assert '"/wf/user_vault/**"' not in Path(path).read_text(), path

    # The edit-layer governance deny is a separate, conjunctive check —
    # external_directory grants must not be mistaken for a reason to drop it.
    worker = Path(".opencode/agents/wayfinder-job-worker.md").read_text()
    assert '"governance/**": deny' in worker


def test_evolution_worker_has_minimal_vault_access_and_denies_audit() -> None:
    path = ".opencode/agents/wayfinder-evolution-worker.md"
    frontmatter = yaml.safe_load(Path(path).read_text().split("---\n")[1])
    mapping = _external_directory_map(path)
    assert list(mapping.items()) == [
        ("*", "deny"),
        ("/wf/user_vault/wayfinder/**", "allow"),
        ("/wf/user_vault/governance/**", "deny"),
        ("/wf/user_vault/audit/**", "deny"),
    ]
    manifest = Path(path).read_text()
    assert '"governance/**": deny' in manifest
    assert '"audit/**": deny' in manifest
    assert frontmatter["permission"]["read"] == {
        "*": "deny",
        ".wayfinder/jobs/**": "allow",
        "/wf/user_vault/wayfinder/jobs/**": "allow",
        "wf/sdk/.wayfinder/jobs/**": "allow",
        "wf/user_vault/wayfinder/jobs/**": "allow",
    }
    assert frontmatter["permission"]["write"] == frontmatter["permission"]["read"]
    assert list(frontmatter["permission"]["edit"].items()) == [
        ("*", "deny"),
        (".wayfinder/jobs/**", "allow"),
        ("wf/sdk/.wayfinder/jobs/**", "allow"),
        ("wf/user_vault/wayfinder/jobs/**", "allow"),
        ("governance/**", "deny"),
        ("audit/**", "deny"),
    ]
    assert frontmatter["permission"]["bash"] == "deny"
    ruleset = _from_config(frontmatter["permission"])
    assert _evaluate("wayfinder_core_jobs", "*", ruleset)["action"] == "allow"
    assert (
        _evaluate("wayfinder_research_search_delta_lab_assets", "*", ruleset)["action"]
        == "allow"
    )
    for denied in (
        "wayfinder_core_run_script",
        "wayfinder_core_runner",
        "wayfinder_hyperliquid_place_market_order",
        "wayfinder_contracts_execute",
    ):
        assert _evaluate(denied, "*", ruleset)["action"] == "deny"


def test_evolution_designer_is_read_only_and_job_scoped() -> None:
    path = ".opencode/agents/wayfinder-evolution-designer.md"
    frontmatter = yaml.safe_load(Path(path).read_text().split("---\n")[1])
    permission = frontmatter["permission"]

    assert frontmatter["temperature"] == 0.5
    assert permission["read"] == {
        "*": "deny",
        ".wayfinder/jobs/**": "allow",
        "/wf/user_vault/wayfinder/jobs/**": "allow",
        "wf/sdk/.wayfinder/jobs/**": "allow",
        "wf/user_vault/wayfinder/jobs/**": "allow",
    }
    assert permission["write"] == "deny"
    assert permission["edit"] == "deny"
    assert permission["bash"] == "deny"
    assert permission["wayfinder_core_jobs"] == "allow"


def test_evolution_worker_can_mutate_candidate_from_global_project() -> None:
    """The production image has no git metadata, so opencode's worktree is `/`.

    Read and edit permissions receive paths relative to that worktree, not the
    absolute paths passed to the tools. Cover both names the worker uses for a
    candidate: the SDK's `.wayfinder` symlink and its canonical vault target.
    """
    path = ".opencode/agents/wayfinder-evolution-worker.md"
    frontmatter = yaml.safe_load(Path(path).read_text().split("---\n")[1])
    permission = frontmatter["permission"]
    candidates = (
        "wf/sdk/.wayfinder/jobs/majors-5m-lab/research/evolution/"
        "campaigns/campaign-1/candidates/candidate-1/workspace/src/strategy.py",
        "wf/user_vault/wayfinder/jobs/majors-5m-lab/research/evolution/"
        "campaigns/campaign-1/candidates/candidate-1/workspace/src/strategy.py",
    )

    for operation in ("read", "edit"):
        ruleset = _from_config({operation: permission[operation]})
        for candidate in candidates:
            assert _evaluate(operation, candidate, ruleset)["action"] == "allow"
        assert _evaluate(operation, "wf/sdk/AGENTS.md", ruleset)["action"] == "deny"
        assert (
            _evaluate(
                operation,
                "wf/user_vault/audit/evolution/private.json",
                ruleset,
            )["action"]
            == "deny"
        )


def _wildcard_match(string: str, pattern: str) -> bool:
    """Faithful port of opencode's Wildcard.match
    (packages/opencode/src/util/wildcard.ts L3-L19 @ v1.18.18, commit
    31406ccc): escape regex specials EXCEPT * and ?, then * -> ".*"
    (crosses slashes; "**" has no special meaning — it compiles to
    ".*.*" == ".*"), ? -> ".", anchored ^...$ with dotall."""
    string = string.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    escaped = re.sub(r"[.+^${}()|[\]\\]", lambda m: "\\" + m.group(0), pattern)
    escaped = escaped.replace("*", ".*").replace("?", ".")
    # "ls *" also matches bare "ls" (trailing-arg wildcard optionality)
    if escaped.endswith(" .*"):
        escaped = escaped[:-3] + "( .*)?"
    return re.match("^" + escaped + "$", string, flags=re.DOTALL) is not None


def _evaluate(
    permission: str, pattern: str, ruleset: list[dict[str, str]]
) -> dict[str, str]:
    """Faithful port of opencode's Permission.evaluate
    (packages/opencode/src/permission/index.ts L28-L38 @ v1.18.18):
    rulesets.flat().findLast(...) — the LAST matching rule wins, with an
    {action: "ask", pattern: "*"} fallback when nothing matches."""
    winner = None
    for rule in ruleset:
        if _wildcard_match(permission, rule["permission"]) and _wildcard_match(
            pattern, rule["pattern"]
        ):
            winner = rule
    return winner or {"permission": permission, "pattern": "*", "action": "ask"}


def _from_config(
    permission_config: dict[str, dict[str, str] | str],
) -> list[dict[str, str]]:
    """Faithful port of opencode's Permission.fromConfig
    (packages/opencode/src/permission/index.ts L186-L198 @ v1.18.18):
    Object.entries insertion order becomes ruleset order ($HOME/~ expansion
    omitted — no such patterns in the agent manifests)."""
    ruleset: list[dict[str, str]] = []
    for key, value in permission_config.items():
        if isinstance(value, str):
            ruleset.append({"permission": key, "pattern": "*", "action": value})
            continue
        for pattern, action in value.items():
            ruleset.append({"permission": key, "pattern": pattern, "action": action})
    return ruleset


def test_external_directory_matcher_semantics() -> None:
    """Run the actual manifest maps through a faithful Python port of
    opencode v1.18.18's matcher and pin the outcomes. Requests are shaped
    exactly as opencode builds them: join(parentDir, "*") — a literal "*"
    suffix (packages/opencode/src/tool/external-directory.ts L29-L37)."""
    for path in AGENT_MANIFESTS:
        ruleset = _from_config({"external_directory": _external_directory_map(path)})

        def action(request: str, ruleset: list[dict[str, str]] = ruleset) -> str:
            return _evaluate("external_directory", request, ruleset)["action"]

        # Job-dir writes (the production breakage under PR #681) → allow.
        assert action("/wf/user_vault/wayfinder/jobs/majors-5m-lab/*") == "allow", path
        assert (
            action("/wf/user_vault/wayfinder/jobs/majors-5m-lab/research/*") == "allow"
        ), path
        assert action("/wf/user_vault/wayfinder/runner/*") == "allow", path
        assert action("/wf/user_vault/scripts/.scratch/session/*") == "allow", path
        assert action("/wf/user_vault/exports/*") == "allow", path
        # Governance plane → deny, at any depth.
        assert action("/wf/user_vault/governance/*") == "deny", path
        assert action("/wf/user_vault/governance/owner_targets/*") == "deny", path
        # Unknown external paths → deny FAST via the catch-all (no ask-hang).
        assert action("/workspace/*") == "deny", path
        assert action("/etc/*") == "deny", path
        # Sibling-prefix escapes do not ride the allow ("wayfinder_evil").
        assert action("/wf/user_vault/wayfinder_evil/*") == "deny", path

        # Regression pin: the PR #681 ordering (catch-all LAST) denies the
        # job dir under last-match-wins — the exact production failure.
        broken = _from_config(
            {
                "external_directory": {
                    "/wf/user_vault/wayfinder/**": "allow",
                    "/wf/user_vault/scripts/**": "allow",
                    "/wf/user_vault/exports/**": "allow",
                    "/wf/user_vault/governance/**": "deny",
                    "*": "deny",
                }
            }
        )
        assert (
            _evaluate(
                "external_directory",
                "/wf/user_vault/wayfinder/jobs/majors-5m-lab/*",
                broken,
            )["action"]
            == "deny"
        )
