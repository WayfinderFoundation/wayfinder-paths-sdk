from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any


class PackScaffoldError(Exception):
    pass


_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    value = re.sub(r"-{2,}", "-", value)
    return value


def humanize_slug(slug: str) -> str:
    parts = [p for p in re.split(r"[-_]+", slug.strip()) if p]
    return " ".join([p[:1].upper() + p[1:] for p in parts]) if parts else slug


def _yaml_quote(value: str) -> str:
    escaped = (value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_template(text: str, context: dict[str, Any]) -> str:
    rendered = text
    for key, value in context.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered


def _read_template(relative_path: str) -> str:
    root = resources.files("wayfinder_paths.packs")
    template_path = root.joinpath("templates").joinpath(relative_path)
    return template_path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class PackInitResult:
    pack_dir: Path
    manifest_path: Path
    created_files: list[Path]
    overwritten_files: list[Path]
    skipped_files: list[Path]


def _build_wfpack_yaml(
    *,
    slug: str,
    name: str,
    version: str,
    summary: str,
    primary_kind: str,
    tags: list[str],
    component_kind: str,
    component_path: str,
    with_applet: bool,
    with_skill: bool,
) -> str:
    tags_unique: list[str] = []
    for tag in tags:
        t = str(tag).strip()
        if not t:
            continue
        if t not in tags_unique:
            tags_unique.append(t)

    description = summary.strip() or f"Use the {slug} pack through Wayfinder."

    lines: list[str] = []
    lines.append('schema_version: "0.1"')
    lines.append("")
    lines.append(f"slug: {slug}")
    lines.append(f"name: {_yaml_quote(name)}")
    lines.append(f"version: {_yaml_quote(version)}")
    if summary.strip():
        lines.append(f"summary: {_yaml_quote(summary)}")
    lines.append("")
    lines.append(f"primary_kind: {primary_kind}")
    lines.append("tags:")
    for tag in tags_unique:
        lines.append(f"  - {tag}")

    lines.append("")
    lines.append("components:")
    lines.append('  - id: "main"')
    lines.append(f"    kind: {component_kind}")
    lines.append(f"    path: {_yaml_quote(component_path)}")

    if with_applet:
        lines.append("")
        lines.append("applet:")
        lines.append('  build_dir: "applet/dist"')
        lines.append('  manifest: "applet/applet.manifest.json"')

    if with_skill:
        lines.append("")
        lines.append("skill:")
        lines.append("  enabled: true")
        lines.append("  source: generated")
        lines.append(f"  name: {_yaml_quote(slug)}")
        lines.append(f"  description: {_yaml_quote(description)}")
        lines.append('  instructions: "skill/instructions.md"')

    lines.append("")
    return "\n".join(lines)


def init_pack(
    *,
    pack_dir: Path,
    slug: str,
    name: str | None = None,
    version: str = "0.1.0",
    summary: str = "",
    primary_kind: str = "bundle",
    tags: list[str] | None = None,
    with_applet: bool = False,
    with_skill: bool = True,
    overwrite: bool = False,
) -> PackInitResult:
    slug = slugify(slug)
    if not slug or not _SLUG_RE.fullmatch(slug):
        raise PackScaffoldError("Invalid slug (expected lowercase url-safe slug)")

    pack_dir = pack_dir.resolve()
    pack_dir.mkdir(parents=True, exist_ok=True)

    pack_name = (name or humanize_slug(slug)).strip() or slug
    primary_kind = (primary_kind or "bundle").strip()
    tag_list = tags if tags is not None else [primary_kind]
    if primary_kind not in tag_list:
        tag_list = [primary_kind, *tag_list]

    if primary_kind == "strategy":
        component_kind = "strategy"
        component_path = "strategy.py"
        component_template = "components/strategy.py.tmpl"
    else:
        component_kind = "script"
        component_path = "scripts/main.py"
        component_template = "components/script.py.tmpl"

    manifest_text = _build_wfpack_yaml(
        slug=slug,
        name=pack_name,
        version=version,
        summary=summary,
        primary_kind=primary_kind,
        tags=tag_list,
        component_kind=component_kind,
        component_path=component_path,
        with_applet=with_applet,
        with_skill=with_skill,
    )

    ctx: dict[str, Any] = {
        "slug": slug,
        "name": pack_name,
        "version": version,
        "summary": summary.strip() or "TODO: describe what this pack does.",
        "primary_kind": primary_kind,
        "component_path": component_path,
    }

    created: list[Path] = []
    overwritten: list[Path] = []
    skipped: list[Path] = []

    def write(rel_path: str, content: str) -> None:
        path = pack_dir / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not overwrite:
            skipped.append(path)
            return
        if path.exists():
            overwritten.append(path)
        else:
            created.append(path)
        path.write_text(content.rstrip() + "\n", encoding="utf-8")

    write("wfpack.yaml", manifest_text)
    write("README.md", _render_template(_read_template("README.md.tmpl"), ctx))
    write(component_path, _render_template(_read_template(component_template), ctx))

    if with_skill:
        write(
            "skill/instructions.md",
            _render_template(_read_template("skill/instructions.md.tmpl"), ctx),
        )

    if with_applet:
        write(
            "applet/applet.manifest.json",
            _render_template(_read_template("applet/applet.manifest.json.tmpl"), ctx),
        )
        write(
            "applet/dist/index.html",
            _render_template(_read_template("applet/dist/index.html.tmpl"), ctx),
        )
        write(
            "applet/dist/assets/app.js",
            _render_template(_read_template("applet/dist/assets/app.js.tmpl"), ctx),
        )

    template_meta = {
        "template": primary_kind,
        "template_version": "0.1.0",
        "created_with": "wayfinder-paths",
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "answers": {
            "slug": slug,
            "name": pack_name,
            "version": version,
            "primary_kind": primary_kind,
            "with_applet": with_applet,
            "with_skill": with_skill,
            "component_path": component_path,
        },
    }
    meta_path = pack_dir / ".wayfinder" / "template.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    if meta_path.exists() and not overwrite:
        skipped.append(meta_path)
    else:
        if meta_path.exists():
            overwritten.append(meta_path)
        else:
            created.append(meta_path)
        meta_path.write_text(
            json.dumps(template_meta, indent=2, default=str) + "\n", encoding="utf-8"
        )

    return PackInitResult(
        pack_dir=pack_dir,
        manifest_path=pack_dir / "wfpack.yaml",
        created_files=created,
        overwritten_files=overwritten,
        skipped_files=skipped,
    )
