"""Installed Paths as jobs: the pin, the validation ladder, the tick and
the upgrade notice."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from wayfinder_paths.jobs.contracts import validate_job_for_kind
from wayfinder_paths.jobs.launch import evaluate_launch_checklist
from wayfinder_paths.jobs.paths_runtime import (
    PIN_PATH,
    UPGRADE_STATE_PATH,
    create_from_path,
    run_path_tick,
    tree_sha256,
    validate_path_job,
)
from wayfinder_paths.jobs.risk_flags import risk_flags
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.paths.builder import PathBuilder
from wayfinder_paths.paths.scaffold import init_path

SLUG = "demo-rotator"
VERSION = "0.1.0"


def _install(
    tmp_path: Path, *, dry_run: str | None = None, freestyle: bool = False
) -> tuple[JobStore, Path]:
    """A fake `wayfinder path install`: scaffolded tree + bundle.zip + lock entry."""
    store = JobStore(repo_root=tmp_path)
    path_dir = tmp_path / ".wayfinder" / "paths" / SLUG / VERSION
    init_path(
        path_dir=path_dir,
        slug=SLUG,
        version=VERSION,
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    if freestyle:
        (path_dir / "scripts" / "tick.py").write_text(
            "from wayfinder_paths.jobs.freestyle import FreestyleSpec\n"
            "SPEC = FreestyleSpec(max_notional_per_tick=100, max_loss_usd=5)\n"
            "def tick(ctx):\n"
            "    if 'ETH' not in ctx.positions:\n"
            "        ctx.act({'venue': 'hyperliquid', 'kind': 'market', 'symbol': 'ETH', 'side': 'long',\n"
            "                 'notional': 50, 'max_loss': 5})\n",
            encoding="utf-8",
        )
    manifest = yaml.safe_load((path_dir / "wfpath.yaml").read_text(encoding="utf-8"))
    if freestyle:
        manifest["components"].append(
            {"id": "tick", "kind": "freestyle", "path": "scripts/tick.py"}
        )
    job_block: dict = {"schedule": {"interval_seconds": 600, "timeout_seconds": 60}}
    if dry_run:
        job_block["dry_run"] = dry_run
    if freestyle:
        job_block["component"] = "tick"
    manifest["job"] = job_block
    (path_dir / "wfpath.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    built = PathBuilder.build(path_dir=path_dir, out_path=path_dir / "bundle.zip")
    lock_dir = tmp_path / ".wayfinder"
    (lock_dir / "paths.lock.json").write_text(
        json.dumps(
            {
                "schemaVersion": "0.1",
                "paths": {
                    SLUG: {
                        "version": VERSION,
                        "bundle_sha256": built.bundle_sha256,
                        "path": str(path_dir),
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return store, path_dir


def test_create_from_path_pins_the_installed_version(tmp_path: Path) -> None:
    store, path_dir = _install(tmp_path)
    result = create_from_path(SLUG, store=store, compile_job=False)
    job = store.load(SLUG)
    assert job.execution_contract == "path_v1"
    assert job.source["slug"] == SLUG and job.source["version"] == VERSION
    assert job.source["tree_sha256"] == tree_sha256(path_dir)
    assert job.source["component_path"] == "scripts/main.py"
    assert (
        job.script_loop.interval_seconds == 600
        and job.script_loop.timeout_seconds == 60
    )
    assert (store.job_dir(SLUG) / PIN_PATH).exists()
    assert result["paper_capable"] is False  # no dry_run declared


def test_create_from_path_refuses_an_uninstalled_slug(tmp_path: Path) -> None:
    store = JobStore(repo_root=tmp_path)
    with pytest.raises(FileNotFoundError):
        create_from_path(
            "never-installed", store=store, compile_job=False, interval_seconds=60
        )


def test_validate_exec_component_without_dry_run_is_not_paper_capable(
    tmp_path: Path,
) -> None:
    store, _ = _install(tmp_path)
    create_from_path(SLUG, store=store, compile_job=False)
    report = validate_job_for_kind(SLUG, store=store)
    by_name = {c["name"]: c for c in report["checks"]}
    assert report["kind"] == "path_v1" and report["status"] == "passed"
    assert (
        by_name["bundle_sha256_verified"]["passed"]
        and by_name["installed_tree_matches_pin"]["passed"]
    )
    assert by_name["dry_run_exec"]["skipped"] is True
    assert report["paper_capable"] is False
    flags = {f["code"] for f in risk_flags(store.load(SLUG), store.job_dir(SLUG))}
    assert "no_dry_run" in flags
    checklist = evaluate_launch_checklist(SLUG, store=store)
    assert checklist["ok"] is True  # paper launch allowed; the tick will skip
    assert any(
        i["id"] == "path_pin" and i["status"] == "pass" for i in checklist["items"]
    )


def test_validate_exec_component_with_dry_run_runs_it(tmp_path: Path) -> None:
    store, _ = _install(tmp_path, dry_run="supported")
    create_from_path(SLUG, store=store, compile_job=False)
    report = validate_path_job(SLUG, store=store)
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["dry_run_exec"]["passed"] is True, by_name["dry_run_exec"]
    assert by_name["dry_run_exec"]["exit_code"] == 0
    assert report["paper_capable"] is True


def test_tick_refuses_on_pin_mismatch_and_skips_without_paper_mode(
    tmp_path: Path, monkeypatch
) -> None:
    store, path_dir = _install(tmp_path)
    create_from_path(SLUG, store=store, compile_job=False)
    root = store.job_dir(SLUG)
    from wayfinder_paths.jobs import paths_runtime as pr

    monkeypatch.setattr(pr, "JobStore", lambda: store)
    monkeypatch.setattr(pr, "fire_triggers", lambda *a, **k: None)
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "paper")
    monkeypatch.delenv("WAYFINDER_JOB_REVISION", raising=False)
    monkeypatch.delenv("WAYFINDER_FORWARD_DIR", raising=False)
    payload = run_path_tick(root)
    assert payload["ok"] and payload["skipped"] is True
    runs = (root / "results" / "forward" / "runs.jsonl").read_text().splitlines()
    assert json.loads(runs[-1])["status"] == "skipped"

    (path_dir / "scripts" / "main.py").write_text(
        "print('tampered')\n", encoding="utf-8"
    )
    payload = run_path_tick(root)
    assert payload["ok"] is False and payload["refused"] is True
    assert "changed" in payload["error"]
    runs = (root / "results" / "forward" / "runs.jsonl").read_text().splitlines()
    assert json.loads(runs[-1])["status"] == "failed"


def test_tick_executes_a_dry_run_capable_component_and_notes_upgrades(
    tmp_path: Path, monkeypatch
) -> None:
    store, path_dir = _install(tmp_path, dry_run="supported")
    create_from_path(SLUG, store=store, compile_job=False)
    root = store.job_dir(SLUG)
    from wayfinder_paths.jobs import paths_runtime as pr

    monkeypatch.setattr(pr, "JobStore", lambda: store)
    monkeypatch.setattr(pr, "fire_triggers", lambda *a, **k: None)
    monkeypatch.setenv("WAYFINDER_JOB_MODE", "paper")
    monkeypatch.delenv("WAYFINDER_JOB_REVISION", raising=False)
    monkeypatch.delenv("WAYFINDER_FORWARD_DIR", raising=False)
    payload = run_path_tick(root)
    assert payload["ok"], payload
    assert payload["exit_code"] == 0
    runs = (root / "results" / "forward" / "runs.jsonl").read_text().splitlines()
    assert json.loads(runs[-1])["status"] == "ok"

    lock_path = tmp_path / ".wayfinder" / "paths.lock.json"
    lock = json.loads(lock_path.read_text())
    lock["paths"][SLUG]["version"] = "0.2.0"
    lock_path.write_text(json.dumps(lock), encoding="utf-8")
    payload = run_path_tick(root)
    assert payload["ok"]
    upgrade = json.loads((root / UPGRADE_STATE_PATH).read_text())
    assert (
        upgrade["available_version"] == "0.2.0" and upgrade["pinned_version"] == VERSION
    )


def test_freestyle_component_runs_through_the_freestyle_runtime(tmp_path: Path) -> None:
    store, _ = _install(tmp_path, freestyle=True)
    result = create_from_path(SLUG, store=store, compile_job=False)
    assert (
        result["pin"]["component_kind"] == "freestyle"
        and result["paper_capable"] is True
    )
    report = validate_path_job(SLUG, store=store)
    by_name = {c["name"]: c for c in report["checks"]}
    assert by_name["dry_run_ok"]["passed"], by_name["dry_run_ok"]
    assert report["freestyle"]["dry_run"]["intents"][0]["symbol"] == "ETH"
    assert report["status"] == "passed"
