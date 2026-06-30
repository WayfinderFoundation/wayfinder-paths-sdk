from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from wayfinder_paths.core.clients.OpenCodeClient import OPENCODE_CLIENT
from wayfinder_paths.jobs.application import claim_application, complete_application
from wayfinder_paths.jobs.models import (
    JOB_AUTO_WORKER_AGENT_NAME,
    JOB_WORKER_AGENT_NAME,
    AgentMode,
    normalize_agent_mode,
    utc_now_iso,
)
from wayfinder_paths.jobs.store import JobStore
from wayfinder_paths.jobs.sync import snapshot_job, sync_all_jobs

JOB_RESULT_MARKER = "WAYFINDER_JOB_RESULT "
STABLE_PREFIX_END_MARKER = "## End Stable Cache Prefix"
DYNAMIC_CONTEXT_MARKER = "## Dynamic Wakeup Context"
VOLATILE_STABLE_KEYS = {"created_at", "updated_at", "ts"}


def _read_text(path: Path, *, max_chars: int = 12_000) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def _canonical_json(data: Any, *, max_chars: int | None = None) -> str:
    text = json.dumps(data, indent=2, sort_keys=True, default=str)
    if max_chars is not None and len(text) > max_chars:
        return text[:max_chars] + "\n...<truncated>"
    return text


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _drop_volatile_stable_keys(value: Any) -> Any:
    match value:
        case dict():
            return {
                str(key): _drop_volatile_stable_keys(item)
                for key, item in sorted(value.items())
                if str(key) not in VOLATILE_STABLE_KEYS
            }
        case list():
            return [_drop_volatile_stable_keys(item) for item in value]
        case _:
            return value


