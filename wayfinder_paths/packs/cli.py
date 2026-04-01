from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import click

from wayfinder_paths.packs.builder import PackBuilder, PackBuildError, _sha256_file
from wayfinder_paths.packs.client import PacksApiClient, PacksApiError
from wayfinder_paths.packs.doctor import PackDoctorError, PackDoctorReport, run_doctor
from wayfinder_paths.packs.formatter import PackFormatError, format_pack
from wayfinder_paths.packs.hooks import PackHooksError, install_pack_hooks
from wayfinder_paths.packs.manifest import (
    PackManifest,
    PackManifestError,
)
from wayfinder_paths.packs.preview import (
    PackPreviewError,
    inspect_preview_pack,
    preview_pack,
)
from wayfinder_paths.packs.renderer import (
    PackSkillRenderError,
    PackSkillRenderReport,
    render_skill_exports,
)
from wayfinder_paths.packs.scaffold import PackScaffoldError, init_pack, slugify


def _echo_json(data: Any) -> None:
    click.echo(json.dumps(data, indent=2, default=str))


def _doctor_result_payload(report: PackDoctorReport) -> dict[str, Any]:
    return {
        "slug": report.slug,
        "version": report.version,
        "primary_kind": report.primary_kind,
        "errors": [{"message": i.message, "path": i.path} for i in report.errors],
        "warnings": [{"message": i.message, "path": i.path} for i in report.warnings],
        "created_files": report.created_files,
    }


def _raise_for_doctor_errors(report: PackDoctorReport) -> None:
    if report.ok:
        return
    details = "\n".join(
        f"- {issue.message}" + (f" ({issue.path})" if issue.path else "")
        for issue in report.errors
    )
    raise click.ClickException(f"Pack doctor found errors\n{details}")


def _skill_export_warning_strings(report: PackDoctorReport) -> list[str]:
    return [
        issue.message + (f" ({issue.path})" if issue.path else "")
        for issue in report.warnings
    ]


def _zip_skill_export_dir(export_dir: Path) -> bytes:
    buf = io.BytesIO()
    with ZipFile(buf, "w") as zf:
        for path in sorted(export_dir.rglob("*")):
            if not path.is_file():
                continue
            arcname = Path("skill") / path.relative_to(export_dir)
            zf.write(path, arcname.as_posix())
    return buf.getvalue()


def _collect_skill_export_uploads(
    render_report: PackSkillRenderReport,
    doctor_report: PackDoctorReport,
) -> tuple[dict[str, Any] | None, dict[str, bytes]]:
    if not render_report.rendered_hosts:
        return None, {}

    skill_exports: dict[str, bytes] = {}
    exports_detail: dict[str, Any] = {}
    for host in render_report.rendered_hosts:
        info = render_report.exports.get(host)
        if info is None:
            raise click.ClickException(
                f"Missing rendered export metadata for host '{host}'"
            )
        skill_exports[host] = _zip_skill_export_dir(info.export_dir)
        exports_detail[host] = {
            "filename": info.filename,
            "mode": info.mode,
            "runtime": info.runtime_manifest,
            "export": info.export_manifest,
        }

    exports_manifest = {
        "targets": render_report.rendered_hosts,
        "doctor": {
            "status": "warn" if doctor_report.warnings else "ok",
            "warnings": _skill_export_warning_strings(doctor_report),
        },
        "exports": exports_detail,
    }
    return exports_manifest, skill_exports


def _load_applet_meta(pack_dir: Path, manifest: PackManifest) -> dict[str, Any]:
    if manifest.applet is None:
        return {}
    applet_manifest_path = pack_dir / manifest.applet.manifest_path
    if not applet_manifest_path.exists():
        return {}
    try:
        parsed = json.loads(applet_manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        "build_dir": manifest.applet.build_dir,
        "applet_manifest": manifest.applet.manifest_path,
        **parsed,
    }


def _prepare_pack_for_build(
    pack_dir: Path,
) -> tuple[PackDoctorReport, PackSkillRenderReport]:
    try:
        doctor_report = run_doctor(pack_dir=pack_dir, fix=False, overwrite=False)
    except PackDoctorError as exc:
        raise click.ClickException(str(exc)) from exc
    _raise_for_doctor_errors(doctor_report)

    try:
        render_report = render_skill_exports(pack_dir=pack_dir)
    except PackSkillRenderError as exc:
        raise click.ClickException(str(exc)) from exc

    return doctor_report, render_report


def _load_pack_manifest(pack_dir: Path) -> PackManifest:
    try:
        return PackManifest.load((pack_dir / "wfpack.yaml").resolve())
    except PackManifestError as exc:
        raise click.ClickException(str(exc)) from exc


