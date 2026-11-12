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
        return await self.place_grid(kwargs["amount"])

    async def update(self):
        return await self.rebalance()

    async def exit(self, **kwargs):
        return await self.close_all()

    async def _status(self):
        return {"portfolio_value": self.value}
"""

# The example paths committed to the repo. examples/paths/ is gitignored, so a
# glob would also pick up whatever a developer keeps there locally.
EXAMPLE_PATHS = Path(__file__).resolve().parents[2] / "examples" / "paths"
COMMITTED_EXAMPLES = (
    "boros-carry-demo",
    "conditional-router-reference",
    "pt-carry-roller-demo",
    "virtual-delta-neutral",
)


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
    code = "class Half(Strategy):\n    async def deposit(self):\n        return await self.go()\n"
    [(file, message)] = problems({"strategy.py": code})
    assert file == "strategy.py"
    assert "Half leaves _status, exit, update unimplemented" in message


def test_scaffold_stubs_do_not_count_as_implemented():
    code = (
        "class Grid(Strategy):\n"
        "    async def deposit(self, **kwargs):\n"
        "        return False, 'TODO: move funds'\n"
        "    async def update(self):\n"
        '        """Rebalance."""\n'
        "        pass\n"
        "    async def exit(self, **kwargs):\n"
        "        raise NotImplementedError\n"
        "    async def _status(self):\n"
        "        return {'portfolio_value': 0.0}\n"
    )
    [(_, message)] = problems({"strategy.py": code})
    assert "Grid leaves _status, deposit, exit, update unimplemented" in message


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


def test_scaffolded_strategy_fails_doctor_until_implemented(tmp_path: Path):
    path_dir = tmp_path / "grid-bot"
    init_path(
        path_dir=path_dir,
        slug="grid-bot",
        primary_kind="strategy",
        with_applet=False,
        with_skill=False,
    )

    strategy = path_dir / "strategy.py"
    source = strategy.read_text(encoding="utf-8")
    assert "class GridBotStrategy(Strategy):" in source
    assert any("unimplemented" in error for error in _errors(path_dir))

    strategy.write_text(
        FULL_STRATEGY.replace("class Grid", "class GridBot"), encoding="utf-8"
    )
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


@pytest.mark.parametrize("example", COMMITTED_EXAMPLES)
def test_example_paths_ship_what_their_kind_promises(example: str):
    # The registry rejects a version that fails these rules, so the examples
    # people copy from have to pass them.
    path_dir = EXAMPLE_PATHS / example
    files = {
        path.relative_to(path_dir).as_posix(): path.read_text(encoding="utf-8")
        for path in path_dir.rglob("*")
        if path.suffix in {".py", ".sol"} and path.is_file()
    }
    problems_found = kind_problems(
        manifest=PathManifest.load(path_dir / "wfpath.yaml").raw,
        files=files,
        has_skill=(path_dir / "skill" / "SKILL.md").is_file(),
    )
    assert problems_found == []