def _build_worker_prompt_sections(
    *,
    store: JobStore,
    job_id: str,
    mode: str,
    snapshot: dict[str, Any],
    apply_proposal_id: str | None = None,
) -> dict[str, str]:
    root = store.job_dir(job_id)
    job_data = snapshot.get("job") or store.load(job_id).to_dict()
    memory_md = _read_text(root / "memory.md", max_chars=6000)
    memory_json = store.read_json(job_id, "memory.json", default={}) or {}
    recent_journal = _read_text(root / "journal.jsonl", max_chars=4000)
    stable_payload = {
        "job": _drop_volatile_stable_keys(job_data),
        "memory_json": _drop_volatile_stable_keys(memory_json),
    }
    dynamic_payload = {
        "scorecard": snapshot.get("scorecard") or {},
        "forward": snapshot.get("forward") or {},
        "runner_links": snapshot.get("runner_links") or {},
        "proposals": snapshot.get("proposals") or [],
        "proposal_queue": snapshot.get("proposal_queue") or {},
        "reports": snapshot.get("reports") or {},
    }

    stable_prefix = (
        "Run a Wayfinder job worker wakeup.\n\n"
        f"Mode: {mode}\n"
        "Cache contract:\n"
        "- This prefix is intentionally stable for this job and mode.\n"
        "- Live prices, timestamps, recent logs, reports, and run results appear after "
        f"`{STABLE_PREFIX_END_MARKER}`.\n"
        "- Update durable memory only when the job's standing goals, constraints, "
        "rules, or lessons materially change.\n\n"
        "Rules:\n"
        "- Monitor mode is read-only except reports/memory.\n"
        "- Intervene mode may create candidate proposals under the job bundle, but cannot activate them.\n"
        "- Applying an approved proposal is a separate lifecycle: pending proposals do not pause jobs, "
        "approval only queues application, and runner loops pause only after the apply worker claims the proposal.\n"
        "- Auto mode may execute live trades only inside the configured auto_limits.\n"
        "- Never move funds, send onchain transactions, or execute contracts.\n"
        "- Use structured forward results first (summary, runs, trades, orders, fills); "
        "raw runner logs are fallback/debug only.\n"
        "- Always write/return a compact structured finding.\n\n"
        "Stable job spec:\n"
        f"{_canonical_json(stable_payload, max_chars=12000)}\n\n"
        "Durable job memory:\n"
        f"{memory_md}\n\n"
        f"{STABLE_PREFIX_END_MARKER}\n"
    )
    task_line = (
        f"- Apply approved proposal `{apply_proposal_id}`. The SDK wake path may "
        "have already claimed it, so check `proposal_queue`/proposal application "
        "status first. If it is still queued, call "
        '`core_jobs(action="claim_application", job_id=..., proposal_id=...)`; '
        "if it is applying, do not claim again. Apply edits in the candidate "
        "workspace recorded on the proposal application, not the active workspace. "
        "If the current script entrypoint lives outside the candidate workspace, "
        "copy the active script into the candidate workspace and update the "
        "candidate `job.yaml` so promotion will use the copied script. "
        "The SDK will promote only after deterministic validation succeeds. "
        "Write or preserve a pure-ish `decide_from_snapshot(snapshot, state)` "
        "decision path when feasible, and include scenario fixtures/results that "
        "prove the approved intent contract. Run validation on the claimed "
        "candidate before completion, and rerun it after material candidate edits: "
        '`core_jobs(action="validate_application", job_id=..., proposal_id=...)` '
        "or `poetry run wayfinder job validate-application <job_id> <proposal_id>`. "
        "If validation fails, read the failed checks, patch the same candidate, "
        "and rerun validation inside this same apply wake. Do not complete a "
        "candidate as applied until validation passes. Include validation attempts "
        "in the apply report when checks fail before the final pass. In one final local step write "
        "`reports/apply/latest.json` and call "
        '`core_jobs(action="complete_application", ...)` with applied or failed. '
        "If MCP job tools are unavailable, use the CLI fallback shape "
        "`poetry run wayfinder job complete-application <job_id> <proposal_id> "
        "--status applied --changed-file <relative-job-file> "
        '--validation-json \'{"py_compile":"passed","smoke_run":"passed"}\'`. '
        "Use normal local development tools to apply the change inside the job "
        "bundle: edit/write, shell, Python/YAML helpers, syntax checks, and tests "
        "are allowed. Keep durable candidate changes under the proposal's candidate "
        "directory unless the task explicitly says otherwise. Keep validation "
        "bounded and fit for the patch: syntax/import, smoke, scenario checks, "
        "telemetry preservation, no duplicate async order behavior when relevant, "
        "and no in-progress candle/lookahead behavior for bar-driven strategies. "
        "After the first sufficient validation pass, complete the application "
        "immediately instead of running open-ended exploratory tests. If validation "
        "fails, complete the application as failed so runner loops resume cleanly.\n"
        if apply_proposal_id
        else "- Review the dynamic context against the stable job contract.\n"
    )
    dynamic_context = (
        f"{DYNAMIC_CONTEXT_MARKER}\n"
        "Current snapshot:\n"
        f"{_canonical_json(dynamic_payload, max_chars=12000)}\n\n"
        "Recent journal:\n"
        f"{recent_journal}\n\n"
        "Task:\n"
        f"{task_line}"
        "- Write the appropriate monitor/intervene/auto/apply report.\n"
        "- Emit a user-visible result only for meaningful state transitions, "
        "warnings, proposals, or blocked auto decisions.\n"
    )
    return {
        "prompt": stable_prefix + "\n" + dynamic_context,
        "stable_prefix": stable_prefix,
        "dynamic_context": dynamic_context,
        "stable_prefix_hash": _sha256_text(stable_prefix),
        "dynamic_context_hash": _sha256_text(dynamic_context),
    }


