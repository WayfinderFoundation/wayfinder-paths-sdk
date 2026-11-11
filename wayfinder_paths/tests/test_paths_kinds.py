from __future__ import annotations

from pathlib import Path

import pytest

from wayfinder_paths.paths.doctor import run_doctor
from wayfinder_paths.paths.kinds import PATH_KINDS, kind_problems, primary_kind_error
from wayfinder_paths.paths.manifest import PathManifest
from wayfinder_paths.paths.scaffold import PathScaffoldError, init_path

STRATEGY_MANIFEST = {
    "primary_kind": "strategy",
    "components": [{"id": "main", "kind": "strategy", "path": "strategy.py"}],
}

FULL_STRATEGY = """
from wayfinder_paths.core.strategies.Strategy import Strategy


class Grid(Strategy):
    async def deposit(self, **kwargs):
        return True, "ok"

    async def update(self):
        return True, "ok"

    async def exit(self, **kwargs):
        return True, "ok"

    async def _status(self):
        return {}
"""


def problems(files, manifest=STRATEGY_MANIFEST, has_skill=False):
    return kind_problems(manifest=manifest, files=files, has_skill=has_skill)


@pytest.mark.parametrize("raw", [None, "", "bundle", "dashboard", "applet"])
def test_unsupported_kinds_are_rejected(raw):
    assert primary_kind_error(raw)


@pytest.mark.parametrize("kind", PATH_KINDS)
def test_supported_kinds_are_accepted(kind):
    assert primary_kind_error(kind) is None


def test_strategy_subclass_with_every_abstract_method_passes():
    assert problems({"strategy.py": FULL_STRATEGY}) == []


def test_active_perps_subclass_inherits_the_methods():
    code = "class Perps(ActivePerpsStrategy):\n    pass\n"
    assert problems({"strategy.py": code}) == []


def test_missing_abstract_methods_are_named():
    code = "class Half(Strategy):\n    async def deposit(self):\n        pass\n"
    [(file, message)] = problems({"strategy.py": code})
    assert file == "strategy.py"
    assert "Half does not implement _status, exit, update" in message


def test_a_locally_defined_strategy_base_does_not_count():
    code = "class Strategy:\n    pass\n\n" + FULL_STRATEGY.split("\n\n\n", 1)[1]
    [(_, message)] = problems({"strategy.py": code})
    assert "defines no Strategy subclass" in message


def test_a_generated_skill_counts_as_a_skill():
    manifest = {"primary_kind": "skill", "skill": {"enabled": True}}
    assert problems({}, manifest=manifest) == []
    assert problems({}, manifest={"primary_kind": "skill"})


def test_contract_needs_solidity_source():
    manifest = {"primary_kind": "contract"}
    assert problems({"main.py": ""}, manifest=manifest)
    assert problems({"contracts/Arb.sol": ""}, manifest=manifest) == []


def _errors(path_dir: Path) -> list[str]:
    return [issue.message for issue in run_doctor(path_dir=path_dir).errors]


def test_scaffolded_strategy_passes_doctor(tmp_path: Path):
    path_dir = tmp_path / "grid-bot"
    init_path(
        path_dir=path_dir,
        slug="grid-bot",
        primary_kind="strategy",
        with_applet=False,
        with_skill=False,
    )

    source = (path_dir / "strategy.py").read_text(encoding="utf-8")
    assert "class GridBotStrategy(Strategy):" in source
    assert _errors(path_dir) == []


def test_doctor_rejects_a_strategy_without_a_strategy_class(tmp_path: Path):
    path_dir = tmp_path / "grid-bot"
    init_path(
        path_dir=path_dir,
        slug="grid-bot",
        primary_kind="strategy",
        with_applet=False,
        with_skill=False,
    )
    (path_dir / "strategy.py").write_text(
        "def wfpath_meta():\n    return {}\n", encoding="utf-8"
    )

    assert any("defines no Strategy subclass" in error for error in _errors(path_dir))


def test_doctor_rejects_a_retired_kind_but_load_still_accepts_it(tmp_path: Path):
    path_dir = tmp_path / "old-path"
    init_path(path_dir=path_dir, slug="old-path", with_applet=False, with_skill=False)
    manifest_path = path_dir / "wfpath.yaml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "primary_kind: script", "primary_kind: bundle"
        ),
        encoding="utf-8",
    )

    # Installed copies of paths published as "bundle" must keep loading.
    assert PathManifest.load(manifest_path).primary_kind == "bundle"
    assert any("'bundle' is not supported" in error for error in _errors(path_dir))


def test_init_defaults_and_rejects_retired_kinds(tmp_path: Path):
    init_path(path_dir=tmp_path / "a", slug="a", with_applet=False, with_skill=False)
    assert PathManifest.load(tmp_path / "a" / "wfpath.yaml").primary_kind == "script"

    with pytest.raises(PathScaffoldError, match="not supported"):
        init_path(path_dir=tmp_path / "b", slug="b", primary_kind="bundle")
