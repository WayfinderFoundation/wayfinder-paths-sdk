"""Per-strategy risk limits — opt-in, hand-edited, never inferred from backtest output.

Loaded by `ActivePerpsStrategy` from `<strategy_dir>/risk_limits.json`. Absent
file == no halts. The parent class checks limits at the top of `update()` and
returns a `(False, reason)` halt rather than raising.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RiskLimits:
    max_drawdown: float | None = None                 # negative decimal, e.g. -0.15
    max_gross_exposure_usd: float | None = None
    max_position_per_symbol_usd: float | None = None
    max_daily_loss_usd: float | None = None
    pause_after_consecutive_losses: int | None = None
    min_rolling_30d_sharpe: float | None = None

    @classmethod
    def load_optional(cls, strategy_dir: str | Path) -> RiskLimits | None:
        path = Path(strategy_dir) / "risk_limits.json"
        if not path.exists():
            return None
        with path.open() as f:
            d = json.load(f)
        # Drop any keys we don't recognise so older configs survive newer code.
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})

    def check(self, snapshot: dict) -> str | None:
        """Return a halt reason string if any limit is breached, else None.

        `snapshot` shape (all optional):
          - drawdown: float (negative; e.g. -0.12)
          - gross_exposure_usd: float
          - positions_usd: dict[str, float]
          - daily_pnl_usd: float
          - consecutive_losses: int
          - rolling_30d_sharpe: float
        """
        if self.max_drawdown is not None:
            dd = snapshot.get("drawdown")
            if dd is not None and dd <= self.max_drawdown:
                return f"max_drawdown breached: {dd:.4f} <= {self.max_drawdown:.4f}"
        if self.max_gross_exposure_usd is not None:
            ge = snapshot.get("gross_exposure_usd")
            if ge is not None and ge > self.max_gross_exposure_usd:
                return f"max_gross_exposure_usd breached: {ge:.2f} > {self.max_gross_exposure_usd:.2f}"
        if self.max_position_per_symbol_usd is not None:
            for sym, val in (snapshot.get("positions_usd") or {}).items():
                if abs(val) > self.max_position_per_symbol_usd:
                    return (
                        f"max_position_per_symbol_usd breached on {sym}: "
                        f"{val:.2f} > {self.max_position_per_symbol_usd:.2f}"
                    )
        if self.max_daily_loss_usd is not None:
            dpl = snapshot.get("daily_pnl_usd")
            if dpl is not None and dpl < -self.max_daily_loss_usd:
                return f"max_daily_loss_usd breached: {dpl:.2f} < -{self.max_daily_loss_usd:.2f}"
        if self.pause_after_consecutive_losses is not None:
            cl = snapshot.get("consecutive_losses")
            if cl is not None and cl >= self.pause_after_consecutive_losses:
                return (
                    f"pause_after_consecutive_losses breached: "
                    f"{cl} >= {self.pause_after_consecutive_losses}"
                )
        if self.min_rolling_30d_sharpe is not None:
            rs = snapshot.get("rolling_30d_sharpe")
            if rs is not None and rs < self.min_rolling_30d_sharpe:
                return f"min_rolling_30d_sharpe breached: {rs:.2f} < {self.min_rolling_30d_sharpe:.2f}"
        return None
