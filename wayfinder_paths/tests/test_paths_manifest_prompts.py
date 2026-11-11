from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from wayfinder_paths.paths.doctor import run_doctor
from wayfinder_paths.paths.formatter import format_path
from wayfinder_paths.paths.manifest import (
    MISSING_PROMPTS_WARNING,
    PROMPT_PLACEHOLDER,
    PROMPTS_COMMENT_BLOCK,
    PathManifest,
    PathManifestError,
    prompt_warnings,
)
from wayfinder_paths.paths.scaffold import init_path


def _write_manifest(tmp_path: Path, **extra: Any) -> Path:
    manifest_path = tmp_path / "wfpath.yaml"
    raw = {"slug": "basis-board", "name": "Basis Board", "version": "0.1.0", **extra}
    manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return manifest_path


def _doctor_messages(path_dir: Path) -> tuple[list[str], list[str]]:
    report = run_doctor(path_dir=path_dir)
    return (
        [issue.message for issue in report.errors],
        [issue.message for issue in report.warnings],
    )


@pytest.mark.parametrize("extra", [{}, {"prompts": []}, {"prompts": None}])
def test_missing_or_empty_prompts_warn(tmp_path: Path, extra: dict[str, Any]) -> None:
    manifest = PathManifest.load(_write_manifest(tmp_path, **extra))
    assert manifest.prompts == ()
    assert prompt_warnings(manifest.prompts) == [MISSING_PROMPTS_WARNING]


def test_valid_prompts_do_not_warn(tmp_path: Path) -> None:
    manifest = PathManifest.load(
        _write_manifest(
            tmp_path, prompts=["Which ETH basis trade pays the most right now?"]
        )
    )
    assert prompt_warnings(manifest.prompts) == []


def test_placeholder_prompt_warns(tmp_path: Path) -> None:
    manifest = PathManifest.load(
        _write_manifest(
            tmp_path,
            prompts=["Which basis spreads widened today?", f" {PROMPT_PLACEHOLDER} "],
        )
    )
    warnings = prompt_warnings(manifest.prompts)
    assert len(warnings) == 1
    assert "prompts[1] is still the scaffold placeholder" in warnings[0]


def test_valid_prompts_are_trimmed(tmp_path: Path) -> None:
    manifest = PathManifest.load(
        _write_manifest(
            tmp_path,
            prompts=[
                "  Find the best ETH basis trade right now ",
                "Which basis spreads widened in the last 24h?",
                "x" * 100,
            ],
        )
    )
    assert manifest.prompts == (
        "Find the best ETH basis trade right now",
        "Which basis spreads widened in the last 24h?",
        "x" * 100,
    )


@pytest.mark.parametrize(
    ("prompts", "message"),
    [
        ("Find the best trade", "must be a list"),
        ({"a": "b"}, "must be a list"),
        ([f"Prompt {i}" for i in range(6)], "at most 5 entries"),
        (["ok", 42], r"prompts\[1\] must be a string"),
        (["ok", "   "], r"prompts\[1\] must not be empty"),
        (["x" * 101], "at most 100 characters"),
        (["Read https://example.org/x first"], "URL"),
        (["Check www.wayfinder.ai"], "URL"),
        (["Open wayfinder.ai and trade"], "URL"),
        (["Run `wfpath fmt`"], "markdown"),
        (["Find the **best** trade"], "markdown"),
        (["See [docs](here)"], "markdown"),
        (["# Heading"], "markdown"),
        (["- list item"], "markdown"),
    ],
)
def test_invalid_prompts_fail(tmp_path: Path, prompts: Any, message: str) -> None:
    with pytest.raises(PathManifestError, match=message):
        PathManifest.load(_write_manifest(tmp_path, prompts=prompts))


def test_plain_prompts_with_punctuation_pass(tmp_path: Path) -> None:
    prompts = [
        "What's the ETH/USDC funding rate vs. last week?",
        "Size a 2x basis trade with $500 margin",
    ]
    manifest = PathManifest.load(_write_manifest(tmp_path, prompts=prompts))
    assert manifest.prompts == tuple(prompts)


def test_scaffold_prompts_stay_commented_out(tmp_path: Path) -> None:
    path_dir = tmp_path / "basis-board"
    result = init_path(
        path_dir=path_dir,
        slug="basis-board",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    text = result.manifest_path.read_text(encoding="utf-8")
    assert "# prompts:" in text
    assert text.count(f'#   - "{PROMPT_PLACEHOLDER}"') == 3
    assert "prompts" not in yaml.safe_load(text)
    assert PathManifest.load(result.manifest_path).prompts == ()

    errors, warnings = _doctor_messages(path_dir)
    assert errors == []
    assert MISSING_PROMPTS_WARNING in warnings


@pytest.mark.parametrize("empty_prompts", ["", "prompts: []\n", "prompts:\n"])
def test_format_path_keeps_commented_prompts_block(
    tmp_path: Path, empty_prompts: str
) -> None:
    path_dir = tmp_path / "basis-board"
    result = init_path(
        path_dir=path_dir,
        slug="basis-board",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    with result.manifest_path.open("a", encoding="utf-8") as fh:
        fh.write(empty_prompts)

    format_path(path_dir=path_dir)
    formatted = result.manifest_path.read_text(encoding="utf-8")
    assert PROMPTS_COMMENT_BLOCK in formatted
    assert "prompts" not in yaml.safe_load(formatted)
    assert "wfpath.yaml" not in format_path(path_dir=path_dir).changed_files


def test_uncommented_scaffold_placeholders_warn_without_error(tmp_path: Path) -> None:
    path_dir = tmp_path / "basis-board"
    result = init_path(
        path_dir=path_dir,
        slug="basis-board",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    text = result.manifest_path.read_text(encoding="utf-8")
    uncommented = text.replace("# prompts:", "prompts:").replace(
        f'#   - "{PROMPT_PLACEHOLDER}"', f'  - "{PROMPT_PLACEHOLDER}"'
    )
    result.manifest_path.write_text(uncommented, encoding="utf-8")

    errors, warnings = _doctor_messages(path_dir)
    assert errors == []
    assert MISSING_PROMPTS_WARNING not in warnings
    assert sum("scaffold placeholder" in w for w in warnings) == 3


def test_doctor_reports_malformed_prompts_as_error(tmp_path: Path) -> None:
    path_dir = tmp_path / "basis-board"
    result = init_path(
        path_dir=path_dir,
        slug="basis-board",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    raw = yaml.safe_load(result.manifest_path.read_text(encoding="utf-8"))
    raw["prompts"] = ["See https://example.org"]
    result.manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    errors, _ = _doctor_messages(path_dir)
    assert any("must not contain a URL" in e for e in errors)


def test_format_path_keeps_prompts(tmp_path: Path) -> None:
    path_dir = tmp_path / "basis-board"
    result = init_path(
        path_dir=path_dir,
        slug="basis-board",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    raw = yaml.safe_load(result.manifest_path.read_text(encoding="utf-8"))
    raw["prompts"] = ["Find the best ETH basis trade right now"]
    result.manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    format_path(path_dir=path_dir)

    formatted = yaml.safe_load(result.manifest_path.read_text(encoding="utf-8"))
    assert formatted["prompts"] == ["Find the best ETH basis trade right now"]
    assert list(formatted).index("prompts") == list(formatted).index("tags") + 1
    assert "wfpath.yaml" not in format_path(path_dir=path_dir).changed_files