def prepare_job_worker_prompt(
    *,
    store: JobStore,
    job_id: str,
    mode: str,
    apply_proposal_id: str | None = None,
    claim_application_before_prompt: bool = False,
) -> dict[str, Any]:
    """Prepare the exact prompt payload used for a job worker wakeup."""
    job = store.load(job_id)
    mode = normalize_agent_mode(mode) if mode else job.agent_loop.mode
    if mode == "off":
        mode = "monitor"
    mode_typed: AgentMode = mode

    snapshot = snapshot_job(job.id, store=store)
    application_claim: dict[str, Any] | None = None
    if apply_proposal_id and claim_application_before_prompt:
        application_claim = _ensure_application_claimed(
            store,
            job.id,
            apply_proposal_id,
        )
        snapshot = snapshot_job(job.id, store=store)

    prompt_sections = _build_worker_prompt_sections(
        store=store,
        job_id=job.id,
        mode=mode_typed,
        snapshot=snapshot,
        apply_proposal_id=apply_proposal_id,
    )
    return {
        **prompt_sections,
        "job_id": job.id,
        "mode": mode_typed,
        "application_claim": application_claim,
    }


def _emit_job_result(summary: str, job_id: str) -> None:
    print(
        JOB_RESULT_MARKER
        + json.dumps(
            {
                "type": "job_result",
                "severity": "warning",
                "summary": summary,
                "job_id": job_id,
            }
        )
    )


def run_job_worker(
    job_id: str, mode: str = "monitor", *, apply_proposal_id: str | None = None
) -> dict[str, Any]:
    store = JobStore()
    job = store.load(job_id)
    mode = normalize_agent_mode(mode) if mode else job.agent_loop.mode
    if mode == "off":
        mode = "monitor"
    mode_typed: AgentMode = mode

    blocked_reason = (
        _auto_limits_error(job.agent_loop.auto_limits) if mode_typed == "auto" else None
    )
    if blocked_reason:
        report = _write_report(
            store=store,
            job_id=job.id,
            mode=mode_typed,
            status="red",
            summary=f"Auto agent blocked: {blocked_reason}",
            session_id=None,
            queued=False,
            error=blocked_reason,
        )
        _emit_job_result(report["summary"], job.id)
        return report

    session_id = _ensure_worker_session(job.id, mode_typed)
    queued = False
    error: str | None = None
    application_claim: dict[str, Any] | None = None
    prompt_sections: dict[str, Any] | None = None
    if session_id and apply_proposal_id:
        try:
            prompt_sections = prepare_job_worker_prompt(
                store=store,
                job_id=job.id,
                mode=mode_typed,
                apply_proposal_id=apply_proposal_id,
                claim_application_before_prompt=True,
            )
            application_claim = prompt_sections["application_claim"]
        except Exception as exc:
            error = f"Application claim failed: {exc}"
            session_id = None

    if prompt_sections is None:
        prompt_sections = prepare_job_worker_prompt(
            store=store,
            job_id=job.id,
            mode=mode_typed,
            apply_proposal_id=apply_proposal_id,
            claim_application_before_prompt=False,
        )
    prompt = prompt_sections["prompt"]

    if session_id:
        queued = OPENCODE_CLIENT.prompt_async(
            session_id=session_id,
            text=prompt,
            agent=_agent_name_for_mode(mode_typed),
        )
        if not queued:
            error = "OpenCode prompt_async failed"
            if apply_proposal_id and application_claim:
                complete_application(
                    store,
                    job.id,
                    apply_proposal_id,
                    status="failed",
                    error=error,
                )
    else:
        error = error or "OpenCode server unavailable"

    report = _write_report(
        store=store,
        job_id=job.id,
        mode=mode_typed,
        status="green" if queued else "yellow",
        summary=(
            f"{mode_typed} wakeup queued in OpenCode session {session_id}"
            + (f" for proposal {apply_proposal_id}" if apply_proposal_id else "")
            if queued
            else "Worker could not queue an OpenCode wakeup"
        ),
        session_id=session_id,
        queued=queued,
        error=error,
        apply_proposal_id=apply_proposal_id,
        cache={
            "prompt_cache_key": session_id,
            "stable_prefix_hash": prompt_sections["stable_prefix_hash"],
            "dynamic_context_hash": prompt_sections["dynamic_context_hash"],
            "metrics": "not_available",
        },
    )

    if report["status"] != "green":
        _emit_job_result(report["summary"], job.id)
    return report


