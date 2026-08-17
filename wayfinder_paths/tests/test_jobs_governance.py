"""Protected governance plane: the constitution split into four owner-owned
files OUTSIDE the agent-writable job tree, with a hash-chained epoch record
so uncommitted drift (the agent-side tamper path) is detectable. The legacy
job-root constitution.yaml keeps working until a job is migrated."""

from __future__ import annotations

import json

from wayfinder_paths.jobs.constitution import DEFAULT_CONSTITUTION, load_constitution
from wayfinder_paths.jobs.governance import (
    AUDIT_POLICY_FILE,
    HARD_CONSTRAINTS_FILE,
    commit_epoch,
    governance_dir,
    load_governance,
    migrate_from_constitution,
    verify_chain,
)


def _legacy_job(tmp_path, constitution_text: str | None):
    job_root = tmp_path / ".wayfinder" / "jobs" / "gov-demo"
    job_root.mkdir(parents=True)
    if constitution_text is not None:
        (job_root / "constitution.yaml").write_text(constitution_text)
    return job_root


def test_migration_splits_and_composes_identically(tmp_path) -> None:
    legacy = (
        "enforcement: blocking\n"
        "objective:\n  weights:\n    downside: 0.7\n"
        "promotion:\n  min_oos_trades: 12\n  audit_min_delta_utility: 0.0\n"
        "hard_constraints:\n  max_drawdown_pct: 0.18\n"
        "verdict:\n  minimum_days: 2.0\n"
    )
    job_root = _legacy_job(tmp_path, legacy)
    before = load_constitution(job_root)

    result = migrate_from_constitution(tmp_path, "gov-demo", job_root)
    assert result["epoch"] == 0
    assert not result["warnings"]  # audit floor not negative in this fixture

    after = load_constitution(job_root)  # facade now prefers governance/
    assert after["source"] == "governance"
    # Composition preserves every consumed field of the legacy load.
    for key in (
        "enforcement",
        "objective",
        "evaluation",
        "promotion",
        "hard_constraints",
        "verdict",
    ):
        assert after[key] == before[key], key
    assert after["governance"]["chain_status"] == "verified"
    assert after["governance"]["revisions"][HARD_CONSTRAINTS_FILE] is not None


def test_migration_warns_on_negative_audit_floor(tmp_path) -> None:
    job_root = _legacy_job(tmp_path, "promotion:\n  audit_min_delta_utility: -0.005\n")
    result = migrate_from_constitution(tmp_path, "gov-demo", job_root)
    assert any("negative" in w for w in result["warnings"])


def test_uncommitted_drift_reads_as_tampered(tmp_path) -> None:
    job_root = _legacy_job(tmp_path, "enforcement: blocking\n")
    migrate_from_constitution(tmp_path, "gov-demo", job_root)
    gov = governance_dir(tmp_path, "gov-demo")

    # Agent-style edit: loosen a ceiling with no epoch commit.
    (gov / HARD_CONSTRAINTS_FILE).write_text("max_drawdown_pct: 0.99\n")
    doc = load_governance(tmp_path, "gov-demo")
    assert doc["governance"]["chain_status"] == "tampered"

    # Owner path: commit -> verified again, ceiling change now on record.
    commit_epoch(gov, note="owner change")
    doc = load_governance(tmp_path, "gov-demo")
    assert doc["governance"]["chain_status"] == "verified"
    assert doc["hard_constraints"]["max_drawdown_pct"] == 0.99
    status, epochs = verify_chain(gov)
    assert (status, epochs) == ("verified", 2)


def test_chain_linkage_break_is_tampered(tmp_path) -> None:
    job_root = _legacy_job(tmp_path, "enforcement: blocking\n")
    migrate_from_constitution(tmp_path, "gov-demo", job_root)
    gov = governance_dir(tmp_path, "gov-demo")
    commit_epoch(gov, note="second epoch")

    epochs_path = gov / "epochs.jsonl"
    rows = [json.loads(line) for line in epochs_path.read_text().splitlines()]
    rows[0]["note"] = "history rewritten"  # breaks the next row's prev hash
    epochs_path.write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n"
    )
    status, _ = verify_chain(gov)
    assert status == "tampered"


def test_legacy_and_default_paths_unchanged(tmp_path) -> None:
    # No governance dir: legacy file loads exactly as before.
    job_root = _legacy_job(tmp_path, "enforcement: blocking\n")
    doc = load_constitution(job_root)
    assert doc["source"] == "file"
    assert doc["enforcement"] == "blocking"

    # Neither governance nor legacy: defaults, advisory.
    bare = tmp_path / "elsewhere" / "job"
    bare.mkdir(parents=True)
    doc = load_constitution(bare)
    assert doc["source"] == "defaults"
    assert doc["enforcement"] == DEFAULT_CONSTITUTION["enforcement"]


def test_governance_composite_revision_changes_with_any_file(tmp_path) -> None:
    job_root = _legacy_job(tmp_path, None)
    migrate_from_constitution(tmp_path, "gov-demo", job_root)
    first = load_governance(tmp_path, "gov-demo")["revision"]
    gov = governance_dir(tmp_path, "gov-demo")
    (gov / AUDIT_POLICY_FILE).write_text("enforcement: blocking\n")
    commit_epoch(gov)
    second = load_governance(tmp_path, "gov-demo")["revision"]
    assert first != second


def test_stress_trap_governance_tamper(tmp_path) -> None:
    from wayfinder_paths.jobs.benchmarks.stress import TRAPS

    outcome = TRAPS["governance_tamper"](tmp_path)
    assert outcome["held"] is True, outcome
