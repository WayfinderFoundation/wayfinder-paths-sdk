from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.models import (
    DEFAULT_FORWARD_FILLS,
    DEFAULT_FORWARD_FUNDING,
    DEFAULT_FORWARD_ORDERS,
    DEFAULT_FORWARD_RUNS,
    DEFAULT_FORWARD_SUMMARY,
    DEFAULT_FORWARD_TICKS,
    DEFAULT_FORWARD_TRADES,
    safe_job_id,
    utc_now_iso,
)
from wayfinder_paths.runner.monitor_state import atomic_write_json

FORWARD_SCHEMA_VERSION = "0.1"
TRADE_METRICS_VERSION = 2
OPERATIONAL_SAFETY_EXIT_REASONS = frozenset(
    {"native_protection_unconfirmed", "native_protection_breach"}
)
FORWARD_FILES = {
    "run": DEFAULT_FORWARD_RUNS,
    "trade": DEFAULT_FORWARD_TRADES,
    "order": DEFAULT_FORWARD_ORDERS,
    "fill": DEFAULT_FORWARD_FILLS,
    "funding": DEFAULT_FORWARD_FUNDING,
    "tick": DEFAULT_FORWARD_TICKS,
}


def forward_exit_category(row: Mapping[str, Any]) -> str:
    """Separate execution containment from strategy-authored exits.

    Operational closes remain in total net PnL because they cost real money,
    but they must not manufacture strategy losses, loss streaks, or evidence
    that a strategy completed another intentional trade.
    """
    explicit = str(row.get("exit_category") or "")
    if explicit:
        return explicit
    reason = str(row.get("exit_reason") or "")
    if not reason:
        raw = row.get("raw") or {}
        metadata = raw.get("intent_metadata") or {}
        reason = str(metadata.get("exit_reason") or "")
    return (
        "operational_safety"
        if reason in OPERATIONAL_SAFETY_EXIT_REASONS
        else "strategy"
    )


def _new_trade_summary() -> dict[str, Any]:
    return {
        "trade_metrics_version": TRADE_METRICS_VERSION,
        "closed_count": 0,
        "strategy_closed_count": 0,
        "operational_closed_count": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": None,
        "net_pnl": 0,
        "operational_net_pnl": 0,
        "current_loss_streak": 0,
    }


def _trade_net_pnl(row: Mapping[str, Any]) -> float | None:
    match row.get("pnl"):
        case Mapping() as pnl:
            raw_pnl = pnl.get("net_usd")
            if raw_pnl is None:
                raw_pnl = pnl.get("net")
        case other:
            raw_pnl = other
    if raw_pnl is None:
        raw_pnl = row.get("net_pnl")
    return float(raw_pnl) if raw_pnl is not None else None


def _apply_trade_summary_row(trades: dict[str, Any], row: Mapping[str, Any]) -> None:
    trades["closed_count"] = int(trades.get("closed_count") or 0) + 1
    net_pnl = _trade_net_pnl(row)
    if net_pnl is not None:
        trades["net_pnl"] = float(trades.get("net_pnl") or 0) + net_pnl

    if forward_exit_category(row) == "operational_safety":
        trades["operational_closed_count"] = (
            int(trades.get("operational_closed_count") or 0) + 1
        )
        if net_pnl is not None:
            trades["operational_net_pnl"] = (
                float(trades.get("operational_net_pnl") or 0) + net_pnl
            )
    else:
        trades["strategy_closed_count"] = (
            int(trades.get("strategy_closed_count") or 0) + 1
        )
        if net_pnl is not None:
            if net_pnl >= 0:
                trades["wins"] = int(trades.get("wins") or 0) + 1
                trades["current_loss_streak"] = 0
            else:
                trades["losses"] = int(trades.get("losses") or 0) + 1
                trades["current_loss_streak"] = (
                    int(trades.get("current_loss_streak") or 0) + 1
                )
        strategy_closed = int(trades.get("strategy_closed_count") or 0)
        trades["win_rate"] = (
            int(trades.get("wins") or 0) / strategy_closed if strategy_closed else None
        )
    trades["last_trade_at"] = row.get("closed_at") or row.get("ts")