def _ensure_application_claimed(
    store: JobStore, job_id: str, proposal_id: str
) -> dict[str, Any]:
    proposal = store.load_proposal(job_id, proposal_id)
    if proposal["application"]["status"] == "applying":
        return {"proposal": proposal, "already_claimed": True}
    return claim_application(store, job_id, proposal_id)


def _ensure_worker_session(job_id: str, mode: str) -> str | None:
    if not OPENCODE_CLIENT.healthy():
        return None

    controller_session_id = os.environ.get("OPENCODE_SESSION_ID") or os.environ.get(
        "OPENCODE_SESSIONID"
    )
    agent_name = _agent_name_for_mode(mode)
    try:
        existing = OPENCODE_CLIENT.find_child_session(
            parent_id=controller_session_id,
            title=f"job/{job_id}/{mode}",
        )
        if existing:
            return existing
        return OPENCODE_CLIENT.create_session(
            parent_id=controller_session_id,
            title=f"job/{job_id}/{mode}",
            agent=agent_name,
        )
    except Exception:
        logger.opt(exception=True).debug(
            "Failed to create/find OpenCode job worker session"
        )
        return None


def _write_report(
    *,
    store: JobStore,
    job_id: str,
    mode: AgentMode,
    status: str,
    summary: str,
    session_id: str | None,
    queued: bool,
    error: str | None,
    apply_proposal_id: str | None = None,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report_dir = (
        store.job_dir(job_id) / "reports" / ("apply" if apply_proposal_id else mode)
    )
    report_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "job_id": job_id,
        "mode": mode,
        "status": status,
        "summary": summary,
        "session_id": session_id,
        "queued": queued,
        "error": error,
        "created_at": utc_now_iso(),
    }
    if apply_proposal_id:
        report["apply_proposal_id"] = apply_proposal_id
    if cache is not None:
        report["cache"] = cache
    (report_dir / "latest.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    scorecard_updates: dict[str, Any] = {
        "health": status,
        "last_agent_check_at": report["created_at"],
        "last_agent_mode": mode,
        "last_agent_summary": report["summary"],
    }
    if cache is not None:
        scorecard_updates["last_agent_cache"] = cache
    store.refresh_scorecard(
        job_id,
        scorecard_updates,
    )
    store.append_journal(
        job_id, {"type": "agent_wakeup", "mode": mode, "report": report}
    )

    try:
        sync_all_jobs(store=store)
    except Exception:
        logger.opt(exception=True).debug(
            "Wayfinder job sync failed after worker wakeup"
        )
    return report


def _agent_name_for_mode(mode: str) -> str:
    return JOB_AUTO_WORKER_AGENT_NAME if mode == "auto" else JOB_WORKER_AGENT_NAME


def _auto_limits_error(limits: dict[str, Any]) -> str | None:
    venues = [
        str(v).strip() for v in limits.get("enabled_venues") or [] if str(v).strip()
    ]
    symbols = [
        str(v).strip() for v in limits.get("allowed_symbols") or [] if str(v).strip()
    ]
    markets = [
        str(v).strip() for v in limits.get("allowed_markets") or [] if str(v).strip()
    ]
    if not venues:
        return "enabled_venues must include at least one venue"
    if not symbols and not markets:
        return "allowed_symbols or allowed_markets must include at least one tradable target"
    for key in (
        "max_notional_per_decision",
        "max_daily_notional",
        "max_open_positions",
        "max_open_orders",
    ):
        if float(limits.get(key) or 0) <= 0:
            return f"{key} must be greater than 0"
    return None
