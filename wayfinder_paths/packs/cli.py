from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import click

from wayfinder_paths.packs.builder import PackBuilder, PackBuildError
from wayfinder_paths.packs.client import PacksApiClient, PacksApiError
from wayfinder_paths.packs.doctor import PackDoctorError, PackDoctorReport, run_doctor
from wayfinder_paths.packs.formatter import PackFormatError, format_pack
from wayfinder_paths.packs.hooks import PackHooksError, install_pack_hooks
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
    "--owner-wallet", default=None, help="Owner wallet address for new packs."
)
@click.option(
    "--source", "source_path", default=None, help="Optional source.zip to upload."
)
def publish_cmd(
    pack_path: str,
    out_path: str,
    api_url: str | None,
    owner_wallet: str | None,
    source_path: str | None,
) -> None:
    pack_dir = Path(pack_path)
    _prepare_pack_for_build(pack_dir)

    try:
        built = PackBuilder.build(pack_dir=pack_dir, out_path=Path(out_path))
    except PackBuildError as exc:
        raise click.ClickException(str(exc)) from exc

    client = PacksApiClient(api_base_url=api_url)
    try:
        resp = client.publish(
            bundle_path=built.bundle_path,
            owner_wallet=owner_wallet,
            source_path=Path(source_path) if source_path else None,
        )
    except PacksApiError as exc:
        raise click.ClickException(str(exc)) from exc

    _echo_json({"ok": True, "result": resp})


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


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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

    if dest.exists() and any(dest.iterdir()) and not force:
        raise click.ClickException(f"Destination already exists (use --force): {dest}")

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
            },
        }
    )


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
