from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from wayfinder_paths.paths.formatter import format_path
from wayfinder_paths.paths.manifest import PathManifest, PathManifestError
from wayfinder_paths.paths.scaffold import init_path


def _write_manifest(tmp_path: Path, **extra: Any) -> Path:
    manifest_path = tmp_path / "wfpath.yaml"
    raw = {"slug": "basis-board", "name": "Basis Board", "version": "0.1.0", **extra}
    manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return manifest_path


def test_prompts_optional(tmp_path: Path) -> None:
    assert PathManifest.load(_write_manifest(tmp_path)).prompts == ()


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
        ([], "1-5 entries"),
        ([f"Prompt {i}" for i in range(6)], "1-5 entries"),
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


def test_scaffold_includes_commented_prompts_example(tmp_path: Path) -> None:
    result = init_path(
        path_dir=tmp_path / "basis-board",
        slug="basis-board",
        primary_kind="monitor",
        with_applet=False,
        with_skill=True,
    )
    text = result.manifest_path.read_text(encoding="utf-8")
    assert "# prompts:" in text
    assert PathManifest.load(result.manifest_path).prompts == ()


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
