from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = "0.2"
JOB_WORKER_AGENT_NAME = "wayfinder-job-worker"
JOB_AUTO_WORKER_AGENT_NAME = "wayfinder-job-auto-worker"
DEFAULT_FORWARD_RUNS = "results/forward/runs.jsonl"
DEFAULT_FORWARD_TRADES = "results/forward/trades.jsonl"
DEFAULT_FORWARD_ORDERS = "results/forward/orders.jsonl"
DEFAULT_FORWARD_FILLS = "results/forward/fills.jsonl"
DEFAULT_FORWARD_SUMMARY = "results/forward/summary.json"

AgentMode = Literal["off", "monitor", "intervene", "auto"]
JobKind = Literal["script_only", "script_agent", "agent_only"]
JobHealth = Literal["green", "yellow", "red", "unknown"]
ProposalStatus = Literal["pending", "approved", "rejected"]
ApplicationStatus = Literal[
    "not_requested",
    "queued",
    "applying",
    "applied",
    "failed",
    "canceled",
]

AGENT_MODE_ALIASES = {
    "improve": "intervene",
    "decide": "auto",
}


def normalize_agent_mode(value: str | None) -> AgentMode:
    raw = str(value or "off").strip().lower()
    raw = AGENT_MODE_ALIASES.get(raw, raw)
    if raw in {"off", "monitor", "intervene", "auto"}:
        return raw  # type: ignore[return-value]
    return "off"


def default_auto_limits() -> dict[str, Any]:
    return {
        "enabled_venues": [],
        "allowed_symbols": [],
        "allowed_markets": [],
        "max_notional_per_decision": 0,
        "max_daily_notional": 0,
        "max_open_positions": 0,
        "max_open_orders": 0,
    }


def infer_job_kind(script_enabled: bool, agent_mode: AgentMode) -> JobKind:
    if script_enabled and agent_mode == "off":
        return "script_only"
    if script_enabled:
        return "script_agent"
    return "agent_only"


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def safe_job_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in value)
    cleaned = "-".join(part for part in cleaned.strip("-_").split("-") if part)
    if not cleaned:
        raise ValueError("job id cannot be empty")
    return cleaned.lower()


