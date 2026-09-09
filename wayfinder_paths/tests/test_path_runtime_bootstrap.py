from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import Version

from wayfinder_paths.paths.renderer import _render_bootstrap_script


@pytest.fixture
def bootstrap(tmp_path: Path) -> dict[str, Any]:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    script = scripts / "wf_bootstrap.py"
    script.write_text(_render_bootstrap_script({}), encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "manifest.json").write_text(
        json.dumps(
            {
                "package": "wayfinder-paths",
                "version": "0.11.0",
                "python": ">=3.12,<3.13",
                "component": "main",
                "require_api_key": False,
            }
        ),
        encoding="utf-8",
    )
    return runpy.run_path(str(script))


@pytest.mark.parametrize("current", [(3, 11, 9), (3, 12, 0), (3, 12, 4), (3, 13, 1)])
@pytest.mark.parametrize("without_packaging", [False, True])
@pytest.mark.parametrize(
    "spec",
    [
        "",
        " ",
        ">=3.12,<3.13",
        ">=3.12, ,<3.13,",
        "==3",
        "==3.12",
        "==3.12.0",
        "==3.12.4",
        "!=3.12",
        "<=3.12",
        ">3.12",
        "<3.12",
        ">=3.12.4",
        "<=3.12.4",
        ">3.12.4",
        "<3.12.4",
        "==3.*",
        "!=3.*",
        "==3.12.*",
        "!=3.12.*",
        "==3.12.4.*",
        "~=3.12",
        "~=3.12.0",
        "~=3.12.4",
        " >= 3.12 , < 3.13 ",
        "==03.012.004",
        "garbage",
        "~=3",
        "==3.12*",
        ">=3.12.*",
        "~=3.12.*",
        "=>3.12",
        "==3..12",
    ],
)
def test_python_check_matches_packaging(
    bootstrap: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    current: tuple[int, int, int],
    without_packaging: bool,
    spec: str,
) -> None:
    try:
        expected = Version(".".join(map(str, current))) in SpecifierSet(spec)
    except InvalidSpecifier:
        expected = False
    monkeypatch.setattr(sys, "version_info", current)
    if without_packaging:
        for module in ("packaging", "packaging.specifiers", "packaging.version"):
            monkeypatch.setitem(sys.modules, module, None)
    assert bootstrap["_python_version_is_compatible"](spec) is expected


@pytest.mark.parametrize("spec", ["==3.12.0rc1", ">=1!3.12", "===3.12", ">=3.12.0.1"])
def test_fallback_rejects_unsupported_syntax(
    bootstrap: dict[str, Any], monkeypatch: pytest.MonkeyPatch, spec: str
) -> None:
    monkeypatch.setitem(sys.modules, "packaging.specifiers", None)
    assert bootstrap["_python_version_is_compatible"](spec) is False


def test_python_check_runs_without_site_packages(bootstrap: dict[str, Any]) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            "import runpy, sys; "
            "check = runpy.run_path(sys.argv[1])['_python_version_is_compatible']; "
            "assert check('>=3.12,<4'); assert not check('>=4')",
            bootstrap["__file__"],
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_pipx_uses_validated_interpreter(
    bootstrap: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_globals = bootstrap["run"].__globals__
    monkeypatch.setattr(sys, "version_info", (3, 12, 4))
    monkeypatch.setattr(runtime_globals["shutil"], "which", lambda _: "/bin/pipx")
    commands: list[list[str]] = []

    def capture(command: list[str], env: dict[str, str]) -> int:
        commands.append(command)
        return 7

    monkeypatch.setitem(runtime_globals, "_call_cli", capture)
    manifest = bootstrap["_load_manifest"]()
    assert bootstrap["_bootstrap_with_pipx"](manifest, {}, ["--", "--help"]) == 7
    assert len(commands) == 1
    assert commands[0][:7] == [
        "/bin/pipx",
        "run",
        "--python",
        sys.executable,
        "--spec",
        "wayfinder-paths~=0.11.1",
        "wayfinder",
    ]
    assert commands[0][-2:] == ["--args-json", '["--help"]']

    monkeypatch.setattr(sys, "version_info", (3, 11, 9))
    with pytest.raises(RuntimeError, match="Current Python does not satisfy"):
        bootstrap["_bootstrap_with_pipx"](manifest, {}, [])
    assert len(commands) == 1


@pytest.mark.parametrize("method", ["uv", "pipx", "local_venv"])
def test_component_failure_is_not_retried(
    bootstrap: dict[str, Any], monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    runtime_globals = bootstrap["run"].__globals__
    monkeypatch.setitem(
        runtime_globals, "_current_runtime_is_compatible", lambda _: False
    )
    monkeypatch.setitem(runtime_globals, "_compatible_wayfinder_binary", lambda _: None)
    calls: list[str] = []

    def missing(*args: object) -> int:
        raise FileNotFoundError("bootstrap unavailable")

    def failed_component(*args: object) -> int:
        calls.append(method)
        return 7

    for candidate in ("uv", "pipx", "local_venv"):
        monkeypatch.setitem(runtime_globals, f"_bootstrap_with_{candidate}", missing)
    monkeypatch.setitem(runtime_globals, f"_bootstrap_with_{method}", failed_component)

    assert bootstrap["run"](None, []) == 7
    assert calls == [method]
