from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.models import (
    DEFAULT_FORWARD_FILLS,
    DEFAULT_FORWARD_ORDERS,
    DEFAULT_FORWARD_RUNS,
    DEFAULT_FORWARD_SUMMARY,
    DEFAULT_FORWARD_TRADES,
    safe_job_id,
    utc_now_iso,
)

FORWARD_SCHEMA_VERSION = "0.1"
FORWARD_FILES = {
    "run": DEFAULT_FORWARD_RUNS,
    "trade": DEFAULT_FORWARD_TRADES,
    "order": DEFAULT_FORWARD_ORDERS,
    "fill": DEFAULT_FORWARD_FILLS,
}


def default_forward_summary(job_id: str | None = None) -> dict[str, Any]:
    now = utc_now_iso()
    return {
        "schema_version": FORWARD_SCHEMA_VERSION,
        "job_id": job_id,
        "updated_at": now,
        "runs": {
            "count": 0,
            "last_run_at": None,
            "last_decision": None,
            "last_reason": None,
            "error_count": 0,
        },
        "trades": {
            "closed_count": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "net_pnl": 0,
            "current_loss_streak": 0,
        },
        "orders": {"count": 0, "last_order_at": None, "pending_count": 0},
        "fills": {"count": 0, "last_fill_at": None},
    }