def _resolve_component_execution_target(
    manifest: PackManifest,
    *,
    component_id: str | None = None,
) -> tuple[str, str]:
    component = manifest.resolve_component(component_id)
    component_id_value = str(component.get("id") or "").strip() or "main"
    component_path = str(component.get("path") or "").strip()
    if not component_path:
        raise click.ClickException(f"Component '{component_id_value}' is missing path")
    return component_id_value, component_path


def _run_pack_component(
    *,
    pack_dir: Path,
    component_id: str | None,
    args: tuple[str, ...] | list[str],
) -> int:
    manifest = _load_pack_manifest(pack_dir)
    resolved_component_id, component_path = _resolve_component_execution_target(
        manifest,
        component_id=component_id,
    )
    target = (pack_dir / component_path).resolve()
    if not target.exists():
        raise click.ClickException(
            f"Component path not found for '{resolved_component_id}': {target}"
        )

    env = os.environ.copy()
    current_pythonpath = env.get("PYTHONPATH", "").strip()
    env["PYTHONPATH"] = (
        str(pack_dir)
        if not current_pythonpath
        else str(pack_dir) + os.pathsep + current_pythonpath
    )
    cmd = [sys.executable, str(target), *list(args)]
    return subprocess.call(cmd, cwd=str(pack_dir), env=env)


def _export_single_skill(
    *,
    pack_dir: Path,
    host: str,
) -> tuple[PackDoctorReport, PackSkillRenderReport]:
    try:
        doctor_report = run_doctor(pack_dir=pack_dir, fix=False, overwrite=False)
    except PackDoctorError as exc:
        raise click.ClickException(str(exc)) from exc
    _raise_for_doctor_errors(doctor_report)

    try:
        render_report = render_skill_exports(pack_dir=pack_dir, hosts=[host])
    except PackSkillRenderError as exc:
        raise click.ClickException(str(exc)) from exc
    return doctor_report, render_report


def _copy_export_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest)


def _activate_destination(host: str, scope: str, *, cwd: Path) -> Path:
    if host == "claude":
        if scope == "project":
            return cwd / ".claude" / "skills"
        if scope == "personal":
            return Path.home() / ".claude" / "skills"
    elif host == "codex":
        if scope == "repo":
            return cwd / ".agents" / "skills"
        if scope == "user":
            return Path.home() / ".agents" / "skills"
        if scope == "admin":
            return Path("/etc/codex/skills")
    elif host == "openclaw":
        if scope == "workspace":
            return cwd / "skills"
        if scope == "shared":
            return Path.home() / ".openclaw" / "skills"

    raise click.ClickException(f"Unsupported host/scope combination: {host}/{scope}")


@click.group(name="pack", help="Build, publish, and emit signals for Packs.")
def pack_cli() -> None:
    pass


@pack_cli.command(
    name="init", help="Scaffold a new pack folder (wfpack.yaml + optional applet)."
)
@click.argument("slug")
@click.option(
    "--dir",
    "base_dir",
    default=".",
    show_default=True,
    help="Base directory to create the pack in.",
)
@click.option("--name", default=None, help="Pack display name (defaults from slug).")
@click.option("--version", default="0.1.0", show_default=True)
@click.option("--summary", default="", show_default=True)
@click.option(
    "--kind",
    "primary_kind",
    default="bundle",
    show_default=True,
    type=click.Choice(
        ["bundle", "monitor", "strategy", "script", "contract", "dashboard", "policy"],
        case_sensitive=False,
    ),
)
@click.option("--tag", "tags", multiple=True, help="Tag (repeatable).")
@click.option("--applet/--no-applet", default=False, show_default=True)
@click.option("--skill/--no-skill", default=True, show_default=True)
@click.option(
    "--overwrite", is_flag=True, help="Overwrite scaffolded files if they exist."
)
def init_cmd(
    slug: str,
    base_dir: str,
    name: str | None,
    version: str,
    summary: str,
    primary_kind: str,
    tags: tuple[str, ...],
    applet: bool,
    skill: bool,
    overwrite: bool,
) -> None:
    safe_slug = slugify(slug)
    pack_dir = Path(base_dir).expanduser() / safe_slug
    try:
        result = init_pack(
            pack_dir=pack_dir,
            slug=safe_slug,
            name=name,
            version=version,
            summary=summary,
            primary_kind=primary_kind.lower(),
            tags=list(tags) if tags else None,
            with_applet=applet,
            with_skill=skill,
            overwrite=overwrite,
        )
    except PackScaffoldError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json(
        {
            "ok": True,
            "result": {
                "pack_dir": str(result.pack_dir),
                "manifest": str(result.manifest_path),
                "created": [
                    str(p.relative_to(result.pack_dir)) for p in result.created_files
                ],
                "overwritten": [
                    str(p.relative_to(result.pack_dir))
                    for p in result.overwritten_files
                ],
                "skipped": [
                    str(p.relative_to(result.pack_dir)) for p in result.skipped_files
                ],
            },
        }
    )