def _legacy_safety_unwind_keys(forward_dir: Path) -> set[tuple[str, str]]:
    """Recover pre-reason safety exits from their durable guard events."""
    path = forward_dir / Path(DEFAULT_FORWARD_TICKS).name
    keys: set[tuple[str, str]] = set()
    if not path.exists():
        return keys
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                tick = json.loads(line)
            except ValueError:
                continue
            for event in tick.get("guard_events") or []:
                if (
                    event.get("kind") == "native_protection_failed"
                    and event.get("unwind_status") == "filled"
                    and event.get("symbol")
                    and event.get("timestamp")
                ):
                    keys.add((str(event["symbol"]), str(event["timestamp"])))
    return keys


def _upgrade_trade_summary(summary: dict[str, Any], trades_path: Path) -> bool:
    """Rebuild legacy counters once so existing jobs gain honest categories."""
    current = summary.setdefault("trades", {})
    if current.get("trade_metrics_version") == TRADE_METRICS_VERSION:
        return False
    rebuilt = _new_trade_summary()
    rebuilt_from_rows = False
    legacy_safety_unwinds = _legacy_safety_unwind_keys(trades_path.parent)
    if trades_path.exists():
        with trades_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    key = (
                        str(row.get("symbol") or ""),
                        str(row.get("closed_at") or row.get("timestamp") or ""),
                    )
                    if (
                        forward_exit_category(row) == "strategy"
                        and key in legacy_safety_unwinds
                    ):
                        row["exit_category"] = "operational_safety"
                    _apply_trade_summary_row(rebuilt, row)
                    rebuilt_from_rows = True
    if not rebuilt_from_rows:
        # Compatibility for callers that persisted only summary.json before
        # the recorder owned detailed rows: absent evidence of an operational
        # close, preserve the old counters as strategy outcomes.
        rebuilt.update(
            {
                "closed_count": int(current.get("closed_count") or 0),
                "strategy_closed_count": int(current.get("closed_count") or 0),
                "wins": int(current.get("wins") or 0),
                "losses": int(current.get("losses") or 0),
                "win_rate": current.get("win_rate"),
                "net_pnl": float(current.get("net_pnl") or 0),
                "current_loss_streak": int(current.get("current_loss_streak") or 0),
                "last_trade_at": current.get("last_trade_at"),
            }
        )
    summary["trades"] = {**current, **rebuilt}
    return True


def default_forward_summary(
    job_id: str | None = None, *, inception_at: str | None = None
) -> dict[str, Any]:
    now = utc_now_iso()
    return {
        "schema_version": FORWARD_SCHEMA_VERSION,
        "job_id": job_id,
        "inception_at": inception_at or now,
        "updated_at": now,
        "runs": {
            "count": 0,
            "last_run_at": None,
            "last_decision": None,
            "last_reason": None,
            "error_count": 0,
        },
        "trades": _new_trade_summary(),
        "orders": {"count": 0, "last_order_at": None, "pending_count": 0},
        "fills": {"count": 0, "last_fill_at": None, "fees_total": 0.0},
        # Signed usd funding payments (negative = paid) — real PnL that never
        # appears in trade rows; the reconciliation block needs it.
        "funding": {"count": 0, "total_usd": 0.0, "last_funding_at": None},
    }


def is_forward_empty(forward: dict[str, Any] | None) -> bool:
    """True when no forward telemetry has executed yet — no runs, closed
    trades, orders, or fills, and no recent detail rows. Used to fence the
    prompt's historical backtest block so an agent cannot restate backtest
    numbers as forward performance when there is no forward evidence. The
    `summary` sub-dict is always present (seeded by `default_forward_summary`),
    so the `.get` chains are safe on a partial or missing snapshot."""
    if not isinstance(forward, dict):
        return True
    summary = forward.get("summary") or {}
    counts = (
        (summary.get("runs") or {}).get("count"),
        (summary.get("trades") or {}).get("closed_count"),
        (summary.get("orders") or {}).get("count"),
        (summary.get("fills") or {}).get("count"),
    )
    if any(count for count in counts):
        return False
    for key in ("recent_runs", "recent_trades", "recent_orders", "recent_fills"):
        if forward.get(key):
            return False
    return True


