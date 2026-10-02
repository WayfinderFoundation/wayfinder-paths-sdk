from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from wayfinder_paths.jobs.activities import ActivityBinding, run_activity
from wayfinder_paths.jobs.activity_extensions import (
    load_activity_extension,
    pin_activity_extension,
)
from wayfinder_paths.jobs.paths_runtime import tree_sha256
from wayfinder_paths.jobs.store import JobStore


def installed(tmp_path: Path) -> tuple[JobStore, Path, Path]:
    store = JobStore(repo_root=tmp_path)
    source = tmp_path / ".wayfinder/paths/demo/0.1.0"
    source.mkdir(parents=True)
    (source / "wfpath.yaml").write_text(
        yaml.safe_dump(
            {
                "slug": "demo",
                "name": "Fixture",
                "version": "0.1.0",
                "components": [
                    {
                        "id": "main",
                        "kind": "activity",
                        "path": "adapter.py",
                        "capabilities": ["demo.observe"],
                    }
                ],
            }
        )
    )
    (source / "adapter.py").write_text("""
class Adapter:
    supports_submit = True
    async def close(self): pass
    async def observe(self): raise AssertionError("validation must not call the network")
    async def protect(self, **kw): raise AssertionError("validation must not execute protection")
    async def submit(self, *a, **kw): raise AssertionError("unverified extension must never submit")
    async def reconcile(self, **kw): raise AssertionError("validation must not reconcile")
def build_activity_adapter(config, options): return Adapter()
""")
    (tmp_path / ".wayfinder/paths.lock.json").write_text(
        json.dumps(
            {
                "schemaVersion": "0.1",
                "paths": {"demo": {"version": "0.1.0", "path": str(source)}},
            }
        )
    )
    return store, source, store.job_dir("objective")


def load(root: Path, pin: dict[str, Any], capability: str = "demo.observe") -> Any:
    return load_activity_extension(
        root, pin, capability=capability, config={}, options={}
    )


def test_pin_is_local_and_installed_updates_do_not_change_it(tmp_path: Path) -> None:
    store, source, root = installed(tmp_path)
    alias, pin = pin_activity_extension(
        root, {"alias": "demo", "slug": "demo"}, store=store
    )
    assert alias == "demo" and pin["tree_sha256"] == tree_sha256(source)
    (source / "adapter.py").write_text("raise RuntimeError('installed update')\n")
    assert load(root, pin).supports_submit
    (root / pin["directory"] / "adapter.py").write_text(
        "raise RuntimeError('tampered')\n"
    )
    with pytest.raises(ValueError, match="revision changed"):
        load(root, pin)


def test_pin_refuses_symlinks_and_capability_or_path_escape(tmp_path: Path) -> None:
    store, source, root = installed(tmp_path)
    _, pin = pin_activity_extension(
        root, {"alias": "demo", "slug": "demo"}, store=store
    )
    with pytest.raises(ValueError, match="not declared"):
        load(root, pin, "demo.spend")
    with pytest.raises(ValueError, match="escapes"):
        load(root, {**pin, "directory": str(source)})
    with pytest.raises(ValueError, match="escapes"):
        load(root, {**pin, "component_path": "../../../../outside.py"})
    (source / "link.py").symlink_to(source / "adapter.py")
    with pytest.raises(ValueError, match="symlinks"):
        pin_activity_extension(root, {"alias": "second", "slug": "demo"}, store=store)


@pytest.mark.asyncio
async def test_pin_does_not_grant_live_authority(tmp_path: Path) -> None:
    store, _, root = installed(tmp_path)
    _, pin = pin_activity_extension(
        root, {"alias": "demo", "slug": "demo"}, store=store
    )
    binding = ActivityBinding.model_validate(
        {
            "extension": "demo",
            "capability": "demo.observe",
            "limits": {
                "protocol": "demo",
                "program": "test",
                "rule_revision": "v1",
                "enabled": True,
                "cost_unit": "USD",
                "max_total_cost": 1,
                "max_daily_cost": 1,
                "max_operation_cost": 1,
            },
        }
    )
    kwargs = {
        "state_dir": root / "state",
        "now": 1000,
        "mode": "live",
        "halted": False,
        "extension_pin": pin,
        "job_root": root,
    }
    assert (await run_activity(binding, dry_run=True, **kwargs))[
        "external_verified"
    ] is False
    with pytest.raises(ValueError, match="not certified"):
        await run_activity(binding, dry_run=False, **kwargs)
    assert not (root / "state/participation.json").exists()