@pack_cli.command(
    name="doctor", help="Validate a pack folder and optionally fix common issues."
)
@click.option("--path", "pack_path", default=".", show_default=True)
@click.option(
    "--check",
    is_flag=True,
    help="Validation-only mode. Equivalent to the default behavior.",
)
@click.option("--fix", is_flag=True, help="Create missing recommended files.")
@click.option(
    "--overwrite", is_flag=True, help="Overwrite generated files when using --fix."
)
def doctor_cmd(pack_path: str, check: bool, fix: bool, overwrite: bool) -> None:
    if check and fix:
        raise click.ClickException("--check cannot be used together with --fix")

    try:
        report = run_doctor(pack_dir=Path(pack_path), fix=fix, overwrite=overwrite)
    except PackDoctorError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": report.ok, "result": _doctor_result_payload(report)})
    _raise_for_doctor_errors(report)


@pack_cli.command(name="fmt", help="Format pack metadata and generated skill exports.")
@click.option("--path", "pack_path", default=".", show_default=True)
def fmt_cmd(pack_path: str) -> None:
    try:
        report = format_pack(pack_dir=Path(pack_path))
    except PackFormatError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": True, "result": {"changed_files": report.changed_files}})


@pack_cli.command(
    name="render-skill", help="Generate host-specific skill exports under .build/."
)
@click.option("--path", "pack_path", default=".", show_default=True)
def render_skill_cmd(pack_path: str) -> None:
    try:
        report = render_skill_exports(pack_dir=Path(pack_path))
    except PackSkillRenderError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json(
        {
            "ok": True,
            "result": {
                "output_root": str(report.output_root),
                "rendered_hosts": report.rendered_hosts,
                "written_files": report.written_files,
            },
        }
    )


@pack_cli.command(name="version", help="Print the installed wayfinder-paths version.")
def version_cmd() -> None:
    try:
        click.echo(importlib_metadata.version("wayfinder-paths"))
    except importlib_metadata.PackageNotFoundError:
        click.echo("0.0.0")


