"""BacktestRef: deployment manifest pinning code, data, params, performance, drift tolerances.

This is *not* a strategy spec — logic lives in code modules (signal.py / decide.py).
The ref pins what was actually used to produce the published numbers so we can detect
drift between the deployed strategy and the validated backtest.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

SCHEMA_VERSION = "0.1"

REF_FILENAME = "backtest_ref.json"
CANDIDATE_FILENAME = "backtest_ref.candidate.json"
ARCHIVE_DIRNAME = "archive"


@dataclass
class CodeEntry:
    module: str
    entrypoint: str
    source_sha256: str


@dataclass
class CodeRefs:
    signal: CodeEntry
    decide: CodeEntry | None = None


@dataclass
class VenueRefs:
    perp: bool = True
    hip3: list[str] = field(default_factory=list)


@dataclass
class DataWindow:
    start: str
    end: str
    bars: int | None = None


@dataclass
class DataRefs:
    symbols: list[str]
    interval: str
    window: DataWindow
    fingerprint: str


@dataclass
class ExecutionAssumptions:
    fill_model: str = "next_bar_open"
    slippage_bps: float = 1.0
    fee_bps: float = 4.5
    min_order_usd: float = 10.0


@dataclass
class ProducedBy:
    at: str
    skill: str
    git_sha: str
    ref_hash: str = ""


@dataclass
class BacktestRef:
    schema_version: str
    produced: ProducedBy
    code: CodeRefs
    venues: VenueRefs
    data: DataRefs
    params: dict[str, Any]
    execution_assumptions: ExecutionAssumptions
    performance: dict[str, Any]
    monitoring: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(asdict(self))


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_jsonable(v) for v in obj]
    return obj


def _from_dict(d: dict[str, Any]) -> BacktestRef:
    """Permissive parser. Accepts both the canonical shape (`emit_backtest_ref`)
    and looser hand-authored variants:
      - `source_hashes: {<file>: sha}` as an alias for `code: {signal/decide: {...}}`
      - `data.start_date`/`end_date` instead of `data.window: {start, end}`
      - missing `produced` / `venues` / `data.fingerprint` (sensible defaults)
    """
    # ---- code / source_hashes ----
    code_in = d.get("code")
    if code_in is None and "source_hashes" in d:
        sh = d["source_hashes"] or {}
        sig_sha = next(
            (v for k, v in sh.items() if "signal" in k.lower()), ""
        )
        dec_sha = next(
            (v for k, v in sh.items() if "decide" in k.lower()), ""
        )
        sig_entry = CodeEntry(module="", entrypoint="", source_sha256=sig_sha or "")
        dec_entry = (
            CodeEntry(module="", entrypoint="", source_sha256=dec_sha) if dec_sha else None
        )
        code = CodeRefs(signal=sig_entry, decide=dec_entry)
    elif code_in is None:
        raise KeyError(
            "backtest_ref.json missing 'code' (and 'source_hashes' fallback). "
            "Use `emit_backtest_ref(...)` to produce a canonical ref."
        )
    else:
        code = CodeRefs(
            signal=CodeEntry(**code_in["signal"]),
            decide=CodeEntry(**code_in["decide"]) if code_in.get("decide") else None,
        )

    # ---- produced ----
    if "produced" in d:
        produced = ProducedBy(**d["produced"])
    else:
        produced = ProducedBy(at="", skill="", git_sha="unknown", ref_hash="")

    # ---- venues ----
    venues_in = d.get("venues") or {}
    venues = VenueRefs(
        perp=venues_in.get("perp", True),
        hip3=list(venues_in.get("hip3") or []),
    )

    # ---- data ----
    data_in = d["data"]
    if "window" in data_in:
        window = DataWindow(**data_in["window"])
    else:
        window = DataWindow(
            start=str(data_in.get("start_date") or data_in.get("start") or ""),
            end=str(data_in.get("end_date") or data_in.get("end") or ""),
            bars=data_in.get("bars"),
        )
    data = DataRefs(
        symbols=list(data_in["symbols"]),
        interval=data_in.get("interval", "1h"),
        window=window,
        fingerprint=str(data_in.get("fingerprint") or ""),
    )

    exe = d.get("execution_assumptions") or {}
    # Drop unknown keys so older/newer variants survive each other.
    exe_keys = {f.name for f in ExecutionAssumptions.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    exe = {k: v for k, v in exe.items() if k in exe_keys}

    return BacktestRef(
        schema_version=d.get("schema_version", SCHEMA_VERSION),
        produced=produced,
        code=code,
        venues=venues,
        data=data,
        params=dict(d.get("params") or {}),
        execution_assumptions=ExecutionAssumptions(**exe),
        performance=dict(d.get("performance") or {}),
        monitoring=dict(d.get("monitoring") or {}),
    )


def load_ref(strategy_dir: str | Path) -> BacktestRef:
    path = Path(strategy_dir) / REF_FILENAME
    with path.open() as f:
        return _from_dict(json.load(f))


def hash_module_source(module: str) -> str:
    """SHA256 of the source file for `module` (importable dotted path)."""
    spec = importlib.util.find_spec(module)
    if spec is None or spec.origin is None:
        raise ImportError(f"Cannot locate source for module {module!r}")
    with open(spec.origin, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def fingerprint_frames(*frames: pd.DataFrame) -> str:
    """Deterministic SHA256 over the canonical numpy bytes + index/columns of merged frames.

    Sorts columns and index per frame for stability across run order.
    """
    h = hashlib.sha256()
    for i, df in enumerate(frames):
        canonical = df.sort_index(axis=0).sort_index(axis=1)
        h.update(f"|frame{i}|shape={canonical.shape}|".encode())
        h.update(",".join(map(str, canonical.columns)).encode())
        h.update(b"|index|")
        # int64 ns for datetime indexes, stringified otherwise
        idx = canonical.index
        if isinstance(idx, pd.DatetimeIndex):
            h.update(idx.asi8.tobytes())
        else:
            h.update("\n".join(map(str, idx)).encode())
        h.update(b"|values|")
        h.update(canonical.to_numpy(copy=False).tobytes())
    return h.hexdigest()


def _git_sha() -> str:
    git = shutil.which("git")
    if not git:
        return "unknown"
    try:
        out = subprocess.check_output(
            [git, "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        )
        return out.strip()
    except subprocess.CalledProcessError:
        return "unknown"


def _hash_payload(d: dict[str, Any]) -> str:
    canonical = json.dumps(d, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()


def emit_backtest_ref(
    *,
    strategy_dir: str | Path,
    signal_module: str,
    signal_entrypoint: str,
    decide_module: str | None,
    decide_entrypoint: str | None,
    venues_perp: bool,
    hip3_dexes: list[str],
    symbols: list[str],
    interval: str,
    window_start: str,
    window_end: str,
    bars: int | None,
    data_fingerprint: str,
    params: dict[str, Any],
    execution_assumptions: ExecutionAssumptions,
    performance: dict[str, Any],
    monitoring: dict[str, Any] | None = None,
    skill: str = "backtest-strategy",
) -> Path:
    """Write a candidate ref next to the strategy. Promotion is a separate explicit step."""
    strategy_path = Path(strategy_dir)
    strategy_path.mkdir(parents=True, exist_ok=True)

    decide_entry: CodeEntry | None = None
    if decide_module and decide_entrypoint:
        decide_entry = CodeEntry(
            module=decide_module,
            entrypoint=decide_entrypoint,
            source_sha256=hash_module_source(decide_module),
        )

    ref = BacktestRef(
        schema_version=SCHEMA_VERSION,
        produced=ProducedBy(
            at=datetime.now(UTC).isoformat(),
            skill=skill,
            git_sha=_git_sha(),
        ),
        code=CodeRefs(
            signal=CodeEntry(
                module=signal_module,
                entrypoint=signal_entrypoint,
                source_sha256=hash_module_source(signal_module),
            ),
            decide=decide_entry,
        ),
        venues=VenueRefs(perp=venues_perp, hip3=list(hip3_dexes)),
        data=DataRefs(
            symbols=list(symbols),
            interval=interval,
            window=DataWindow(start=window_start, end=window_end, bars=bars),
            fingerprint=data_fingerprint,
        ),
        params=dict(params),
        execution_assumptions=execution_assumptions,
        performance=dict(performance),
        monitoring=dict(monitoring or {}),
    )

    payload = ref.to_dict()
    # Hash everything except the hash field itself.
    payload_for_hash = json.loads(json.dumps(payload))
    payload_for_hash["produced"]["ref_hash"] = ""
    ref.produced.ref_hash = _hash_payload(payload_for_hash)
    payload["produced"]["ref_hash"] = ref.produced.ref_hash

    out = strategy_path / CANDIDATE_FILENAME
    with out.open("w") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
    return out


def promote_candidate(strategy_dir: str | Path) -> Path:
    """Promote backtest_ref.candidate.json → backtest_ref.json, archiving the previous ref."""
    sd = Path(strategy_dir)
    candidate = sd / CANDIDATE_FILENAME
    if not candidate.exists():
        raise FileNotFoundError(f"No candidate ref at {candidate}")

    target = sd / REF_FILENAME
    if target.exists():
        archive_dir = sd / "backtest_refs" / ARCHIVE_DIRNAME
        archive_dir.mkdir(parents=True, exist_ok=True)
        with target.open() as f:
            prev = json.load(f)
        prev_hash = prev.get("produced", {}).get("ref_hash", "nohash")
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        archived = archive_dir / f"{ts}_{prev_hash[:12]}.json"
        target.rename(archived)

    candidate.rename(target)
    return target
