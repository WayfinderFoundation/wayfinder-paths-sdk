"""Genome → executable strategy. ONE generic interpreter runs any genome
through the production engine; the oracle vectorizes the same contract.
Signals and filters are computed via the SAME library builders in both paths
(suffix equality is guaranteed by the library's causality gate), so parity
failures can only come from execution semantics — which is exactly what the
parity test is for."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from wayfinder_paths.jobs.benchmarks.grammar import Genome

INTERPRETER_STRATEGY = '''
import pandas as pd

from wayfinder_paths.jobs.signal_library import SIGNAL_LIBRARY

_SIGNALS = {s.name: s for s in SIGNAL_LIBRARY}


def _filter_ok(name, frame, hour):
    if name == "none":
        return True
    closes = frame["close"]
    if name in ("above_sma50", "below_sma50"):
        if len(closes) < 50:
            return False
        sma = closes.rolling(50).mean().iloc[-1]
        value = closes.iloc[-1]
        return value > sma if name == "above_sma50" else value < sma
    if name in ("high_vol", "low_vol"):
        if len(frame) < 30:
            return False
        tr = (frame["high"] - frame["low"]).rolling(14).mean()
        atr = tr.iloc[-1]
        med = tr.median()
        return atr > med if name == "high_vol" else atr <= med
    if name.startswith("session_"):
        bucket = {"session_a": 0, "session_b": 1, "session_c": 2}[name]
        return hour // 8 == bucket
    return False


def build_strategy(params):
    genome = dict(params.get("genome_spec") or {})
    signal_name = str(genome["signal"])
    direction = str(genome["direction"])
    confirm = str(genome["confirm_filter"])
    exit_family = str(genome["exit_family"])
    exit_params = dict(genome.get("exit_params") or {})
    sizing_family = str(genome["sizing_family"])
    sizing_params = dict(genome.get("sizing_params") or {})
    side_open = "buy" if direction == "long" else "sell"
    side_close = "sell" if direction == "long" else "buy"
    sign = 1.0 if direction == "long" else -1.0

    class Strategy:
        def decide(self, ctx):
            state = ctx.strategy_state
            frame = ctx.view.to_frame()
            bar = ctx.view.latest()
            close = float(bar["close"])
            symbol = str(bar["symbol"])
            hour = int(str(ctx.timestamp)[11:13])
            position = ctx.ledger.positions.get(symbol)

            if position is not None:
                state["held"] = int(state.get("held") or 0) + 1
                entry = float(state.get("entry_price") or close)
                move = sign * (close / entry - 1.0)
                if direction == "long":
                    state["peak"] = max(float(state.get("peak") or close), close)
                    trail_ref = float(state["peak"])
                    trail_move = close / trail_ref - 1.0
                else:
                    state["peak"] = min(float(state.get("peak") or close), close)
                    trail_ref = float(state["peak"])
                    trail_move = -(close / trail_ref - 1.0)
                exit_now = False
                if exit_family == "fixed_time":
                    exit_now = state["held"] >= int(exit_params["hold_bars"])
                elif exit_family == "target_stop":
                    exit_now = move >= float(exit_params["target_pct"]) or (
                        move <= -float(exit_params["stop_pct"])
                    )
                elif exit_family == "trailing":
                    exit_now = trail_move <= -float(exit_params["trail_pct"])
                elif exit_family == "time_stop":
                    exit_now = state["held"] >= int(exit_params["hold_bars"]) or (
                        move <= -float(exit_params["stop_pct"])
                    )
                if exit_now:
                    state["held"] = 0
                    state["entry_price"] = None
                    state["peak"] = None
                    return [
                        {
                            "action": "CLOSE",
                            "venue": "backtest",
                            "symbol": symbol,
                            "side": side_close,
                            "size": position.size,
                            "reduce_only": True,
                        }
                    ]
                return []

            signal = _SIGNALS[signal_name]
            if len(frame) < signal.min_bars:
                return []
            fired = bool(signal.build(frame).iloc[-1])
            if not fired or not _filter_ok(confirm, frame, hour):
                return []
            size = 1.0
            if sizing_family == "vol_target":
                returns = frame["close"].pct_change().tail(20)
                realized = float(returns.std() or 0.0)
                target = float(sizing_params.get("target_vol") or 0.01)
                size = max(0.25, min(4.0, target / realized)) if realized > 0 else 1.0
            state["held"] = 0
            state["entry_price"] = close
            state["peak"] = close
            return [
                {
                    "action": "OPEN",
                    "venue": "backtest",
                    "symbol": symbol,
                    "side": side_open,
                    "size": size,
                }
            ]

    return Strategy()
'''


def compile_genome(genome: Genome, *, fee_bps: float = 4.5) -> dict[str, Any]:
    """Params for the interpreter strategy. `entry_price` is tracked at the
    DECISION close, matching the oracle's convention; fills happen next open
    in both paths, so target/stop distances are measured identically."""
    return {"genome_spec": genome.to_dict(), "fee_bps": fee_bps}


def write_interpreter(workdir: Path) -> Path:
    path = workdir / "genome_interpreter.py"
    path.write_text(INTERPRETER_STRATEGY.lstrip(), encoding="utf-8")
    return path
