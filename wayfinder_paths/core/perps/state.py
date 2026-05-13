"""StateStore: per-strategy free-form key/value plus per-update snapshots.

Modes:
- live:      JSON-file backed at .wayfinder/state/<strategy>/state.json,
             with per-update snapshots at snapshots/<bar_ts_iso>.json
- backtest:  in-memory only
- reconcile: read-only access to historical snapshots via snapshot_at()
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

Mode = Literal["live", "backtest", "reconcile"]

STATE_ROOT = Path(".wayfinder/state")
SNAPSHOT_AGE_WARN_DAYS = 30


def _strategy_dir(strategy_name: str) -> Path:
    return STATE_ROOT / strategy_name


def _snapshots_dir(strategy_name: str) -> Path:
    return _strategy_dir(strategy_name) / "snapshots"


def _state_file(strategy_name: str) -> Path:
    return _strategy_dir(strategy_name) / "state.json"


def _ts_to_filename(t: datetime) -> str:
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    # filesystem-safe iso (no colons)
    return t.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ") + ".json"


def _filename_to_ts(name: str) -> datetime:
    stem = name.removesuffix(".json")
    return datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


class StateStore:
    def __init__(self, strategy_name: str, mode: Mode):
        self.strategy_name = strategy_name
        self.mode = mode
        self._data: dict[str, Any] = {}
        if mode == "live":
            self._load_live()

    # ---------- core kv ----------
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        if self.mode == "live":
            self._persist_live()

    def update(self, updates: dict[str, Any]) -> None:
        self._data.update(updates)
        if self.mode == "live":
            self._persist_live()

    def snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._data, default=str))

    # ---------- per-bar snapshots ----------
    def write_snapshot(self, t: datetime) -> Path | None:
        if self.mode != "live":
            return None
        d = _snapshots_dir(self.strategy_name)
        d.mkdir(parents=True, exist_ok=True)
        path = d / _ts_to_filename(t)
        with path.open("w") as f:
            json.dump(self.snapshot(), f, indent=2, default=str)
        return path

    @classmethod
    def snapshot_at(cls, strategy_name: str, t: datetime) -> dict[str, Any]:
        path = _snapshots_dir(strategy_name) / _ts_to_filename(t)
        if not path.exists():
            return {}
        with path.open() as f:
            return json.load(f)

    @classmethod
    def snapshots_in_bar(
        cls,
        strategy_name: str,
        bar_t: datetime,
        bar_interval: timedelta,
    ) -> list[dict[str, Any]]:
        """Snapshots whose ts ∈ `[bar_t, bar_t + bar_interval)`, oldest first.
        Multiple triggers can fire per bar; callers usually take latest state
        but union intents across all of them."""
        if bar_t.tzinfo is None:
            bar_t = bar_t.replace(tzinfo=UTC)
        upper = bar_t + bar_interval
        out: list[dict[str, Any]] = []
        for ts in cls.list_snapshots(strategy_name):
            if ts < bar_t:
                continue
            if ts >= upper:
                break
            out.append(cls.snapshot_at(strategy_name, ts))
        return out

    @classmethod
    def list_snapshots(cls, strategy_name: str) -> list[datetime]:
        d = _snapshots_dir(strategy_name)
        if not d.exists():
            return []
        out: list[datetime] = []
        for p in d.iterdir():
            if p.suffix != ".json":
                continue
            try:
                out.append(_filename_to_ts(p.name))
            except ValueError:
                continue
        out.sort()
        return out

    @classmethod
    def oldest_snapshot_age_days(cls, strategy_name: str) -> float | None:
        snaps = cls.list_snapshots(strategy_name)
        if not snaps:
            return None
        return (datetime.now(UTC) - snaps[0]).total_seconds() / 86400

    def prune_snapshots_before(self, cutoff: datetime) -> int:
        if self.mode != "live":
            raise RuntimeError("prune_snapshots_before only supported in live mode")
        # Loud warning; user runs this knowing strategies often run on cloud VMs.
        print(
            "[StateStore] WARNING: about to delete snapshots before "
            f"{cutoff.isoformat()} for {self.strategy_name!r}. "
            "Back up .wayfinder/state/ if you may need them — "
            "strategies typically run on cloud VMs with no automatic backup."
        )
        d = _snapshots_dir(self.strategy_name)
        if not d.exists():
            return 0
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=UTC)
        deleted = 0
        for p in list(d.iterdir()):
            if p.suffix != ".json":
                continue
            try:
                ts = _filename_to_ts(p.name)
            except ValueError:
                continue
            if ts < cutoff:
                p.unlink()
                deleted += 1
        return deleted

    # ---------- internals ----------
    def _load_live(self) -> None:
        f = _state_file(self.strategy_name)
        if f.exists():
            with f.open() as fh:
                self._data = json.load(fh)

    def _persist_live(self) -> None:
        d = _strategy_dir(self.strategy_name)
        d.mkdir(parents=True, exist_ok=True)
        f = _state_file(self.strategy_name)
        tmp = f.with_suffix(".json.tmp")
        with tmp.open("w") as fh:
            json.dump(self._data, fh, indent=2, default=str)
        tmp.replace(f)