@dataclass
class ScriptLoop:
    enabled: bool = False
    runner_job_name: str = ""
    entrypoint: str | None = None
    interval_seconds: int | None = None
    cron_expr: str | None = None
    timezone: str = "UTC"
    timeout_seconds: int = 120
    mode: str = "paper"
    state_key: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ScriptLoop:
        data = dict(data or {})
        return cls(
            enabled=bool(data.get("enabled", False)),
            runner_job_name=str(data.get("runner_job_name") or ""),
            entrypoint=data.get("entrypoint"),
            interval_seconds=data.get("interval_seconds"),
            cron_expr=data.get("cron_expr"),
            timezone=str(data.get("timezone") or "UTC"),
            timeout_seconds=int(data.get("timeout_seconds") or 120),
            mode=str(data.get("mode") or "paper"),
            state_key=str(data.get("state_key") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "runner_job_name": self.runner_job_name,
            "entrypoint": self.entrypoint,
            "interval_seconds": self.interval_seconds,
            "cron_expr": self.cron_expr,
            "timezone": self.timezone,
            "timeout_seconds": self.timeout_seconds,
            "mode": self.mode,
            "state_key": self.state_key,
        }


@dataclass
class AgentLoop:
    enabled: bool = False
    mode: AgentMode = "off"
    runner_job_name: str = ""
    wake_interval_seconds: int | None = None
    cron_expr: str | None = None
    timezone: str = "UTC"
    timeout_seconds: int = 600
    agent_name: str = JOB_WORKER_AGENT_NAME
    opencode_session_policy: str = "child_of_controller"
    triggers: list[str] = field(default_factory=list)
    auto_limits: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> AgentLoop:
        data = dict(data or {})
        mode = normalize_agent_mode(data.get("mode"))
        return cls(
            enabled=bool(data.get("enabled", mode != "off")),
            mode=mode,
            runner_job_name=str(data.get("runner_job_name") or ""),
            wake_interval_seconds=data.get("wake_interval_seconds"),
            cron_expr=data.get("cron_expr"),
            timezone=str(data.get("timezone") or "UTC"),
            timeout_seconds=int(data.get("timeout_seconds") or 600),
            agent_name=str(data.get("agent_name") or JOB_WORKER_AGENT_NAME),
            opencode_session_policy=str(
                data.get("opencode_session_policy") or "child_of_controller"
            ),
            triggers=[str(v) for v in data.get("triggers") or []],
            auto_limits=dict(data.get("auto_limits") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "runner_job_name": self.runner_job_name,
            "wake_interval_seconds": self.wake_interval_seconds,
            "cron_expr": self.cron_expr,
            "timezone": self.timezone,
            "timeout_seconds": self.timeout_seconds,
            "agent_name": self.agent_name,
            "opencode_session_policy": self.opencode_session_policy,
            "triggers": list(self.triggers),
            "auto_limits": dict(self.auto_limits),
        }


@dataclass
class WayfinderJob:
    id: str
    name: str
    job_kind: JobKind = "script_only"
    goal: str = ""
    domain: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    controller: dict[str, Any] = field(default_factory=dict)
    versioning: dict[str, Any] = field(default_factory=dict)
    script_loop: ScriptLoop = field(default_factory=ScriptLoop)
    agent_loop: AgentLoop = field(default_factory=AgentLoop)
    performance: dict[str, Any] = field(default_factory=dict)
    reporting: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        job_id: str,
        *,
        name: str | None = None,
        goal: str = "",
        script: str | None = None,
        interval_seconds: int | None = None,
        cron_expr: str | None = None,
        timezone: str = "UTC",
        timeout_seconds: int = 120,
        agent_mode: AgentMode = "off",
        agent_wake_seconds: int | None = None,
        auto_limits: dict[str, Any] | None = None,
    ) -> WayfinderJob:
        jid = safe_job_id(job_id)
        normalized_mode = normalize_agent_mode(agent_mode)
        script_enabled = bool(script)
        script_loop = ScriptLoop(
            enabled=script_enabled,
            runner_job_name=f"{jid}-script",
            entrypoint=script,
            interval_seconds=interval_seconds,
            cron_expr=cron_expr,
            timezone=timezone,
            timeout_seconds=timeout_seconds,
            state_key=jid.replace("-", "_"),
        )
        agent_enabled = normalized_mode != "off"
        auto_limits_payload = default_auto_limits()
        if auto_limits:
            auto_limits_payload.update(auto_limits)
        agent_loop = AgentLoop(
            enabled=agent_enabled,
            mode=normalized_mode,
            runner_job_name=f"{jid}-agent",
            wake_interval_seconds=agent_wake_seconds
            or (900 if normalized_mode == "auto" else 3600 if agent_enabled else None),
            timezone=timezone,
            triggers=[
                "script_failure",
                "drift_warning",
                "health_red",
                "proposal_created",
            ],
            auto_limits=auto_limits_payload if normalized_mode == "auto" else {},
        )
        return cls(
            id=jid,
            name=name or jid.replace("-", " ").title(),
            job_kind=infer_job_kind(script_enabled, normalized_mode),
            goal=goal,
            versioning={
                "active_revision": None,
                "active_label": "v0.1.0",
                "git_dir": "workspace",
            },
            script_loop=script_loop,
            agent_loop=agent_loop,
            performance={
                "baseline_backtest": "results/backtest/baseline.json",
                "forward_results": DEFAULT_FORWARD_TRADES,
                "forward": {
                    "runs": DEFAULT_FORWARD_RUNS,
                    "trades": DEFAULT_FORWARD_TRADES,
                    "orders": DEFAULT_FORWARD_ORDERS,
                    "fills": DEFAULT_FORWARD_FILLS,
                    "summary": DEFAULT_FORWARD_SUMMARY,
                },
                "drift_policy": {
                    "min_forward_trades": 20,
                    "warn_if_win_rate_delta_below": -0.20,
                    "warn_if_profit_factor_ratio_below": 0.60,
                    "warn_if_trade_frequency_ratio_below": 0.40,
                    "warn_if_loss_streak_gt_backtest_p95": True,
                },
            },
            reporting={
                "chat_on": [
                    "state_transition",
                    "drift_warning",
                    "proposal_created",
                    "health_red",
                    "script_failure",
                ],
                "quiet_on": ["no_change", "normal_success"],
            },
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WayfinderJob:
        script_loop = ScriptLoop.from_dict(data.get("script_loop"))
        agent_loop = AgentLoop.from_dict(data.get("agent_loop"))
        raw_kind = str(data.get("job_kind") or "").strip()
        job_kind: JobKind = (
            raw_kind
            if raw_kind in {"script_only", "script_agent", "agent_only"}
            else infer_job_kind(script_loop.enabled, agent_loop.mode)
        )  # type: ignore[assignment]
        return cls(
            id=safe_job_id(str(data["id"])),
            name=str(data.get("name") or data["id"]),
            job_kind=job_kind,
            goal=str(data.get("goal") or ""),
            domain=data.get("domain"),
            created_at=str(data.get("created_at") or utc_now_iso()),
            updated_at=str(data.get("updated_at") or utc_now_iso()),
            controller=dict(data.get("controller") or {}),
            versioning=dict(data.get("versioning") or {}),
            script_loop=script_loop,
            agent_loop=agent_loop,
            performance=dict(data.get("performance") or {}),
            reporting=dict(data.get("reporting") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "name": self.name,
            "job_kind": self.job_kind,
            "goal": self.goal,
            "domain": self.domain,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "controller": dict(self.controller),
            "versioning": dict(self.versioning),
            "script_loop": self.script_loop.to_dict(),
            "agent_loop": self.agent_loop.to_dict(),
            "performance": dict(self.performance),
            "reporting": dict(self.reporting),
        }

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def workspace_path(self, root: Path) -> Path:
        return root / "workspace"