@pack_cli.command(name="exec", help="Execute a pack component from a pack directory.")
@click.option(
    "--pack-dir",
    "pack_dir",
    required=True,
    help="Path to the exported or local pack directory.",
)
@click.option(
    "--component",
    default=None,
    help="Component id (defaults to the runtime/default component).",
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def exec_cmd(pack_dir: str, component: str | None, args: tuple[str, ...]) -> None:
    rc = _run_pack_component(
        pack_dir=Path(pack_dir).expanduser().resolve(),
        component_id=component,
        args=args,
    )
    raise SystemExit(rc)


@pack_cli.command(
    name="export-skill", help="Generate a single thin skill export for one host."
)
@click.option("--path", "pack_path", default=".", show_default=True)
@click.option(
    "--host",
    required=True,
    type=click.Choice(
        ["claude", "codex", "openclaw", "portable"], case_sensitive=False
    ),
)
def export_skill_cmd(pack_path: str, host: str) -> None:
    doctor_report, render_report = _export_single_skill(
        pack_dir=Path(pack_path),
        host=host.lower(),
    )
    info = render_report.exports[host.lower()]
    _echo_json(
        {
            "ok": True,
            "result": {
                "host": host.lower(),
                "export_dir": str(info.export_dir),
                "filename": info.filename,
                "mode": info.mode,
                "runtime": info.runtime_manifest,
                "warnings": _skill_export_warning_strings(doctor_report),
            },
        }
    )


@pack_cli.command(
    name="activate", help="Install a rendered skill export into a host skill directory."
)
@click.option(
    "--host",
    required=True,
    type=click.Choice(["claude", "codex", "openclaw"], case_sensitive=False),
)
@click.option(
    "--scope",
    required=True,
    help="Host scope (e.g. project, personal, repo, user, admin, workspace, shared).",
)
@click.option(
    "--path", "pack_path", default=None, help="Local pack directory to render from."
)
@click.option(
    "--export-path", default=None, help="Existing rendered skill export directory."
)
def activate_cmd(
    host: str,
    scope: str,
    pack_path: str | None,
    export_path: str | None,
) -> None:
    if bool(pack_path) == bool(export_path):
        raise click.ClickException("Provide exactly one of --path or --export-path")

    normalized_host = host.lower()
    normalized_scope = scope.strip().lower()
    source_dir: Path

    if pack_path:
        _, render_report = _export_single_skill(
            pack_dir=Path(pack_path),
            host=normalized_host,
        )
        source_dir = render_report.exports[normalized_host].export_dir
        skill_name = render_report.skill_name or source_dir.name
    else:
        source_dir = Path(export_path or "").expanduser().resolve()
        if not (source_dir / "SKILL.md").exists():
            raise click.ClickException(f"Rendered export not found: {source_dir}")
        skill_name = source_dir.name

    destination_root = _activate_destination(
        normalized_host, normalized_scope, cwd=Path.cwd()
    )
    dest = destination_root / skill_name
    _copy_export_tree(source_dir, dest)
    _echo_json(
        {
            "ok": True,
            "result": {
                "host": normalized_host,
                "scope": normalized_scope,
                "source": str(source_dir),
                "dest": str(dest),
                "mode": "copy",
            },
        }
    )


@pack_cli.command(
    name="preview", help="Serve a local parent-shell preview for this pack's applet."
)
@click.option("--path", "pack_path", default=".", show_default=True)
@click.option(
    "--check",
    is_flag=True,
    help="Validate preview prerequisites without starting local servers.",
)
@click.option("--parent-port", default=3333, show_default=True, type=int)
@click.option("--applet-port", default=3334, show_default=True, type=int)
def preview_cmd(
    pack_path: str,
    check: bool,
    parent_port: int,
    applet_port: int,
) -> None:
    try:
        if check:
            inspection = inspect_preview_pack(pack_dir=Path(pack_path))
            _echo_json(
                {
                    "ok": True,
                    "result": {
                        "slug": inspection.slug,
                        "name": inspection.name,
                        "applet_root": str(inspection.applet_root),
                        "entry": inspection.entry,
                        "entry_path": str(inspection.entry_path),
                    },
                }
            )
            return

        preview_pack(
            pack_dir=Path(pack_path),
            parent_port=parent_port,
            applet_port=applet_port,
        )
    except PackPreviewError as exc:
        raise click.ClickException(str(exc)) from exc


@pack_cli.group(name="hooks", help="Install local git hook automation for a pack.")
def hooks_group() -> None:
    pass


@hooks_group.command(name="install", help="Write or update .pre-commit-config.yaml.")
@click.option("--path", "pack_path", default=".", show_default=True)
def hooks_install_cmd(pack_path: str) -> None:
    try:
        report = install_pack_hooks(pack_dir=Path(pack_path))
    except PackHooksError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json(
        {
            "ok": True,
            "result": {
                "config_path": str(report.config_path),
                "changed": report.changed,
                "hooks": report.hooks,
            },
        }
    )


@pack_cli.command(name="build", help="Create a bundle.zip from a pack directory.")
@click.option("--path", "pack_path", default=".", show_default=True)
@click.option("--out", "out_path", default="dist/bundle.zip", show_default=True)
def build_cmd(pack_path: str, out_path: str) -> None:
    pack_dir = Path(pack_path)
    doctor_report, render_report = _prepare_pack_for_build(pack_dir)

    try:
        built = PackBuilder.build(pack_dir=pack_dir, out_path=Path(out_path))
    except PackBuildError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json(
        {
            "ok": True,
            "result": {
                "slug": built.manifest.slug,
                "version": built.manifest.version,
                "bundle_path": str(built.bundle_path),
                "bundle_sha256": built.bundle_sha256,
                "warnings": len(doctor_report.warnings),
                "rendered_hosts": render_report.rendered_hosts,
            },
        }
    )


@pack_cli.command(
    name="publish", help="Build and publish a pack bundle to the Packs API."
)
@click.option("--path", "pack_path", default=".", show_default=True)
@click.option("--out", "out_path", default="dist/bundle.zip", show_default=True)
@click.option("--api-url", "api_url", default=None, help="Override Packs API base URL.")
@click.option(
    "--source", "source_path", default=None, help="Optional source.zip to upload."
)
@click.option("--bonded/--unbonded", default=False, show_default=True)
@click.option(
    "--owner-wallet",
    default=None,
    help="Owner wallet for bonded publish metadata and contract args.",
)
@click.option(
    "--risk-tier",
    default=None,
    type=click.Choice(["read_only", "interactive", "execution"], case_sensitive=False),
    help="Requested risk tier for bonded publish.",
)
def publish_cmd(
    pack_path: str,
    out_path: str,
    api_url: str | None,
    source_path: str | None,
    bonded: bool,
    owner_wallet: str | None,
    risk_tier: str | None,
) -> None:
    pack_dir = Path(pack_path)
    doctor_report, render_report = _prepare_pack_for_build(pack_dir)

    if bonded and not owner_wallet:
        raise click.ClickException("--owner-wallet is required with --bonded")

    try:
        built = PackBuilder.build(pack_dir=pack_dir, out_path=Path(out_path))
    except PackBuildError as exc:
        raise click.ClickException(str(exc)) from exc

    resolved_source_path = (
        Path(source_path) if source_path else built.bundle_path.parent / "source.zip"
    )
    try:
        PackBuilder.build_source_archive(
            pack_dir=pack_dir, out_path=resolved_source_path
        )
    except PackBuildError as exc:
        raise click.ClickException(str(exc)) from exc

    exports_manifest, skill_exports = _collect_skill_export_uploads(
        render_report,
        doctor_report,
    )
    client = PacksApiClient(api_base_url=api_url)
    try:
        resp = client.publish(
            bundle_path=built.bundle_path,
            source_path=resolved_source_path,
            exports_manifest=exports_manifest,
            skill_exports=skill_exports,
            manifest=built.manifest.raw,
            applet_meta=_load_applet_meta(pack_dir, built.manifest),
            has_skill=bool(
                built.manifest.skill or (pack_dir / "skill" / "SKILL.md").exists()
            ),
            owner_wallet=owner_wallet,
            bonded=bonded,
            risk_tier=risk_tier.lower() if risk_tier else None,
        )
    except PacksApiError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": True, "result": resp})
    if resp.get("ownerLinkRequired"):
        manage_url = resp.get("manageUrl", "")
        click.echo(f"\nLink owner wallet and bond at: {manage_url}", err=True)
    elif resp.get("manageUrl"):
        click.echo(f"\nManage at: {resp['manageUrl']}", err=True)
    if resp.get("effectiveRiskTier"):
        click.echo(
            f"Effective risk tier: {resp['effectiveRiskTier']}",
            err=True,
        )
    if resp.get("requiredInitialBond"):
        click.echo(
            f"Required initial bond: {resp['requiredInitialBond']}",
            err=True,
        )
    if resp.get("requiredUpgradePendingBond"):
        click.echo(
            f"Required upgrade pending bond: {resp['requiredUpgradePendingBond']}",
            err=True,
        )
    if resp.get("reservationExpiresAt"):
        click.echo(
            f"Temporary slug reservation expires at: {resp['reservationExpiresAt']}",
            err=True,
        )
    if resp.get("slugPermanent") is True:
        click.echo("Slug reservation is permanent.", err=True)
    elif resp.get("slugPermanent") is False:
        click.echo(
            "Slug reservation is temporary until approval/publication.", err=True
        )


@pack_cli.command(name="search", help="Search packs in the registry.")
@click.argument("query", required=False, default="")
@click.option("--tag", default=None, help="Filter by tag.")
@click.option("--owner-wallet", default=None, help="Filter by owner wallet.")
@click.option("--limit", default=25, show_default=True, type=int)
@click.option("--api-url", "api_url", default=None, help="Override Packs API base URL.")
def search_cmd(
    query: str,
    tag: str | None,
    owner_wallet: str | None,
    limit: int,
    api_url: str | None,
) -> None:
    q = (query or "").strip().lower()
    limit = max(1, min(int(limit or 25), 200))

    client = PacksApiClient(api_base_url=api_url)
    try:
        packs = client.list_packs(owner_wallet=owner_wallet, tag=tag)
    except PacksApiError as exc:
        raise click.ClickException(str(exc)) from exc

    if q:

        def matches(p: dict[str, Any]) -> bool:
            blob = " ".join(
                [
                    str(p.get("slug", "")),
                    str(p.get("name", "")),
                    str(p.get("summary", "")),
                    " ".join([str(t) for t in (p.get("tags") or []) if t]),
                ]
            ).lower()
            return q in blob

        packs = [p for p in packs if matches(p)]

    _echo_json(
        {
            "ok": True,
            "result": {"count": len(packs), "packs": packs[:limit]},
        }
    )


@pack_cli.command(name="info", help="Fetch pack metadata (pack + versions).")
@click.option("--slug", required=True, help="Pack slug.")
@click.option("--api-url", "api_url", default=None, help="Override Packs API base URL.")
def info_cmd(slug: str, api_url: str | None) -> None:
    client = PacksApiClient(api_base_url=api_url)
    try:
        data = client.get_pack(slug=slug)
    except PacksApiError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo_json({"ok": True, "result": data})


@pack_cli.command(name="fork", help="Fork a pack in the registry.")
@click.option("--slug", required=True, help="Parent pack slug.")
@click.option(
    "--version", "pack_version", default=None, help="Pack version (defaults to latest)."
)
@click.option("--new-slug", default=None, help="Slug for the fork (optional).")
@click.option("--name", default=None, help="Name for the fork (optional).")
@click.option("--summary", default=None, help="Summary for the fork (optional).")
@click.option(
    "--owner-wallet", default=None, help="Owner wallet for the fork (optional)."
)
@click.option("--api-url", "api_url", default=None, help="Override Packs API base URL.")
def fork_cmd(
    slug: str,
    pack_version: str | None,
    new_slug: str | None,
    name: str | None,
    summary: str | None,
    owner_wallet: str | None,
    api_url: str | None,
) -> None:
    client = PacksApiClient(api_base_url=api_url)
    try:
        resp = client.fork_pack(
            slug=slug,
            version=pack_version,
            new_slug=new_slug,
            name=name,
            summary=summary,
            owner_wallet=owner_wallet,
        )
    except PacksApiError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": True, "result": resp})


def _safe_extract_zip(zip_path: Path, *, dest_dir: Path) -> list[str]:
    extracted: list[str] = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    with ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts:
                continue
            target = (dest_dir / rel).resolve()
            if (
                dest_dir.resolve() not in target.parents
                and target != dest_dir.resolve()
            ):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as out:
                out.write(src.read())
            extracted.append(rel.as_posix())
    extracted.sort()
    return extracted


def _install_pack(
    *,
    slug: str,
    pack_version: str | None,
    install_dir: str,
    force: bool,
    no_verify: bool,
    api_url: str | None,
) -> None:
    client = PacksApiClient(api_base_url=api_url)
    try:
        detail = client.get_pack(slug=slug)
    except PacksApiError as exc:
        raise click.ClickException(str(exc)) from exc

    pack_obj = detail.get("pack") if isinstance(detail, dict) else None
    versions = detail.get("versions") if isinstance(detail, dict) else None
    if not isinstance(pack_obj, dict) or not isinstance(versions, list) or not versions:
        raise click.ClickException("Pack not found or has no versions")

    desired_version = (
        pack_version or str(pack_obj.get("latest_version") or "")
    ).strip()
    if not desired_version:
        desired_version = str(versions[0].get("version") or "").strip()
    if not desired_version:
        raise click.ClickException("Pack has no published versions")

    version_obj = next(
        (v for v in versions if str(v.get("version") or "").strip() == desired_version),
        None,
    )
    if not isinstance(version_obj, dict):
        try:
            version_detail = client.get_pack_version(slug=slug, version=desired_version)
        except PacksApiError as exc:
            raise click.ClickException(str(exc)) from exc
        version_obj = (
            version_detail.get("version") if isinstance(version_detail, dict) else None
        )

    if not isinstance(version_obj, dict):
        raise click.ClickException(f"Version not found: {desired_version}")

    expected_sha = str(version_obj.get("bundle_sha256") or "").strip()
    if not expected_sha and not no_verify:
        raise click.ClickException("Version is missing bundle_sha256 (cannot verify)")

    base = Path(install_dir).expanduser()
    dest = base / slug / desired_version
    bundle_path = dest / "bundle.zip"

    intent_payload: dict[str, Any] | None = None
    intent_signature = ""
    warnings: list[str] = []

    if dest.exists() and any(dest.iterdir()) and not force:
        raise click.ClickException(f"Destination already exists (use --force): {dest}")

    try:
        intent_resp = client.create_install_intent(
            slug=slug,
            version=desired_version,
            runtime="sdk-cli",
            install_target=str(dest),
        )
        payload = intent_resp.get("intent")
        signature = intent_resp.get("signature")
        if isinstance(payload, dict) and isinstance(signature, str) and signature:
            intent_payload = payload
            intent_signature = signature
    except PacksApiError as exc:
        warnings.append(f"Could not create install intent: {exc}")

    dest.mkdir(parents=True, exist_ok=True)
    try:
        client.download_bundle(slug=slug, version=desired_version, out_path=bundle_path)
    except PacksApiError as exc:
        raise click.ClickException(str(exc)) from exc

    actual_sha = _sha256_file(bundle_path)
    if not no_verify and expected_sha and actual_sha.lower() != expected_sha.lower():
        raise click.ClickException(
            f"Bundle SHA-256 mismatch (expected {expected_sha}, got {actual_sha})"
        )

    extracted = _safe_extract_zip(bundle_path, dest_dir=dest)

    lock_dir = base.parent if base.name == "packs" else base
    lock_path = lock_dir / "packs.lock.json"
    lock: dict[str, Any] = {}
    if lock_path.exists():
        try:
            lock = json.loads(lock_path.read_text()) or {}
        except Exception:
            lock = {}
    if not isinstance(lock, dict):
        lock = {}

    packs_map = lock.get("packs")
    if not isinstance(packs_map, dict):
        packs_map = {}
    packs_map[slug] = {
        "version": desired_version,
        "bundle_sha256": actual_sha,
        "installed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "path": str(dest),
    }
    lock["schemaVersion"] = lock.get("schemaVersion") or "0.1"
    lock["generatedAt"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    lock["packs"] = packs_map
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(lock, indent=2, default=str) + "\n")

    receipt_status = "skipped"
    installation_id = None
    heartbeat_token = None
    if intent_payload and intent_signature:
        try:
            receipt_resp = client.submit_install_receipt(
                slug=slug,
                intent=intent_payload,
                signature=intent_signature,
                runtime="sdk-cli",
                install_path=str(dest),
                extracted_files=len(extracted),
            )
            receipt_status = str(receipt_resp.get("status", "recorded"))
            installation_id = receipt_resp.get("installation_id")
            heartbeat_token = receipt_resp.get("heartbeat_token")
        except PacksApiError as exc:
            warnings.append(f"Could not submit install receipt: {exc}")
            receipt_status = "error"

    packs_map[slug]["installation_id"] = installation_id
    packs_map[slug]["heartbeat_token"] = heartbeat_token
    lock["generatedAt"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    lock["packs"] = packs_map
    lock_path.write_text(json.dumps(lock, indent=2, default=str) + "\n")

    _echo_json(
        {
            "ok": True,
            "result": {
                "slug": slug,
                "version": desired_version,
                "bundle_path": str(bundle_path),
                "bundle_sha256": actual_sha,
                "dest": str(dest),
                "extracted_files": len(extracted),
                "lockfile": str(lock_path),
                "install_intent_id": intent_payload.get("intent_id")
                if intent_payload
                else None,
                "installation_id": installation_id,
                "heartbeat_enabled": bool(installation_id and heartbeat_token),
                "verified_install": receipt_status in {"recorded", "duplicate"},
                "install_receipt_status": receipt_status,
                "warnings": warnings,
            },
        }
    )


@pack_cli.command(name="install", help="Download and unpack a pack bundle locally.")
@click.option("--slug", required=True, help="Pack slug.")
@click.option(
    "--version", "pack_version", default=None, help="Pack version (defaults to latest)."
)
@click.option(
    "--dir",
    "install_dir",
    default=".wayfinder/packs",
    show_default=True,
    help="Base install directory.",
)
@click.option("--force", is_flag=True, help="Overwrite existing files.")
@click.option("--no-verify", is_flag=True, help="Skip bundle SHA-256 verification.")
@click.option("--api-url", "api_url", default=None, help="Override Packs API base URL.")
def install_cmd(
    slug: str,
    pack_version: str | None,
    install_dir: str,
    force: bool,
    no_verify: bool,
    api_url: str | None,
) -> None:
    _install_pack(
        slug=slug,
        pack_version=pack_version,
        install_dir=install_dir,
        force=force,
        no_verify=no_verify,
        api_url=api_url,
    )


@pack_cli.command(
    name="pull", help="Alias for install: download and unpack a pack locally."
)
@click.option("--slug", required=True, help="Pack slug.")
@click.option(
    "--version", "pack_version", default=None, help="Pack version (defaults to latest)."
)
@click.option(
    "--dir",
    "install_dir",
    default=".wayfinder/packs",
    show_default=True,
    help="Base install directory.",
)
@click.option("--force", is_flag=True, help="Overwrite existing files.")
@click.option("--no-verify", is_flag=True, help="Skip bundle SHA-256 verification.")
@click.option("--api-url", "api_url", default=None, help="Override Packs API base URL.")
def pull_cmd(
    slug: str,
    pack_version: str | None,
    install_dir: str,
    force: bool,
    no_verify: bool,
    api_url: str | None,
) -> None:
    _install_pack(
        slug=slug,
        pack_version=pack_version,
        install_dir=install_dir,
        force=force,
        no_verify=no_verify,
        api_url=api_url,
    )


@pack_cli.command(
    name="heartbeat-install",
    help="Refresh the active-install heartbeat for an installed pack.",
)
@click.option("--slug", required=True, help="Pack slug.")
@click.option(
    "--dir",
    "install_dir",
    default=".wayfinder/packs",
    show_default=True,
    help="Base install directory used during install.",
)
@click.option("--status", default="active", show_default=True)
@click.option("--api-url", "api_url", default=None, help="Override Packs API base URL.")
def heartbeat_install_cmd(
    slug: str,
    install_dir: str,
    status: str,
    api_url: str | None,
) -> None:
    base = Path(install_dir).expanduser()
    lock_dir = base.parent if base.name == "packs" else base
    lock_path = lock_dir / "packs.lock.json"
    if not lock_path.exists():
        raise click.ClickException(f"Lockfile not found: {lock_path}")

    try:
        lock = json.loads(lock_path.read_text()) or {}
    except Exception as exc:
        raise click.ClickException(f"Failed to read lockfile: {lock_path}") from exc
    packs_map = lock.get("packs") if isinstance(lock, dict) else None
    if not isinstance(packs_map, dict) or slug not in packs_map:
        raise click.ClickException(f"Pack not found in lockfile: {slug}")

    entry = packs_map.get(slug) or {}
    installation_id = str(entry.get("installation_id") or "").strip()
    heartbeat_token = str(entry.get("heartbeat_token") or "").strip()
    if not installation_id or not heartbeat_token:
        raise click.ClickException(
            "This install does not have heartbeat credentials. Reinstall the pack first."
        )

    client = PacksApiClient(api_base_url=api_url)
    try:
        resp = client.submit_install_heartbeat(
            installation_id=installation_id,
            heartbeat_token=heartbeat_token,
            status=status,
        )
    except PacksApiError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": True, "result": resp})


@pack_cli.group(name="signal", help="Emit and manage pack signals.")
def signal_group() -> None:
    pass


@signal_group.command(name="emit", help="Emit a public signal for a pack.")
@click.option("--slug", required=True, help="Pack slug.")
@click.option("--version", "pack_version", default=None, help="Optional pack version.")
@click.option("--title", required=True)
@click.option("--message", default="")
@click.option(
    "--level",
    default="info",
    show_default=True,
    type=click.Choice(["debug", "info", "warning", "error"], case_sensitive=False),
)
@click.option(
    "--metric",
    "metrics",
    multiple=True,
    help="Add a metric key=value (repeatable).",
)
@click.option("--api-url", "api_url", default=None, help="Override Packs API base URL.")
def signal_emit_cmd(
    slug: str,
    pack_version: str | None,
    title: str,
    message: str,
    level: str,
    metrics: tuple[str, ...],
    api_url: str | None,
) -> None:
    parsed_metrics: dict[str, float] = {}
    for item in metrics:
        if "=" not in item:
            raise click.ClickException(f"Invalid --metric (expected key=value): {item}")
        k, v = item.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            raise click.ClickException(f"Invalid --metric key: {item}")
        try:
            parsed_metrics[k] = float(v)
        except ValueError as exc:
            raise click.ClickException(
                f"Invalid --metric value (expected number): {item}"
            ) from exc

    client = PacksApiClient(api_base_url=api_url)
    try:
        resp = client.emit_signal(
            slug=slug,
            pack_version=pack_version,
            title=title,
            message=message,
            level=level.lower(),
            metrics=parsed_metrics,
        )
    except PacksApiError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": True, "result": resp})


@pack_cli.group(
    name="event",
    help="Emit pack runtime events (state_snapshot, decision_snapshot, receipt, heartbeat).",
)
def event_group() -> None:
    pass


@event_group.command(name="emit", help="Emit an event for a pack.")
@click.option("--slug", required=True, help="Pack slug.")
@click.option(
    "--type", "event_type", required=True, help="Event type (e.g. state_snapshot)."
)
@click.option("--version", "pack_version", default=None, help="Optional pack version.")
@click.option("--stream-key", default="public", show_default=True)
@click.option(
    "--visibility",
    default="public",
    show_default=True,
    type=click.Choice(["public", "private", "internal"], case_sensitive=False),
)
@click.option(
    "--payload-json",
    default="{}",
    show_default=True,
    help="JSON object payload (inline).",
)
@click.option("--payload-file", default=None, help="Path to a JSON payload file.")
@click.option("--api-url", "api_url", default=None, help="Override Packs API base URL.")
def event_emit_cmd(
    slug: str,
    event_type: str,
    pack_version: str | None,
    stream_key: str,
    visibility: str,
    payload_json: str,
    payload_file: str | None,
    api_url: str | None,
) -> None:
    if payload_file:
        try:
            payload_value = json.loads(Path(payload_file).read_text())
        except OSError as exc:
            raise click.ClickException(
                f"Failed to read --payload-file: {payload_file}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise click.ClickException(
                f"Invalid JSON in --payload-file: {payload_file}"
            ) from exc
    else:
        try:
            payload_value = json.loads(payload_json or "{}")
        except json.JSONDecodeError as exc:
            raise click.ClickException("Invalid JSON in --payload-json") from exc

    if payload_value is None:
        payload_value = {}
    if not isinstance(payload_value, dict):
        raise click.ClickException("Payload must be a JSON object")

    client = PacksApiClient(api_base_url=api_url)
    try:
        resp = client.emit_event(
            slug=slug,
            event_type=event_type,
            pack_version=pack_version,
            payload=payload_value,
            visibility=visibility.lower(),
            stream_key=stream_key.strip() or "public",
        )
    except PacksApiError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": True, "result": resp})
