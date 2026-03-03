from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from wayfinder_paths.packs.manifest import PackManifest, PackManifestError
from wayfinder_paths.packs.scaffold import slugify

_ROOT_ASSET_RE = re.compile(r"""(?:src|href)=["']/(assets|_next)/""")
_SERVICE_WORKER_RE = re.compile(r"serviceWorker", re.IGNORECASE)


class PackDoctorError(Exception):
    pass


@dataclass(frozen=True)
class DoctorIssue:
    level: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class PackDoctorReport:
    ok: bool
    slug: str | None
    version: str | None
    primary_kind: str | None
    errors: list[DoctorIssue]
    warnings: list[DoctorIssue]
    created_files: list[str]


def _read_template(relative_path: str) -> str:
    root = resources.files("wayfinder_paths.packs")
    template_path = root.joinpath("templates").joinpath(relative_path)
    return template_path.read_text(encoding="utf-8")


def _render_template(text: str, context: dict[str, Any]) -> str:
    rendered = text
    for key, value in context.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", str(value))
    return rendered


def _write_if_missing(path: Path, content: str, *, overwrite: bool) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        return False
    path.write_text(content.rstrip() + "\n", encoding="utf-8")
    return True


def run_doctor(
    *,
    pack_dir: Path,
    fix: bool = False,
    overwrite: bool = False,
) -> PackDoctorReport:
    pack_dir = pack_dir.resolve()
    if not pack_dir.exists():
        raise PackDoctorError(f"Pack directory not found: {pack_dir}")
    if not pack_dir.is_dir():
        raise PackDoctorError(f"Pack path must be a directory: {pack_dir}")

    manifest_path = pack_dir / "wfpack.yaml"
    if not manifest_path.exists():
        raise PackDoctorError(
            "Missing wfpack.yaml (run `wayfinder pack init <slug>` to scaffold one)"
        )

    errors: list[DoctorIssue] = []
    warnings: list[DoctorIssue] = []
    created_files: list[str] = []

    manifest: PackManifest | None = None
    try:
        manifest = PackManifest.load(manifest_path)
    except PackManifestError as exc:
        errors.append(
            DoctorIssue(level="error", message=str(exc), path=str(manifest_path))
        )

    ctx: dict[str, Any] = {}
    if manifest:
        expected_slug = slugify(manifest.slug)
        if expected_slug != manifest.slug:
            errors.append(
                DoctorIssue(
                    level="error",
                    message=f"wfpack.yaml slug must be URL-safe (suggested: {expected_slug})",
                    path=str(manifest_path),
                )
            )

        component_path = ""
        components = manifest.raw.get("components")
        if isinstance(components, list) and components:
            first = components[0] if components else None
            if isinstance(first, dict):
                component_path = str(first.get("path") or "").strip()
        if not component_path:
            component_path = (
                "strategy.py"
                if manifest.primary_kind == "strategy"
                else "scripts/main.py"
            )

        ctx = {
            "slug": manifest.slug,
            "name": manifest.name,
            "version": manifest.version,
            "summary": manifest.summary.strip()
            or "TODO: describe what this pack does.",
            "primary_kind": manifest.primary_kind,
            "component_path": component_path,
        }

        if isinstance(components, list):
            for item in components:
                if not isinstance(item, dict):
                    warnings.append(
                        DoctorIssue(
                            level="warning",
                            message="components entry must be an object",
                        )
                    )
                    continue
                path_raw = str(item.get("path") or "").strip()
                if not path_raw:
                    continue
                target = pack_dir / path_raw
                if not target.exists():
                    warnings.append(
                        DoctorIssue(
                            level="warning",
                            message=f"Component file not found: {path_raw}",
                            path=str(target),
                        )
                    )
        elif components is not None:
            warnings.append(
                DoctorIssue(
                    level="warning", message="wfpack.yaml components must be a list"
                )
            )

        readme_path = pack_dir / "README.md"
        if not readme_path.exists():
            warnings.append(
                DoctorIssue(
                    level="warning", message="Missing README.md", path=str(readme_path)
                )
            )
            if fix:
                rendered = _render_template(_read_template("README.md.tmpl"), ctx)
                if _write_if_missing(readme_path, rendered, overwrite=overwrite):
                    created_files.append("README.md")

        skill_path = pack_dir / "skill" / "SKILL.md"
        if not skill_path.exists():
            warnings.append(
                DoctorIssue(
                    level="warning",
                    message="Missing skill/SKILL.md",
                    path=str(skill_path),
                )
            )
            if fix:
                rendered = _render_template(_read_template("skill/SKILL.md.tmpl"), ctx)
                if _write_if_missing(skill_path, rendered, overwrite=overwrite):
                    created_files.append("skill/SKILL.md")

        if manifest.applet:
            applet_manifest_path = pack_dir / manifest.applet.manifest_path
            if not applet_manifest_path.exists():
                errors.append(
                    DoctorIssue(
                        level="error",
                        message="Missing applet.manifest.json (declared in wfpack.yaml)",
                        path=str(applet_manifest_path),
                    )
                )
                if fix:
                    rendered = _render_template(
                        _read_template("applet/applet.manifest.json.tmpl"),
                        ctx,
                    )
                    if _write_if_missing(
                        applet_manifest_path, rendered, overwrite=overwrite
                    ):
                        created_files.append(manifest.applet.manifest_path)

            build_dir = pack_dir / manifest.applet.build_dir
            if not build_dir.exists():
                errors.append(
                    DoctorIssue(
                        level="error",
                        message="Applet build_dir does not exist (run your applet build step or scaffold a static UI)",
                        path=str(build_dir),
                    )
                )
                if fix:
                    build_dir.mkdir(parents=True, exist_ok=True)

            entry = "index.html"
            if applet_manifest_path.exists():
                try:
                    parsed = json.loads(
                        applet_manifest_path.read_text(encoding="utf-8")
                    )
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    entry_val = str(parsed.get("entry") or "").strip()
                    if entry_val:
                        entry = entry_val
                    if not str(parsed.get("readySelector") or "").strip():
                        warnings.append(
                            DoctorIssue(
                                level="warning",
                                message="applet.manifest.json missing readySelector",
                                path=str(applet_manifest_path),
                            )
                        )
                else:
                    warnings.append(
                        DoctorIssue(
                            level="warning",
                            message="applet.manifest.json must be a JSON object",
                            path=str(applet_manifest_path),
                        )
                    )

            entry_path = build_dir / entry
            if not entry_path.exists():
                errors.append(
                    DoctorIssue(
                        level="error",
                        message=f"Applet entry not found: {manifest.applet.build_dir}/{entry}",
                        path=str(entry_path),
                    )
                )
                if fix and entry == "index.html":
                    rendered = _render_template(
                        _read_template("applet/dist/index.html.tmpl"),
                        ctx,
                    )
                    if _write_if_missing(entry_path, rendered, overwrite=overwrite):
                        created_files.append(f"{manifest.applet.build_dir}/{entry}")

            if entry_path.exists():
                entry_text = entry_path.read_text(encoding="utf-8", errors="ignore")
                if _ROOT_ASSET_RE.search(entry_text):
                    errors.append(
                        DoctorIssue(
                            level="error",
                            message="Applet entry uses root-absolute asset URLs (/assets or /_next). Use relative asset URLs.",
                            path=str(entry_path),
                        )
                    )
                if _SERVICE_WORKER_RE.search(entry_text):
                    warnings.append(
                        DoctorIssue(
                            level="warning",
                            message="Applet entry references service workers; avoid service workers for MVP pack applets.",
                            path=str(entry_path),
                        )
                    )

            js_path = build_dir / "assets" / "app.js"
            if fix and entry_path.exists() and not js_path.exists():
                rendered = _render_template(
                    _read_template("applet/dist/assets/app.js.tmpl"),
                    ctx,
                )
                if _write_if_missing(js_path, rendered, overwrite=overwrite):
                    created_files.append(f"{manifest.applet.build_dir}/assets/app.js")

    ok = len(errors) == 0
    return PackDoctorReport(
        ok=ok,
        slug=getattr(manifest, "slug", None),
        version=getattr(manifest, "version", None),
        primary_kind=getattr(manifest, "primary_kind", None),
        errors=errors,
        warnings=warnings,
        created_files=created_files,
    )
