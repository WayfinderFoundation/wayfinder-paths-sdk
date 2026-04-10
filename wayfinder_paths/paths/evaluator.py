from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from wayfinder_paths.paths.doctor import PathDoctorError, run_doctor
from wayfinder_paths.paths.manifest import PathManifest, PathManifestError
from wayfinder_paths.paths.pipeline import STANDARD_OUTPUT_CONTRACT
from wayfinder_paths.paths.renderer import PathSkillRenderError, render_skill_exports


class PathEvalError(Exception):
    pass


@dataclass(frozen=True)
class EvalIssue:
    name: str
    passed: bool
    message: str
    path: str | None = None


@dataclass(frozen=True)
class PathEvalReport:
    ok: bool
    slug: str | None
    issues: list[EvalIssue]


def _load_structured_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _fixture_path(fixtures_dir: Path, fixture_name: str) -> Path:
    direct = fixtures_dir / fixture_name
    if direct.exists():
        return direct
    for suffix in (".yaml", ".yml", ".json"):
        candidate = fixtures_dir / f"{fixture_name}{suffix}"
        if candidate.exists():
            return candidate
    raise PathEvalError(f"Fixture not found: {fixture_name}")


def _lookup(value: Any, dotted: str) -> Any:
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(dotted)
        current = current[part]
    return current


def _evaluate_fixture_case(
    *,
    path_dir: Path,
    eval_path: Path,
    payload: dict[str, Any],
) -> list[EvalIssue]:
    fixture_name = str(payload.get("fixture") or "").strip()
    if not fixture_name:
        raise PathEvalError(f"{eval_path} is missing fixture")
    fixture_path = _fixture_path(path_dir / "tests" / "fixtures", fixture_name)
    fixture = _load_structured_file(fixture_path) or {}
    if not isinstance(fixture, dict):
        raise PathEvalError(f"{fixture_path} must contain an object")
    output = fixture.get("output", fixture)
    if not isinstance(output, dict):
        raise PathEvalError(f"{fixture_path} output must be an object")

    issues: list[EvalIssue] = []
    missing_fields = [
        field for field in STANDARD_OUTPUT_CONTRACT if field not in output
    ]
    if missing_fields:
        issues.append(
            EvalIssue(
                name=str(payload.get("name") or eval_path.stem),
                passed=False,
                message=(
                    "Fixture output is missing required contract fields: "
                    + ", ".join(missing_fields)
                ),
                path=str(fixture_path),
            )
        )
    assertions = payload.get("assert") or {}
    if assertions and not isinstance(assertions, dict):
        raise PathEvalError(f"{eval_path} assert must be an object")
    for dotted, expected in assertions.items():
        case_name = f"{payload.get('name') or eval_path.stem}:{dotted}"
        try:
            actual = _lookup(output, str(dotted))
        except KeyError:
            issues.append(
                EvalIssue(
                    name=case_name,
                    passed=False,
                    message=f"Missing dotted path in fixture output: {dotted}",
                    path=str(fixture_path),
                )
            )
            continue
        issues.append(
            EvalIssue(
                name=case_name,
                passed=actual == expected,
                message=f"expected {expected!r}, got {actual!r}",
                path=str(fixture_path),
            )
        )
    if not assertions and not missing_fields:
        issues.append(
            EvalIssue(
                name=str(payload.get("name") or eval_path.stem),
                passed=True,
                message="fixture output matches the standard contract",
                path=str(fixture_path),
            )
        )
    return issues


def _evaluate_host_render_case(
    *,
    path_dir: Path,
    eval_path: Path,
    payload: dict[str, Any],
) -> list[EvalIssue]:
    hosts_raw = payload.get("hosts") or []
    if not isinstance(hosts_raw, list) or not hosts_raw:
        raise PathEvalError(f"{eval_path} hosts must be a non-empty list")
    hosts = [str(item).strip() for item in hosts_raw if str(item).strip()]
    report = render_skill_exports(path_dir=path_dir, hosts=hosts)
    issues: list[EvalIssue] = []
    expected_files = payload.get("expected_files") or []
    if expected_files and not isinstance(expected_files, list):
        raise PathEvalError(f"{eval_path} expected_files must be a list")
    for rel_path in expected_files:
        rel = Path(str(rel_path))
        found = any(
            (info.export_dir / rel).exists() for info in report.exports.values()
        )
        issues.append(
            EvalIssue(
                name=f"{payload.get('name') or eval_path.stem}:{rel.as_posix()}",
                passed=found,
                message="rendered" if found else "missing rendered host file",
                path=str(eval_path),
            )
        )
    if not expected_files:
        issues.append(
            EvalIssue(
                name=str(payload.get("name") or eval_path.stem),
                passed=bool(report.rendered_hosts),
                message=f"rendered hosts: {', '.join(report.rendered_hosts)}",
                path=str(eval_path),
            )
        )
    return issues


def run_path_eval(*, path_dir: Path) -> PathEvalReport:
    path_dir = path_dir.resolve()
    manifest_path = path_dir / "wfpath.yaml"
    if not manifest_path.exists():
        raise PathEvalError(f"Missing wfpath.yaml in {path_dir}")

    try:
        manifest = PathManifest.load(manifest_path)
    except PathManifestError as exc:
        raise PathEvalError(str(exc)) from exc

    try:
        doctor_report = run_doctor(path_dir=path_dir, fix=False, overwrite=False)
    except PathDoctorError as exc:
        raise PathEvalError(str(exc)) from exc

    issues: list[EvalIssue] = [
        EvalIssue(
            name="doctor",
            passed=doctor_report.ok,
            message="doctor passed" if doctor_report.ok else "doctor reported errors",
            path=str(manifest_path),
        )
    ]

    eval_dir = path_dir / "tests" / "evals"
    if not eval_dir.exists():
        raise PathEvalError(f"Missing tests/evals in {path_dir}")

    for eval_path in sorted(eval_dir.glob("*")):
        if not eval_path.is_file() or eval_path.suffix not in {
            ".yaml",
            ".yml",
            ".json",
        }:
            continue
        payload = _load_structured_file(eval_path) or {}
        if not isinstance(payload, dict):
            raise PathEvalError(f"{eval_path} must contain an object")
        eval_type = str(payload.get("type") or "fixture").strip()
        if eval_type == "fixture":
            issues.extend(
                _evaluate_fixture_case(
                    path_dir=path_dir,
                    eval_path=eval_path,
                    payload=payload,
                )
            )
            continue
        if eval_type == "host_render":
            try:
                issues.extend(
                    _evaluate_host_render_case(
                        path_dir=path_dir,
                        eval_path=eval_path,
                        payload=payload,
                    )
                )
            except PathSkillRenderError as exc:
                issues.append(
                    EvalIssue(
                        name=str(payload.get("name") or eval_path.stem),
                        passed=False,
                        message=str(exc),
                        path=str(eval_path),
                    )
                )
            continue
        raise PathEvalError(f"Unsupported eval type in {eval_path}: {eval_type}")

    return PathEvalReport(
        ok=all(issue.passed for issue in issues),
        slug=manifest.slug,
        issues=issues,
    )