def render_forward_recap(summary: dict[str, Any] | None) -> str:
    """The single sanctioned source of a forward-performance sentence, computed
    deterministically from `forward.summary` (which the ForwardRecorder derives
    from live transactions). The worker cites this verbatim instead of authoring
    its own numbers — forward performance is system-owned, not LLM-narrated."""
    summary = summary or {}
    trades = summary.get("trades") or {}
    runs = summary.get("runs") or {}
    closed = int(trades.get("closed_count") or 0)
    run_count = int(runs.get("count") or 0)
    if closed <= 0:
        if run_count > 0:
            return f"No forward trades yet ({run_count} runs, 0 closed)"
        return "No forward trades yet"
    wins = int(trades.get("wins") or 0)
    losses = int(trades.get("losses") or 0)
    # Stored win_rate is a fraction (wins/closed); render as a whole percent.
    operational = int(trades.get("operational_closed_count") or 0)
    raw_strategy_closed = trades.get("strategy_closed_count")
    strategy_closed = (
        int(raw_strategy_closed) if raw_strategy_closed is not None else closed
    )
    win_rate = trades.get("win_rate")
    win_rate_denominator = strategy_closed if operational else closed
    win_rate_pct = (
        (float(win_rate) * 100.0)
        if win_rate is not None
        else (wins / win_rate_denominator * 100.0)
        if win_rate_denominator
        else 0.0
    )
    net_pnl = float(trades.get("net_pnl") or 0.0)
    if operational:
        safety_label = "safety exit" if operational == 1 else "safety exits"
        if strategy_closed <= 0:
            return (
                f"Forward: 0 strategy closed · {operational} {safety_label} · "
                f"{net_pnl:+g} total net"
            )
        return (
            f"Forward: {strategy_closed} strategy closed · {wins}W/{losses}L · "
            f"{win_rate_pct:.0f}% WR · {operational} {safety_label} · "
            f"{net_pnl:+g} total net"
        )
    return (
        f"Forward: {closed} closed · {wins}W/{losses}L · "
        f"{win_rate_pct:.0f}% WR · {net_pnl:+g} net"
    )


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
        job_id = job_id or os.environ.get("WAYFINDER_HIGH_LEVEL_JOB_ID")
        job_dir = job_dir or os.environ.get("WAYFINDER_JOB_DIR")
        forward_dir = forward_dir or os.environ.get("WAYFINDER_FORWARD_DIR")
        if forward_dir:
            self.forward_dir = Path(forward_dir)
        elif job_dir:
            self.forward_dir = Path(job_dir) / "results" / "forward"
        elif job_id:
            self.forward_dir = (
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
        self.forward_dir.mkdir(parents=True, exist_ok=True)
        self.job_id = job_id or (
            self.forward_dir.parent.parent.name
            if self.forward_dir.name == "forward"
            and self.forward_dir.parent.name == "results"
            else None
        )
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
            match decision:
                case Mapping():
                    decision_payload = dict(decision)
                    if reason and "reason" not in decision_payload:
                        decision_payload["reason"] = reason
                case _:
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

    def record_trade_open(
        self, payload: Mapping[str, Any] | None = None, **fields: Any
    ) -> dict[str, Any]:
        row = _merge_payload(payload, fields)
        row.setdefault("status", "open")
        row.setdefault("event", "trade_open")
        return self.append("trade", row)

    def record_trade_close(
        self, payload: Mapping[str, Any] | None = None, **fields: Any
    ) -> dict[str, Any]:
        row = _merge_payload(payload, fields)
        row.setdefault("status", "closed")
        row.setdefault("event", "trade_close")
        return self.append("trade", row)

    def record_order(
        self, payload: Mapping[str, Any] | None = None, **fields: Any
    ) -> dict[str, Any]:
        return self.append("order", _merge_payload(payload, fields))

    def record_fill(
        self, payload: Mapping[str, Any] | None = None, **fields: Any
    ) -> dict[str, Any]:
        return self.append("fill", _merge_payload(payload, fields))

    def record_funding(
        self, payload: Mapping[str, Any] | None = None, **fields: Any
    ) -> dict[str, Any]:
        """One signed venue funding payment ({time_ms, coin, usdc, ...})."""
        return self.append("funding", _merge_payload(payload, fields))

    def record_tick(
        self, payload: Mapping[str, Any] | None = None, **fields: Any
    ) -> dict[str, Any]:
        """One row per engine tick with everything the reconciler needs to
        replay the decision: bar timestamp, view hash, snapshot, intents,
        fills, and guard events."""
        return self.append("tick", _merge_payload(payload, fields))

    def summary(self) -> dict[str, Any]:
        path = self.forward_dir / Path(DEFAULT_FORWARD_SUMMARY).name
        return _read_json(path, default_forward_summary(self.job_id))

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
        trade_summary_rebuilt = _upgrade_trade_summary(
            summary, self.forward_dir / Path(DEFAULT_FORWARD_TRADES).name
        )
        if kind == "run":
            runs = summary.setdefault("runs", {})
            runs["count"] = int(runs.get("count") or 0) + 1
            runs["last_run_at"] = row.get("ts")
            decision = row.get("decision")
            match decision:
                case dict():
                    runs["last_decision"] = decision.get("action")
                    runs["last_reason"] = decision.get("reason")
                case _:
                    runs["last_decision"] = decision
                    runs["last_reason"] = row.get("reason")
            if str(row.get("status") or "ok").lower() not in {"ok", "success"}:
                runs["error_count"] = int(runs.get("error_count") or 0) + 1
        elif kind == "trade":
            trades = summary.setdefault("trades", {})
            # A legacy rebuild reads the row that append() just wrote, so do
            # not count it a second time.
            if not trade_summary_rebuilt:
                _apply_trade_summary_row(trades, row)
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
            fills["fees_total"] = float(fills.get("fees_total") or 0.0) + float(
                row.get("fee") or 0.0
            )
        elif kind == "funding":
            funding = summary.setdefault("funding", {})
            funding["count"] = int(funding.get("count") or 0) + 1
            funding["total_usd"] = float(funding.get("total_usd") or 0.0) + float(
                row.get("usdc") or 0.0
            )
            funding["last_funding_at"] = row.get("ts")
        elif kind == "tick":
            ticks = summary.setdefault("ticks", {})
            ticks["count"] = int(ticks.get("count") or 0) + 1
            ticks["last_tick_at"] = row.get("ts")
            if row.get("skipped"):
                ticks["skipped_count"] = int(ticks.get("skipped_count") or 0) + 1
            if row.get("guard_events"):
                ticks["guard_event_count"] = int(
                    ticks.get("guard_event_count") or 0
                ) + len(row["guard_events"])
            if row.get("reconciliation"):
                # Last-write-wins: each live tick carries the current
                # trades/fees/funding vs venue-equity decomposition.
                summary["reconciliation"] = row["reconciliation"]
        _write_json(path, summary)


def get_forward_recorder(**kwargs: Any) -> ForwardRecorder:
    return ForwardRecorder(**kwargs)


def load_forward_snapshot(
    job_id: str,
    *,
    job_dir: Path | None = None,
    store: Any | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    # Local import: forward_artifacts imports this module for summary defaults.
    from wayfinder_paths.jobs.forward_artifacts import (
        forward_open_position,
        forward_pnl_breakdown,
    )

    root = job_dir or (store.job_dir(job_id) if store is not None else None)
    if root is None:
        root = Path.cwd() / ".wayfinder" / "jobs" / safe_job_id(job_id)
    forward_dir = root / "results" / "forward"
    summary_path = forward_dir / Path(DEFAULT_FORWARD_SUMMARY).name
    summary = _read_json(summary_path, default_forward_summary(job_id))
    # Enrich the snapshot copy (never the recorder-owned summary.json) with the
    # paper-vs-live split and any open position — the jobs UI reads these from
    # the synced snapshot for its PnL headline.
    summary = {
        **summary,
        **forward_pnl_breakdown(forward_dir),
        "open_position": forward_open_position(forward_dir),
    }
    return {
        "summary": summary,
        # System-owned forward-performance line (from live transactions) that the
        # worker cites verbatim instead of authoring its own numbers.
        "recap": render_forward_recap(summary),
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
    atomic_write_json(path, data, default=str)


def _tail_jsonl(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists() or limit <= 0:
        return []
    # _append_jsonl is the only writer: one JSON object per line, always.
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return [json.loads(line) for line in lines[-limit:] if line.strip()]
