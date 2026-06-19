#!/usr/bin/env python3
"""General OpenCode eval station for graph/prompt experiments.

The station intentionally runs candidate arms through the default/top-level OpenCode
agent, matching production use. The judge is a separate OpenCode run with the
`wayfinder-eval-judge` agent and receives only the question, anonymous answers, and
rubric; timing and cost metadata are reported outside the judge prompt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CANDIDATE_MODEL = "wayfinder/deepseek-v4-pro"
DEFAULT_JUDGE_MODEL = "openai/gpt-5.5"
DEFAULT_FALLBACK_JUDGE_MODEL = "wayfinder/deepseek-v4-pro"
DEFAULT_OPENCODE = str(Path.home() / ".opencode" / "bin" / "opencode")
DEFAULT_DB = str(Path.home() / ".local" / "share" / "opencode" / "opencode.db")
DEFAULT_MCP_URL_ENV = "WAYFINDER_EVAL_MCP_URL"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
FINAL_ANSWER_BLOCKLIST = {
    "continue if you have next steps": "CHECKPOINT_CONTINUATION",
    "progress so far": "PROGRESS_CHECKPOINT",
}
WORKSPACE_IGNORE_NAMES = (
    ".git",
    ".venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".env",
    "config.json",
    "htmlcov",
    "dist",
    "build",
    ".coverage",
    "coverage.xml",
)
WORKSPACE_IGNORE_WAYFINDER_RUNS_DIRS = {
    ".scratch",
    "scratch",
    "eval_station",
    "packs",
}


@dataclass
class RunResult:
    id: str
    question_id: str
    variant_id: str
    command: list[str]
    cwd: str
    log_path: str
    answer_path: str
    status: str
    returncode: int | None
    duration_seconds: float
    started_at: str
    finished_at: str
    workspace: str | None = None
    error: str | None = None
    final_answer_issues: list[dict[str, str]] = field(default_factory=list)
    lookup_diagnostics: dict[str, Any] | None = None


@dataclass
class JudgeResult:
    id: str
    question_id: str
    answer_a: str
    answer_b: str
    command: list[str]
    log_path: str
    prompt_path: str
    verdict_path: str | None
    status: str
    returncode: int | None
    duration_seconds: float
    started_at: str
    finished_at: str
    error: str | None = None


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def load_station_config(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"station config must be a mapping: {path}")
    for key in ("questions", "variants"):
        if not isinstance(data.get(key), list) or not data[key]:
            raise ValueError(f"station config needs non-empty `{key}`: {path}")
    return data


def build_candidate_command(
    opencode: str,
    model: str,
    question: str,
    *,
    title: str | None = None,
    directory: str | None = None,
) -> list[str]:
    """Build a production-shaped candidate command.

    Deliberately no `--agent`: candidates enter through OpenCode's default agent.
    Deliberately no injected answer marker, tool budget, or timeout constraint.
    """
    command = [opencode, "run", "-m", model]
    if directory:
        command.extend(["--dir", directory])
    if title:
        command.extend(["--title", title])
    command.append(question)
    return command


def build_judge_command(
    opencode: str,
    model: str,
    prompt: str,
    *,
    title: str | None = None,
) -> list[str]:
    command = [opencode, "run", "--agent", "wayfinder-eval-judge", "-m", model]
    if title:
        command.extend(["--title", title])
    command.append(prompt)
    return command


def build_judge_prompt(
    rubric_text: str,
    question: str,
    answer_a: str,
    answer_b: str,
) -> str:
    return "\n".join(
        [
            "You are a GROUNDED blind judge. First use only your allowed read-only",
            "validation tools for the bounded grounding pass described by your agent",
            "instructions and the rubric. Then score ONLY the two anonymous answer",
            "texts below against the rubric and your observations. Output strict JSON",
            "and stop.",
            "",
            rubric_text.rstrip(),
            "",
            "---",
            "",
            "THE QUESTION:",
            question,
            "",
            "---",
            "",
            "ANSWER A:",
            answer_a,
            "",
            "---",
            "",
            "ANSWER B:",
            answer_b,
            "",
        ]
    )


def copy_workspace(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name in WORKSPACE_IGNORE_NAMES}
        current = Path(directory)
        try:
            relative = current.relative_to(source)
        except ValueError:
            return ignored
        if relative == Path(".wayfinder_runs"):
            ignored.update(
                name for name in names if name in WORKSPACE_IGNORE_WAYFINDER_RUNS_DIRS
            )
        return ignored

    shutil.copytree(
        source,
        destination,
        ignore=ignore,
    )


def initialize_pack_store(workspace: Path) -> None:
    packs = workspace / ".wayfinder_runs" / "packs"
    if packs.exists():
        shutil.rmtree(packs)
    packs.mkdir(parents=True, exist_ok=True)
    (packs / "index.jsonl").write_text("")


def apply_variant_overlays(workspace: Path, variant: Mapping[str, Any]) -> None:
    for overlay in variant.get("overlays", []) or []:
        if not isinstance(overlay, Mapping):
            raise ValueError(f"variant overlay must be a mapping: {variant.get('id')}")
        relative = overlay.get("path")
        if not relative:
            raise ValueError(f"variant overlay missing path: {variant.get('id')}")
        target = workspace / str(relative)
        target.parent.mkdir(parents=True, exist_ok=True)

        if "content_from" in overlay:
            source = workspace / str(overlay["content_from"])
            if not source.exists():
                raise ValueError(f"overlay content_from not found: {source}")
            target.write_text(source.read_text())
            continue

        if "content" in overlay:
            target.write_text(str(overlay["content"]))
            continue

        text = target.read_text() if target.exists() else ""
        for replacement in overlay.get("replace", []) or []:
            old = str(replacement["old"])
            new = str(replacement["new"])
            count = int(replacement.get("count", 1))
            if old not in text:
                raise ValueError(f"replacement text not found in {relative!s}")
            text = text.replace(old, new, count)
        if "append" in overlay:
            append = str(overlay["append"]).strip("\n")
            text = text.rstrip() + "\n\n" + append + "\n"
        target.write_text(text)


def configure_workspace_mcp_url(workspace: Path, mcp_url: str | None) -> None:
    """Point the materialized OpenCode workspace at a specific MCP server.

    Eval candidates run in copied workspaces, so relying on the source checkout's
    untracked `.opencode/opencode.json` is fragile. This keeps MCP selection explicit
    without modifying tracked agent prompts or leaking local config into workspaces.
    """
    if not mcp_url:
        return
    root_config_path = workspace / "opencode.json"
    nested_config_path = workspace / ".opencode" / "opencode.json"
    runtime_config = (
        json.loads(nested_config_path.read_text())
        if nested_config_path.exists()
        else {}
    )
    config_paths = [
        path for path in (root_config_path, nested_config_path) if path.exists()
    ]
    if not config_paths:
        raise FileNotFoundError(f"OpenCode config not found under: {workspace}")

    for config_path in config_paths:
        data = json.loads(config_path.read_text())
        if config_path == root_config_path and runtime_config:
            for key in (
                "permission",
                "compaction",
                "lsp",
                "snapshot",
                "share",
                "autoupdate",
            ):
                if key in runtime_config:
                    data[key] = runtime_config[key]
            runtime_agents = runtime_config.get("agent", {})
            if isinstance(runtime_agents, dict):
                root_agents = data.setdefault("agent", {})
                if not isinstance(root_agents, dict):
                    raise ValueError(f"`agent` must be an object in {config_path}")
                root_agents.update(runtime_agents)

        data.setdefault("model", DEFAULT_CANDIDATE_MODEL)
        data.setdefault("default_agent", "wayfinder")

        mcp = data.setdefault("mcp", {})
        if not isinstance(mcp, dict):
            raise ValueError(f"`mcp` must be an object in {config_path}")
        wayfinder = mcp.setdefault("wayfinder", {})
        if not isinstance(wayfinder, dict):
            raise ValueError(f"`mcp.wayfinder` must be an object in {config_path}")
        wayfinder["type"] = "remote"
        wayfinder["url"] = mcp_url
        wayfinder["enabled"] = True
        config_path.write_text(json.dumps(data, indent=2) + "\n")


def run_process(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    log_path: Path,
    timeout_seconds: int | None = None,
) -> tuple[int | None, float, str | None]:
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("w") as log:
            proc = subprocess.run(
                command,
                cwd=cwd,
                env=dict(env),
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        return proc.returncode, time.monotonic() - started, None
    except subprocess.TimeoutExpired as exc:
        return None, time.monotonic() - started, f"timeout after {exc.timeout}s"


def newest_session_for_title(db_path: Path, title: str) -> str | None:
    if not db_path.exists():
        return None
    con = sqlite3.connect(db_path)
    row = con.execute(
        "SELECT id FROM session WHERE title=? ORDER BY time_updated DESC LIMIT 1",
        (title,),
    ).fetchone()
    return str(row[0]) if row else None


def newest_session_for_question(db_path: Path, question: str) -> str | None:
    if not db_path.exists():
        return None
    con = sqlite3.connect(db_path)
    row = con.execute(
        """SELECT m.session_id FROM part p JOIN message m ON p.message_id = m.id
           WHERE json_extract(p.data,'$.type')='text'
             AND json_extract(p.data,'$.text') LIKE ?
           ORDER BY m.time_created DESC LIMIT 1""",
        (f"%{question[:80]}%",),
    ).fetchone()
    return str(row[0]) if row else None


def harvest_answer_from_db(db_path: Path, *, title: str, question: str) -> str | None:
    session_id = newest_session_for_title(
        db_path, title
    ) or newest_session_for_question(db_path, question)
    if not session_id:
        return None

    con = sqlite3.connect(db_path)
    rows = con.execute(
        """SELECT json_extract(p.data,'$.text')
           FROM part p JOIN message m ON p.message_id = m.id
           WHERE m.session_id=? AND json_extract(p.data,'$.type')='text'
           ORDER BY m.time_created ASC""",
        (session_id,),
    ).fetchall()
    texts = [str(row[0]).strip() for row in rows if row[0] and len(str(row[0])) > 80]
    if not texts:
        return None
    return texts[-1]


def harvest_answer(log_path: Path, db_path: Path, *, title: str, question: str) -> str:
    answer = harvest_answer_from_db(db_path, title=title, question=question)
    if answer:
        return answer
    text = log_path.read_text(errors="replace") if log_path.exists() else ""
    return text.strip() or "(no answer harvested)"


def find_json(text: str) -> dict[str, Any] | None:
    candidates = re.findall(r"\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}", text, re.S)
    for blob in reversed(candidates):
        if '"verdict"' not in blob:
            continue
        try:
            parsed = json.loads(blob)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def extract_polymarket_search_calls(log_text: str) -> list[dict[str, Any]]:
    """Extract Polymarket search calls from an OpenCode candidate log.

    OpenCode generally prints tool arguments but not the full tool response. We use
    these observed queries for a bounded post-run replay that records the SDK lookup
    diagnostics outside the judge prompt.
    """
    calls: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_line in log_text.splitlines():
        line = ANSI_RE.sub("", raw_line)
        if "wayfinder_polymarket_read" not in line:
            continue
        match = re.search(r"wayfinder_polymarket_read\s+(\{.*\})", line)
        if not match:
            continue
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            continue
        if payload.get("action") != "search" or not payload.get("query"):
            continue
        normalized = {
            "query": str(payload["query"]),
            "limit": int(payload.get("limit") or 10),
            "sort": str(payload.get("sort") or "trending"),
            "status": str(payload.get("status") or "active"),
            "candidate_limit": int(payload.get("candidate_limit") or 10),
        }
        key = (
            normalized["query"].lower(),
            normalized["sort"],
            normalized["status"],
        )
        if key in seen:
            continue
        seen.add(key)
        calls.append(normalized)
    return calls


def _compact_lookup_replay(query: str, response: dict[str, Any]) -> dict[str, Any]:
    if not response.get("ok"):
        return {
            "query": query,
            "ok": False,
            "error": response.get("error") or response.get("message") or response,
        }
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    relevance = (
        result.get("relevance") if isinstance(result.get("relevance"), dict) else {}
    )
    candidates = (
        result.get("candidates") if isinstance(result.get("candidates"), list) else []
    )
    top = []
    for candidate in candidates[:3]:
        if not isinstance(candidate, dict):
            continue
        cand_relevance = candidate.get("relevance")
        top.append(
            {
                "slug": candidate.get("slug"),
                "eventSlug": candidate.get("eventSlug"),
                "score": (
                    cand_relevance.get("score")
                    if isinstance(cand_relevance, dict)
                    else None
                ),
            }
        )

    return {
        "query": query,
        "ok": True,
        "mode": relevance.get("mode"),
        "confidence": relevance.get("confidence"),
        "expansionReason": relevance.get("expansionReason"),
        "queriesTried": relevance.get("queriesTried", []),
        "directHydrations": relevance.get("directHydrations", []),
        "eventHydrations": relevance.get("eventHydrations", []),
        "elapsedMs": relevance.get("elapsedMs"),
        "returnedRowsBeforeTruncation": relevance.get("returnedRowsBeforeTruncation"),
        "top": top,
    }


async def _replay_polymarket_searches(
    calls: list[dict[str, Any]],
    *,
    max_queries: int,
) -> list[dict[str, Any]]:
    from wayfinder_paths.mcp.tools.polymarket import polymarket_read

    out: list[dict[str, Any]] = []
    for call in calls[:max_queries]:
        query = str(call["query"])
        try:
            response = await polymarket_read(
                "search",
                query=query,
                limit=min(int(call.get("limit") or 10), 10),
                sort=str(call.get("sort") or "trending"),
                status=str(call.get("status") or "active"),
                candidate_limit=min(int(call.get("candidate_limit") or 10), 10),
                summary=True,
            )
        except Exception as exc:
            out.append({"query": query, "ok": False, "error": repr(exc)})
            continue
        out.append(_compact_lookup_replay(query, response))
    return out


def collect_lookup_diagnostics(
    *,
    log_path: Path,
    root: Path,
    max_queries: int = 3,
) -> dict[str, Any] | None:
    log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
    calls = extract_polymarket_search_calls(log_text)
    if not calls:
        return None
    try:
        from wayfinder_paths.core.config import load_config

        load_config(root / "config.json")
        replays = asyncio.run(
            _replay_polymarket_searches(calls, max_queries=max_queries)
        )
    except Exception as exc:
        replays = [{"ok": False, "error": repr(exc)}]

    new_flow = any(
        bool(
            item.get("mode") or item.get("queriesTried") or item.get("eventHydrations")
        )
        for item in replays
        if isinstance(item, dict)
    )
    return {
        "source": "eval_replay",
        "searchCallCount": len(calls),
        "queriesSeen": [call["query"] for call in calls],
        "replayCount": len(replays),
        "newLookupFlowObserved": new_flow,
        "replays": replays,
    }


def resolve_judge_model(
    requested_model: str,
    *,
    fallback_model: str,
    allow_fallback: bool,
    env: dict[str, str],
) -> str:
    if not requested_model.startswith("openai/"):
        return requested_model
    if env.get("OPENAI_API_KEY"):
        return requested_model

    try:
        from wayfinder_paths.core.config import get_openai_credentials, load_config

        load_config()
        creds = get_openai_credentials()
    except Exception:
        creds = {"api_key": None, "organization": None}

    if creds.get("api_key"):
        env["OPENAI_API_KEY"] = str(creds["api_key"])
    if creds.get("organization"):
        env["OPENAI_ORGANIZATION"] = str(creds["organization"])
    if env.get("OPENAI_API_KEY"):
        return requested_model
    if allow_fallback:
        return fallback_model
    raise RuntimeError(
        f"{requested_model} requires OPENAI_API_KEY or system.openai.api_key. "
        "Set --allow-judge-fallback to use the fallback judge model explicitly."
    )


def resolve_wayfinder_model_env(model: str, env: dict[str, str]) -> None:
    if not model.startswith("wayfinder/"):
        return
    if env.get("WAYFINDER_API_KEY"):
        return

    try:
        from wayfinder_paths.core.config import get_api_key, load_config

        load_config()
        key = get_api_key()
    except Exception:
        key = None

    if key:
        env["WAYFINDER_API_KEY"] = str(key).strip()
        return
    raise RuntimeError(f"{model} requires WAYFINDER_API_KEY or system.api_key.")


def force_eval_wayfinder_api_env(root: Path, env: dict[str, str]) -> None:
    """Pin eval child processes to the checked-out config's production API fields.

    OpenCode and MCP child processes inherit the caller's environment. For evals, a
    stale local/dev env can silently route tools to localhost or strategies-dev and
    make candidate quality look worse than the prompt actually is. The eval station
    should exercise the same top-level agent graph, but with a deterministic API
    endpoint/key source: `system.api_base_url` and `system.api_key`.
    """
    default_config = root / "config.json"
    if not default_config.exists():
        return

    env["WAYFINDER_CONFIG_PATH"] = str(default_config)
    env.pop("WAYFINDER_CONFIG", None)
    env.pop("WAYFINDER_PATHS_API_URL", None)

    try:
        data = json.loads(default_config.read_text())
    except Exception:
        return
    system = data.get("system", {})
    if not isinstance(system, dict):
        return
    api_key = system.get("api_key")
    if api_key:
        env["WAYFINDER_API_KEY"] = str(api_key).strip()


def resolve_judge_pairs(
    variants: list[Mapping[str, Any]], config: Mapping[str, Any]
) -> list[tuple[str, str]]:
    configured = config.get("judge_pairs")
    if configured:
        return [(str(a), str(b)) for a, b in configured]
    ids = [str(variant["id"]) for variant in variants]
    if len(ids) < 2:
        return []
    return [(ids[0], other) for other in ids[1:]]


def detect_final_answer_issues(answer: str) -> list[dict[str, str]]:
    lowered = answer.lower()
    issues: list[dict[str, str]] = []
    for needle, code in FINAL_ANSWER_BLOCKLIST.items():
        if needle in lowered:
            issues.append(
                {
                    "code": code,
                    "message": "Final answer contains checkpoint/followup artifact.",
                    "matched": needle,
                }
            )
    return issues


def _lookup_report_line(variant_id: str, diagnostics: Mapping[str, Any]) -> str:
    flow = "yes" if diagnostics.get("newLookupFlowObserved") else "no"
    calls = diagnostics.get("searchCallCount", 0)
    replay_count = diagnostics.get("replayCount", 0)
    replay_bits = []
    for replay in diagnostics.get("replays", [])[:3]:
        if not isinstance(replay, Mapping):
            continue
        if not replay.get("ok"):
            replay_bits.append(f"{replay.get('query', '?')}: error")
            continue
        top = replay.get("top", [])
        top_slug = None
        if isinstance(top, list) and top and isinstance(top[0], Mapping):
            top_slug = top[0].get("slug")
        mode = replay.get("mode") or "unknown"
        confidence = replay.get("confidence") or "unknown"
        hydrated = len(replay.get("eventHydrations", []) or []) + len(
            replay.get("directHydrations", []) or []
        )
        replay_bits.append(
            f"{replay.get('query')}: {mode}/{confidence}, hydrated={hydrated}, top={top_slug}"
        )
    detail = "; ".join(replay_bits) if replay_bits else "no replay details"
    return (
        f"- `{variant_id}`: searchCalls={calls}, replayed={replay_count}, "
        f"newLookupFlow={flow}; {detail}"
    )


def write_markdown_report(report: Mapping[str, Any], path: Path) -> None:
    lines = [
        f"# Eval Station Report: {report['station']}",
        "",
        f"- Started: {report['started_at']}",
        f"- Candidate model: `{report['candidate_model']}`",
        f"- Judge model: `{report['judge_model']}`",
        "",
    ]
    for question in report["questions"]:
        lines.extend([f"## {question['id']}", "", question["text"], ""])
        lines.append("| Variant | Status | Duration | Answer |")
        lines.append("| --- | --- | ---: | --- |")
        for variant_id, result in question["variants"].items():
            duration = f"{result['duration_seconds']:.1f}s"
            answer = Path(result["answer_path"]).name
            status = result["status"]
            if result.get("final_answer_issues"):
                status = f"{status} ({len(result['final_answer_issues'])} final-answer issue)"
            lines.append(f"| `{variant_id}` | {status} | {duration} | `{answer}` |")
        lookup_rows = [
            (variant_id, result.get("lookup_diagnostics"))
            for variant_id, result in question["variants"].items()
            if result.get("lookup_diagnostics")
        ]
        if lookup_rows:
            lines.extend(["", "Lookup diagnostics:"])
            for variant_id, diagnostics in lookup_rows:
                lines.append(_lookup_report_line(variant_id, diagnostics))
        if question.get("judgments"):
            lines.extend(
                [
                    "",
                    "| Judge Pair | Verdict | Scores | Duration |",
                    "| --- | --- | --- | ---: |",
                ]
            )
            for judgment in question["judgments"]:
                verdict = judgment.get("verdict", {})
                scores = verdict.get("scores", {}) if isinstance(verdict, dict) else {}
                score_text = ""
                if scores:
                    score_text = (
                        f"A={scores.get('A', {}).get('total')} / "
                        f"B={scores.get('B', {}).get('total')}"
                    )
                lines.append(
                    f"| `{judgment['answer_a']}` vs `{judgment['answer_b']}` | "
                    f"{verdict.get('verdict') if isinstance(verdict, dict) else 'n/a'} | "
                    f"{score_text} | {judgment['duration_seconds']:.1f}s |"
                )
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n")


def run_station(args: argparse.Namespace) -> Path:
    root = repo_root()
    config_path = Path(args.config).expanduser()
    if not config_path.is_absolute():
        config_path = root / config_path
    config = load_station_config(config_path)

    station_name = str(config.get("name") or config_path.stem)
    candidate_model = str(config.get("candidate_model") or DEFAULT_CANDIDATE_MODEL)
    requested_judge_model = str(config.get("judge_model") or DEFAULT_JUDGE_MODEL)
    fallback_judge_model = str(
        config.get("judge_fallback_model") or DEFAULT_FALLBACK_JUDGE_MODEL
    )
    output_base = Path(config.get("output_dir") or ".wayfinder_runs/eval_station")
    if not output_base.is_absolute():
        output_base = root / output_base
    run_dir = output_base / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=True)

    rubric_path = Path(config.get("rubric") or "scripts/eval_sports_ab_judge.md")
    if not rubric_path.is_absolute():
        rubric_path = root / rubric_path
    rubric_text = rubric_path.read_text()

    env = os.environ.copy()
    force_eval_wayfinder_api_env(root, env)
    resolve_wayfinder_model_env(candidate_model, env)
    judge_model = resolve_judge_model(
        requested_judge_model,
        fallback_model=fallback_judge_model,
        allow_fallback=args.allow_judge_fallback,
        env=env,
    )
    resolve_wayfinder_model_env(judge_model, env)
    opencode = args.opencode_bin or os.environ.get("OPENCODE_BIN") or DEFAULT_OPENCODE
    db_path = Path(args.opencode_db or os.environ.get("OPENCODE_DB", DEFAULT_DB))
    mcp_url = args.mcp_url or os.environ.get(DEFAULT_MCP_URL_ENV)

    report: dict[str, Any] = {
        "station": station_name,
        "config": str(config_path),
        "started_at": utc_now(),
        "candidate_model": candidate_model,
        "judge_model": judge_model,
        "questions": [],
    }

    variants = config["variants"]
    variant_map = {str(variant["id"]): variant for variant in variants}
    judge_pairs = resolve_judge_pairs(variants, config)

    for question in config["questions"]:
        qid = str(question["id"])
        qtext = str(question["text"])
        question_dir = run_dir / qid
        question_dir.mkdir(parents=True, exist_ok=True)
        question_report: dict[str, Any] = {
            "id": qid,
            "text": qtext,
            "variants": {},
            "judgments": [],
        }

        for variant in variants:
            variant_id = str(variant["id"])
            run_id = f"{station_name}-{qid}-{variant_id}-{uuid.uuid4().hex[:8]}"
            log_path = question_dir / f"{variant_id}.log"
            answer_path = question_dir / f"{variant_id}.md"
            started_at = utc_now()
            workspace_path: Path | None = None
            error: str | None = None

            with tempfile.TemporaryDirectory(prefix=f"wf-eval-{variant_id}-") as tmp:
                workspace = Path(tmp) / "repo"
                copy_workspace(root, workspace)
                initialize_pack_store(workspace)
                apply_variant_overlays(workspace, variant)
                configure_workspace_mcp_url(workspace, mcp_url)
                workspace_path = workspace
                command = build_candidate_command(
                    opencode,
                    candidate_model,
                    qtext,
                    title=run_id,
                    directory=str(workspace),
                )
                returncode, duration, error = run_process(
                    command,
                    cwd=workspace,
                    env=env,
                    log_path=log_path,
                    timeout_seconds=args.candidate_timeout_seconds,
                )
                answer = harvest_answer(log_path, db_path, title=run_id, question=qtext)
                answer_path.write_text(answer)
                final_answer_issues = detect_final_answer_issues(answer)
                lookup_diagnostics = (
                    collect_lookup_diagnostics(
                        log_path=log_path,
                        root=root,
                    )
                    if args.lookup_diagnostics
                    else None
                )
                if args.keep_workspaces:
                    kept = question_dir / f"workspace_{variant_id}"
                    if kept.exists():
                        shutil.rmtree(kept)
                    shutil.copytree(workspace, kept)
                    workspace_path = kept

            status = "ok" if returncode == 0 and not error else "failed"
            result = RunResult(
                id=run_id,
                question_id=qid,
                variant_id=variant_id,
                command=command,
                cwd=str(workspace_path) if workspace_path else "",
                log_path=str(log_path),
                answer_path=str(answer_path),
                status=status,
                returncode=returncode,
                duration_seconds=duration,
                started_at=started_at,
                finished_at=utc_now(),
                workspace=str(workspace_path) if args.keep_workspaces else None,
                error=error,
                final_answer_issues=final_answer_issues,
                lookup_diagnostics=lookup_diagnostics,
            )
            question_report["variants"][variant_id] = asdict(result)

        for answer_a, answer_b in judge_pairs:
            if answer_a not in variant_map or answer_b not in variant_map:
                raise ValueError(f"unknown judge pair: {answer_a}, {answer_b}")
            ans_a_path = Path(question_report["variants"][answer_a]["answer_path"])
            ans_b_path = Path(question_report["variants"][answer_b]["answer_path"])
            prompt = build_judge_prompt(
                rubric_text,
                qtext,
                ans_a_path.read_text(errors="replace"),
                ans_b_path.read_text(errors="replace"),
            )
            judge_id = (
                f"{station_name}-{qid}-{answer_a}-vs-{answer_b}-{uuid.uuid4().hex[:8]}"
            )
            prompt_path = question_dir / f"judge_{answer_a}_vs_{answer_b}.prompt.md"
            log_path = question_dir / f"judge_{answer_a}_vs_{answer_b}.log"
            verdict_path = question_dir / f"judge_{answer_a}_vs_{answer_b}.json"
            prompt_path.write_text(prompt)
            command = build_judge_command(opencode, judge_model, prompt, title=judge_id)
            started_at = utc_now()
            returncode, duration, error = run_process(
                command,
                cwd=root,
                env=env,
                log_path=log_path,
                timeout_seconds=args.judge_timeout_seconds,
            )
            verdict = find_json(log_path.read_text(errors="replace"))
            if verdict is not None:
                verdict_path.write_text(json.dumps(verdict, indent=2))
            judge_result = JudgeResult(
                id=judge_id,
                question_id=qid,
                answer_a=answer_a,
                answer_b=answer_b,
                command=command,
                log_path=str(log_path),
                prompt_path=str(prompt_path),
                verdict_path=str(verdict_path) if verdict is not None else None,
                status="ok" if returncode == 0 and verdict is not None else "failed",
                returncode=returncode,
                duration_seconds=duration,
                started_at=started_at,
                finished_at=utc_now(),
                error=error
                if error
                else (None if verdict is not None else "no verdict JSON"),
            )
            row = asdict(judge_result)
            row["verdict"] = verdict
            question_report["judgments"].append(row)

        report["questions"].append(question_report)

    report["finished_at"] = utc_now()
    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2))
    write_markdown_report(report, run_dir / "report.md")
    return report_path


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "config",
        nargs="?",
        default="evals/stations/sports_graphs.yaml",
        help="YAML station config path",
    )
    parser.add_argument("--opencode-bin", default=None)
    parser.add_argument("--opencode-db", default=None)
    parser.add_argument(
        "--candidate-timeout-seconds",
        type=int,
        default=None,
        help="Optional safety cap; omitted by default so candidate runs are natural.",
    )
    parser.add_argument(
        "--judge-timeout-seconds",
        type=int,
        default=None,
        help="Optional safety cap for judge runs; omitted by default.",
    )
    parser.add_argument(
        "--allow-judge-fallback",
        action="store_true",
        help="Explicitly allow non-GPT-5.5 judge fallback when OpenAI auth is missing.",
    )
    parser.add_argument(
        "--keep-workspaces",
        action="store_true",
        help="Copy each materialized variant workspace into the run directory.",
    )
    parser.add_argument(
        "--mcp-url",
        default=None,
        help=(
            "Override the OpenCode wayfinder MCP URL inside each isolated workspace. "
            f"Defaults to ${DEFAULT_MCP_URL_ENV} when set."
        ),
    )
    parser.add_argument(
        "--lookup-diagnostics",
        action="store_true",
        help=(
            "Replay observed Polymarket search calls after candidate runs and include "
            "lookup diagnostics in the report. Disabled by default to avoid extra live "
            "API calls during normal evals."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    report_path = run_station(args)
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
