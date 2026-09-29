from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from wayfinder_paths.jobs.offload_cli import offload_cli

SPRITES = {"backend": "https://backend.example", "preset": "jobs-v1"}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.chdir(tmp_path)
    for name in (
        "WAYFINDER_BACKTEST_RUNNER",
        "WAYFINDER_CONFIG_PATH",
        "WAYFINDER_CONFIG",
        "WAYFINDER_API_KEY",
        "WAYFINDER_SPRITES_BACKEND",
        "WAYFINDER_SPRITES_APP_NAME",
        "WAYFINDER_SPRITES_PRESET",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _write(repo: Path, config: dict) -> Path:
    path = repo / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    return path


def _run(*args: str) -> dict:
    result = CliRunner().invoke(offload_cli, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_on_and_off_flip_only_the_switch(repo: Path) -> None:
    config = {
        "system": {"api_key": "key"},
        "wallets": [{"label": "main"}],
        "backtest_runner": {"fallback": "none", "sprites": SPRITES},
    }
    path = _write(repo, config)

    on = _run("on")
    assert (
        on["offloading"],
        on["evolution_campaigns"],
        on["standalone_operations"],
    ) == (
        True,
        "remote",
        "remote",
    )
    assert on["sprites"]["backend"] == "https://backend.example"
    saved = json.loads(path.read_text())
    assert saved["backtest_runner"] == {
        "fallback": "none",
        "sprites": SPRITES,
        "provider": "sprites",
        "offload_operations": True,
    }
    assert {key: saved[key] for key in ("system", "wallets")} == {
        key: config[key] for key in ("system", "wallets")
    }

    campaigns = _run("on", "--campaigns-only")
    assert campaigns["standalone_operations"] == "local"
    assert campaigns["evolution_campaigns"] == "remote"

    off = _run("off")
    assert (off["offloading"], off["evolution_campaigns"]) == (False, "local")
    # Everything else stays, so `on` restores it.
    assert json.loads(path.read_text())["backtest_runner"] == {
        "fallback": "none",
        "sprites": SPRITES,
        "provider": "local",
        "offload_operations": False,
    }
    # The config holds keys: its permissions never loosen.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _run("status")["offloading"] is False


def test_a_configuration_that_cannot_run_remotely_is_not_saved(repo: Path) -> None:
    path = _write(repo, {"backtest_runner": {"sprites": SPRITES}})  # no API key
    before = path.read_text()
    result = CliRunner().invoke(offload_cli, ["on"])
    assert result.exit_code != 0 and "Not saved" in result.output
    assert path.read_text() == before


def test_off_with_nothing_configured_writes_nothing(repo: Path) -> None:
    assert _run("off")["offloading"] is False
    assert not (repo / "config.json").exists()
    path = _write(repo, {"system": {"api_key": "key"}})
    before = path.read_text()
    _run("off")
    assert path.read_text() == before


def test_an_unreadable_config_is_never_rewritten(repo: Path) -> None:
    path = repo / "config.json"
    path.write_text("{not json")
    result = CliRunner().invoke(offload_cli, ["on"])
    assert result.exit_code != 0
    assert path.read_text() == "{not json"


def test_status_warns_when_the_environment_overrides_the_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(
        repo,
        {"system": {"api_key": "key"}, "backtest_runner": {"sprites": SPRITES}},
    )
    _run("off")
    monkeypatch.setenv("WAYFINDER_BACKTEST_RUNNER", "sprites")
    result = CliRunner().invoke(offload_cli, ["status"])
    assert result.exit_code == 0
    status = json.loads(result.stdout)
    assert status["offloading"] is True
    assert "WAYFINDER_BACKTEST_RUNNER=sprites" in status["overridden_by"]
    assert "Warning" in result.stderr
