"""Named gaps in a job's risk parameters, shown before every paper launch
and acknowledged one by one before a live one.

A flag is ``block`` (cannot be acknowledged: governance ceilings), ``warn``
(must be acknowledged before live) or ``info`` (shown only). The catalogue is
deliberately small and mechanical: it reads risk_limits.json, governance
hard constraints, execution params, the strategy source and the latest
validation report — never a backtest verdict.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from wayfinder_paths.core.strategies.risk_limits import RiskLimits
from wayfinder_paths.jobs.gating import clamp_leverage, governance_hard_constraints
from wayfinder_paths.jobs.models import WayfinderJob, utc_now_iso
from wayfinder_paths.jobs.store import JobStore

RISK_FLAGS_PATH = "state/risk_flags.json"
MAX_TIMEOUT_S = 3600
STOP_TOKENS = ("bracket", "stop_loss", "native_stop", "STOP_LOSS")


@dataclass(frozen=True)
class RiskFlag:
    code: str
    severity: str  # block | warn | info
    message: str
    fix: str
    scope: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def risk_flags(job: WayfinderJob, root: Path) -> list[dict[str, Any]]:
    root = Path(root)
    params = dict(job.execution_params or {})
    limits = RiskLimits.load_optional(root / "workspace")
    hard = governance_hard_constraints(root)
    flags: list[RiskFlag] = []

    _effective, ceiling = clamp_leverage(params.get("leverage"), hard)
    if ceiling is not None:
        flags.append(
            RiskFlag(
                "leverage_above_governance",
                "block",
                f"execution_params.leverage {params.get('leverage')} exceeds the governance ceiling {ceiling}",
                f"set execution_params.leverage to {ceiling} or lower",
                "governance",
            )
        )
    timeout_s = int(job.script_loop.timeout_seconds or 0)
    if timeout_s <= 0 or timeout_s > MAX_TIMEOUT_S:
        flags.append(
            RiskFlag(
                "no_timeout",
                "warn",
                f"script_loop.timeout_seconds is {timeout_s}; a tick can run unbounded",
                f"set script_loop.timeout_seconds within (0, {MAX_TIMEOUT_S}]",
                "schedule",
            )
        )

    if job.execution_contract == "jobs_v1":
        flags.extend(_jobs_v1_flags(job, root, params))
    else:
        flags.extend(_freestyle_flags(job, root, limits))

    if (limits is None or limits.max_drawdown is None) and hard.get(
        "max_drawdown"
    ) is None:
        flags.append(
            RiskFlag(
                "no_max_drawdown",
                "warn",
                "no drawdown cap: the job keeps trading through any loss from peak",
                "add max_drawdown (negative decimal, e.g. -0.10) to workspace/risk_limits.json",
                "risk_limits",
            )
        )
    if limits is None or limits.max_daily_loss_usd is None:
        flags.append(
            RiskFlag(
                "no_max_daily_loss",
                "warn",
                "no daily loss cap: one bad day can run to the drawdown cap or beyond",
                "add max_daily_loss_usd to workspace/risk_limits.json",
                "risk_limits",
            )
        )
    if limits is None or limits.max_position_per_symbol_usd is None:
        flags.append(
            RiskFlag(
                "no_position_cap",
                "info",
                "no per-symbol position cap",
                "add max_position_per_symbol_usd to workspace/risk_limits.json",
                "risk_limits",
            )
        )
    if (limits is None or limits.max_gross_exposure_usd is None) and not (
        hard.get("max_gross_exposure_usd") or hard.get("max_gross_exposure")
    ):
        flags.append(
            RiskFlag(
                "unbounded_notional",
                "warn",
                "no gross exposure cap: total notional is unbounded",
                "add max_gross_exposure_usd to workspace/risk_limits.json",
                "risk_limits",
            )
        )
    if limits is None or limits.pause_after_consecutive_losses is None:
        flags.append(
            RiskFlag(
                "no_consecutive_loss_pause",
                "info",
                "no pause after a losing streak",
                "add pause_after_consecutive_losses to workspace/risk_limits.json",
                "risk_limits",
            )
        )
    return [flag.to_dict() for flag in flags]


def _jobs_v1_flags(
    job: WayfinderJob, root: Path, params: dict[str, Any]
) -> list[RiskFlag]:
    flags: list[RiskFlag] = []
    source = _strategy_source(job, root)
    has_stop = params.get("native_stop_required") or any(
        token in source for token in STOP_TOKENS
    )
    if not has_stop:
        flags.append(
            RiskFlag(
                "no_stop_loss",
                "warn",
                "the strategy never emits a stop: positions exit only on signal",
                "attach a bracket to OPEN intents, or require a venue-native stop (execution_params.native_stop_required)",
                "strategy",
            )
        )
    if not params.get("native_stop_required"):
        flags.append(
            RiskFlag(
                "no_native_stop",
                "info",
                "stops (if any) are engine-side; a venue-native stop survives a dead runner",
                "set execution_params.native_stop_required: true on venues that support it",
                "strategy",
            )
        )
    return flags


def _freestyle_flags(
    job: WayfinderJob, root: Path, limits: RiskLimits | None
) -> list[RiskFlag]:
    flags: list[RiskFlag] = []
    report = _read_validation(root)
    section = dict(report.get("freestyle") or report.get("path") or {})
    spec = dict(section.get("spec") or {})
    dry_run = dict(section.get("dry_run") or {})
    intents = [
        intent for intent in dry_run.get("intents") or [] if isinstance(intent, dict)
    ]
    has_max_loss = (
        spec.get("max_loss_usd") is not None
        or any(
            (intent.get("metadata") or {}).get("max_loss") is not None
            for intent in intents
        )
        or (limits is not None and limits.max_daily_loss_usd is not None)
    )
    if not has_max_loss:
        flags.append(
            RiskFlag(
                "no_stop_loss",
                "warn",
                "no action carries max_loss and SPEC.max_loss_usd is unset: nothing bounds a losing position",
                "put max_loss on opening actions or set SPEC.max_loss_usd / risk_limits max_daily_loss_usd",
                "script",
            )
        )
    if not spec.get("halt_when") and limits is None:
        flags.append(
            RiskFlag(
                "no_kill_switch",
                "warn",
                "no halt condition: nothing stops the script except the owner",
                "set SPEC.halt_when or write workspace/risk_limits.json",
                "script",
            )
        )
    if spec.get("max_notional_per_tick") is None:
        flags.append(
            RiskFlag(
                "no_per_tick_notional_cap",
                "warn",
                "no per-tick notional cap: one tick can open unlimited size",
                "set SPEC.max_notional_per_tick",
                "script",
            )
        )
    if dry_run.get("unpapered_actions"):
        flags.append(
            RiskFlag(
                "custom_actions",
                "warn",
                "the script uses ctx.custom: those venue calls cannot be papered and are skipped in paper mode",
                "acknowledge the risk (SPEC.custom_risk_acknowledged) and enable execution_params.freestyle.allow_custom_actions for live",
                "script",
            )
        )
    if report.get("paper_capable") is False:
        flags.append(
            RiskFlag(
                "no_dry_run",
                "warn",
                "this component declares no dry-run support: paper mode cannot exercise it",
                "declare dry_run: supported in the path manifest, or launch live after acknowledging",
                "path",
            )
        )
    return flags


def acknowledged_flags(store: JobStore, job_id: str) -> dict[str, dict[str, Any]]:
    doc = store.read_json(job_id, RISK_FLAGS_PATH, default={}) or {}
    return dict(doc.get("acknowledged") or {})


def acknowledge_risk_flags(
    store: JobStore,
    job_id: str,
    codes: list[str],
    *,
    by: str,
    memo: str | None = None,
) -> dict[str, Any]:
    job = store.load(job_id)
    current = {flag["code"]: flag for flag in risk_flags(job, store.job_dir(job_id))}
    blocked = [
        code for code in codes if current.get(code, {}).get("severity") == "block"
    ]
    if blocked:
        raise ValueError(f"block-level risk flags cannot be acknowledged: {blocked}")
    doc = store.read_json(job_id, RISK_FLAGS_PATH, default={}) or {}
    acknowledged = dict(doc.get("acknowledged") or {})
    stamp = utc_now_iso()
    for code in codes:
        acknowledged[str(code)] = {"by": by, "at": stamp, "memo": memo}
    doc["acknowledged"] = acknowledged
    store.write_json(job_id, RISK_FLAGS_PATH, doc)
    store.append_journal(
        job_id,
        {
            "type": "risk_flags_acknowledged",
            "codes": [str(c) for c in codes],
            "by": by,
            "memo": memo,
        },
    )
    return {"job_id": job_id, "acknowledged": acknowledged}


def unacknowledged_risk_flags(
    store: JobStore, job_id: str, flags: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    acknowledged = acknowledged_flags(store, job_id)
    return {
        "blocking": [flag for flag in flags if flag["severity"] == "block"],
        "unacknowledged": [
            flag
            for flag in flags
            if flag["severity"] == "warn" and flag["code"] not in acknowledged
        ],
    }


def _strategy_source(job: WayfinderJob, root: Path) -> str:
    raw = str(job.script_loop.entrypoint or "")
    if not raw:
        return ""
    path = Path(raw)
    candidates = [root / path] if not path.is_absolute() else [path]
    if ".wayfinder" in path.parts and "workspace" in path.parts:
        candidates.append(root.joinpath(*path.parts[path.parts.index("workspace") :]))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    return ""


def _read_validation(root: Path) -> dict[str, Any]:
    path = root / "reports" / "validation" / "latest.json"
    if not path.exists():
        return {}
    import json

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}