class ForwardRecorder:
    """Optional structured forward telemetry writer for high-level jobs.

    Rows are intentionally loose: the helper adds useful defaults and appends JSONL,
    but strategy-specific dimensions are preserved instead of validated away.
    """

    def __init__(
        self,
        *,
        job_id: str | None = None,
        job_dir: str | Path | None = None,
        forward_dir: str | Path | None = None,
        mode: str | None = None,
        revision: str | None = None,
        run_id: str | None = None,
    ) -> None:
        env_job_id = os.environ.get("WAYFINDER_HIGH_LEVEL_JOB_ID")
        env_job_dir = os.environ.get("WAYFINDER_JOB_DIR")
        env_forward_dir = os.environ.get("WAYFINDER_FORWARD_DIR")
        self.forward_dir = _resolve_forward_dir(
            forward_dir=forward_dir or env_forward_dir,
            job_dir=job_dir or env_job_dir,
            job_id=job_id or env_job_id,
        )
        self.job_id = job_id or env_job_id or _job_id_from_forward_dir(self.forward_dir)
        self.mode = mode if mode is not None else os.environ.get("WAYFINDER_JOB_MODE")
        self.revision = (
            revision
            if revision is not None
            else os.environ.get("WAYFINDER_JOB_REVISION")
        )
        self.run_id = (
            run_id if run_id is not None else os.environ.get("WAYFINDER_RUN_ID")
        )

    def record_run(
        self,
        payload: Mapping[str, Any] | None = None,
        *,
        status: str | None = "ok",
        decision: str | Mapping[str, Any] | None = None,
        reason: str | None = None,
        state: Mapping[str, Any] | None = None,
        metrics: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        row = _merge_payload(payload, fields)
        if status is not None:
            row["status"] = status
        if decision is not None:
            if isinstance(decision, Mapping):
                decision_payload = dict(decision)
                if reason and "reason" not in decision_payload:
                    decision_payload["reason"] = reason
                row["decision"] = decision_payload
            else:
                decision_payload = {"action": str(decision)}
                if reason:
                    decision_payload["reason"] = reason
                row["decision"] = decision_payload
        elif reason:
            row["reason"] = reason
        if state is not None:
            row["state"] = dict(state)
        if metrics is not None:
            row["metrics"] = dict(metrics)
        return self.append("run", row)

    def record_trade(
        self, payload: Mapping[str, Any] | None = None, **fields: Any
    ) -> dict[str, Any]:
        return self.append("trade", _merge_payload(payload, fields))

    def record_order(
        self, payload: Mapping[str, Any] | None = None, **fields: Any
    ) -> dict[str, Any]:
        return self.append("order", _merge_payload(payload, fields))

    def record_fill(
        self, payload: Mapping[str, Any] | None = None, **fields: Any
    ) -> dict[str, Any]:
        return self.append("fill", _merge_payload(payload, fields))

    def append(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if kind not in FORWARD_FILES:
            raise ValueError(f"Unsupported forward record kind: {kind}")
        row = dict(payload)
        row["schema_version"] = str(row.get("schema_version") or FORWARD_SCHEMA_VERSION)
        row["kind"] = kind
        row.setdefault("ts", utc_now_iso())
        if self.job_id:
            row.setdefault("job_id", self.job_id)
        if self.run_id:
            row.setdefault("run_id", self.run_id)
        if self.mode:
            row.setdefault("mode", self.mode)
        if self.revision:
            row.setdefault("revision", self.revision)

        path = self.forward_dir / Path(FORWARD_FILES[kind]).name
        _append_jsonl(path, row)
        self._update_summary(kind, row)
        return row

    def _update_summary(self, kind: str, row: dict[str, Any]) -> None:
        path = self.forward_dir / Path(DEFAULT_FORWARD_SUMMARY).name
        summary = _read_json(path, default_forward_summary(self.job_id))
        summary["schema_version"] = str(
            summary.get("schema_version") or FORWARD_SCHEMA_VERSION
        )
        summary["job_id"] = summary.get("job_id") or self.job_id
        summary["updated_at"] = utc_now_iso()
        if kind == "run":
            runs = summary.setdefault("runs", {})
            runs["count"] = int(runs.get("count") or 0) + 1
            runs["last_run_at"] = row.get("ts")
            runs["last_decision"] = _decision_action(row.get("decision"))
            runs["last_reason"] = _decision_reason(row)
            if str(row.get("status") or "ok").lower() not in {"ok", "success"}:
                runs["error_count"] = int(runs.get("error_count") or 0) + 1
        elif kind == "trade":
            trades = summary.setdefault("trades", {})
            trades["closed_count"] = int(trades.get("closed_count") or 0) + 1
            net_pnl = _extract_net_pnl(row)
            if net_pnl is not None:
                trades["net_pnl"] = float(trades.get("net_pnl") or 0) + net_pnl
                if net_pnl >= 0:
                    trades["wins"] = int(trades.get("wins") or 0) + 1
                    trades["current_loss_streak"] = 0
                else:
                    trades["losses"] = int(trades.get("losses") or 0) + 1
                    trades["current_loss_streak"] = (
                        int(trades.get("current_loss_streak") or 0) + 1
                    )
                closed_count = int(trades.get("closed_count") or 0)
                trades["win_rate"] = (
                    int(trades.get("wins") or 0) / closed_count
                    if closed_count
                    else None
                )
            trades["last_trade_at"] = row.get("closed_at") or row.get("ts")
        elif kind == "order":
            orders = summary.setdefault("orders", {})
            orders["count"] = int(orders.get("count") or 0) + 1
            orders["last_order_at"] = row.get("ts")
            status = str(row.get("status") or "").lower()
            if status in {"open", "pending", "partially_filled", "resting"}:
                orders["pending_count"] = int(orders.get("pending_count") or 0) + 1
        elif kind == "fill":
            fills = summary.setdefault("fills", {})
            fills["count"] = int(fills.get("count") or 0) + 1
            fills["last_fill_at"] = row.get("ts")
        _write_json(path, summary)


def get_forward_recorder(**kwargs: Any) -> ForwardRecorder:
    return ForwardRecorder(**kwargs)


def record_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return get_forward_recorder().record_run(*args, **kwargs)


def record_trade(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return get_forward_recorder().record_trade(*args, **kwargs)


def record_order(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return get_forward_recorder().record_order(*args, **kwargs)


def record_fill(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return get_forward_recorder().record_fill(*args, **kwargs)


def load_forward_snapshot(
    job_id: str,
    *,
    job_dir: Path | None = None,
    store: Any | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    root = job_dir or (store.job_dir(job_id) if store is not None else None)
    if root is None:
        root = Path.cwd() / ".wayfinder" / "jobs" / safe_job_id(job_id)
    forward_dir = root / "results" / "forward"
    summary_path = forward_dir / Path(DEFAULT_FORWARD_SUMMARY).name
    return {
        "summary": _read_json(summary_path, default_forward_summary(job_id)),
        "recent_runs": _tail_jsonl(
            forward_dir / Path(DEFAULT_FORWARD_RUNS).name, limit
        ),
        "recent_trades": _tail_jsonl(
            forward_dir / Path(DEFAULT_FORWARD_TRADES).name, limit
        ),
        "recent_orders": _tail_jsonl(
            forward_dir / Path(DEFAULT_FORWARD_ORDERS).name, limit
        ),
        "recent_fills": _tail_jsonl(
            forward_dir / Path(DEFAULT_FORWARD_FILLS).name, limit
        ),
    }


def _resolve_forward_dir(
    *,
    forward_dir: str | Path | None,
    job_dir: str | Path | None,
    job_id: str | None,
) -> Path:
    if forward_dir:
        path = Path(forward_dir)
    elif job_dir:
        path = Path(job_dir) / "results" / "forward"
    elif job_id:
        path = (
            Path.cwd()
            / ".wayfinder"
            / "jobs"
            / safe_job_id(job_id)
            / "results"
            / "forward"
        )
    else:
        raise RuntimeError(
            "Cannot locate Wayfinder forward directory. Pass forward_dir/job_id or run "
            "inside a compiled Wayfinder job with WAYFINDER_FORWARD_DIR set."
        )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_id_from_forward_dir(path: Path) -> str | None:
    try:
        if path.name == "forward" and path.parent.name == "results":
            return path.parent.parent.name
    except Exception:
        return None
    return None


def _merge_payload(
    payload: Mapping[str, Any] | None, fields: Mapping[str, Any]
) -> dict[str, Any]:
    merged = dict(payload or {})
    merged.update(fields)
    return merged


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists() or limit <= 0:
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[
        -limit:
    ]:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _decision_action(value: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get("action")
    return value


def _decision_reason(row: Mapping[str, Any]) -> Any:
    decision = row.get("decision")
    if isinstance(decision, Mapping):
        return decision.get("reason")
    return row.get("reason")


def _extract_net_pnl(row: Mapping[str, Any]) -> float | None:
    pnl = row.get("pnl")
    if isinstance(pnl, Mapping):
        value = pnl.get("net_usd")
        if value is None:
            value = pnl.get("net")
    else:
        value = pnl
    if value is None:
        value = row.get("net_pnl")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
