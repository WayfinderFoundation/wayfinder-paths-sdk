"""User-trained models as strategy indicators.

Discipline: training happens OFFLINE (a notebook, a script, anywhere);
decide() only runs inference on an artifact shipped in the job's workspace.
Artifacts under `workspace/` are hashed into the workspace revision
(jobs/gating.py), so a model change is a strategy change — it invalidates the
promotion gate exactly like a code edit.

Two artifact formats:

1. Pure-numpy JSON (`LinearModel`) — linear or logistic coefficients, zero
   extra dependencies, deterministic, tiny (loads fast on live per-tick
   strategy reload). `fit_linear_from_frame` trains one without sklearn.
2. Pickled sklearn-compatible estimators (`.joblib` / `.pkl`) via joblib —
   anything exposing `.predict(X)`. Requires `poetry install --with ml`.

Inference is purity-sandbox safe as long as the model does not draw global
randomness at predict time (tree/linear inference does not).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np
import pandas as pd


class PredictsLike(Protocol):
    def predict(self, X: Any) -> Any: ...


@dataclass
class LinearModel:
    """Linear/logistic coefficients with optional feature standardization.

    JSON schema: {"kind", "features", "coef", "intercept", "mean"?, "scale"?}
    """

    kind: str  # "linear" | "logistic"
    features: list[str]
    coef: np.ndarray
    intercept: float
    mean: np.ndarray | None = None
    scale: np.ndarray | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def predict(self, X: Any) -> np.ndarray:
        """Accepts an ndarray of shape (n, len(features)) or a DataFrame
        (columns selected by self.features). Logistic returns probabilities."""
        match X:
            case pd.DataFrame():
                matrix = X[self.features].to_numpy(dtype=float)
            case _:
                matrix = np.asarray(X, dtype=float)
                if matrix.ndim == 1:
                    matrix = matrix.reshape(1, -1)
        if matrix.shape[1] != len(self.features):
            raise ValueError(
                f"expected {len(self.features)} features "
                f"({self.features}), got {matrix.shape[1]}"
            )
        if self.mean is not None and self.scale is not None:
            safe_scale = np.where(self.scale == 0, 1.0, self.scale)
            matrix = (matrix - self.mean) / safe_scale
        raw = matrix @ self.coef + self.intercept
        if self.kind == "logistic":
            return 1.0 / (1.0 + np.exp(-raw))
        return raw

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "features": list(self.features),
            "coef": [float(value) for value in np.asarray(self.coef).ravel()],
            "intercept": float(self.intercept),
        }
        if self.mean is not None:
            payload["mean"] = [float(v) for v in np.asarray(self.mean).ravel()]
        if self.scale is not None:
            payload["scale"] = [float(v) for v in np.asarray(self.scale).ravel()]
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LinearModel:
        kind = str(payload["kind"])
        if kind not in {"linear", "logistic"}:
            raise ValueError(f"unsupported LinearModel kind: {kind!r}")
        mean = payload.get("mean")
        scale = payload.get("scale")
        return cls(
            kind=kind,
            features=[str(name) for name in payload["features"]],
            coef=np.asarray(payload["coef"], dtype=float),
            intercept=float(payload["intercept"]),
            mean=np.asarray(mean, dtype=float) if mean is not None else None,
            scale=np.asarray(scale, dtype=float) if scale is not None else None,
            metadata=dict(payload.get("metadata") or {}),
        )

    def save(self, path: str | Path) -> Path:
        location = Path(path)
        location.parent.mkdir(parents=True, exist_ok=True)
        location.write_text(
            json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
        return location

    @classmethod
    def load(cls, path: str | Path) -> LinearModel:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def load_model(path_or_relative: str | Path, *, module_file: str | None = None) -> Any:
    """Load a model artifact for use inside decide().

    Relative paths resolve against the directory of `module_file` — pass
    `__file__` from the strategy script so `models/foo.json` finds the
    artifact next to the code that ships with it (and shares its revision).

    Dispatch by suffix: `.json` -> LinearModel; `.joblib`/`.pkl` -> joblib
    (lazy import; needs `poetry install --with ml`).
    """
    path = Path(path_or_relative)
    if not path.is_absolute() and module_file:
        path = Path(module_file).parent / path
    suffix = path.suffix.lower()
    if suffix == ".json":
        return LinearModel.load(path)
    if suffix in {".joblib", ".pkl"}:
        try:
            import joblib  # lazy: optional ml dep (poetry install --with ml)
        except ImportError as exc:  # pragma: no cover - exercised via sys.modules
            raise RuntimeError(
                f"joblib is required to load {suffix!r} model artifacts; "
                "run `poetry install --with ml`"
            ) from exc
        # Pickle deserialization executes arbitrary code. Acceptable here ONLY
        # because the artifact ships inside the job's own workspace — the same
        # trust boundary as the strategy script itself, which is arbitrary
        # Python and revision-hashed together with the artifact. Never point
        # this at a downloaded/untrusted file; use the JSON format for models
        # that must cross a trust boundary.
        return joblib.load(path)
    raise ValueError(
        f"unsupported model artifact suffix {suffix!r} for {path} "
        "(expected .json, .joblib, or .pkl)"
    )


def fit_linear_from_frame(
    frame: pd.DataFrame,
    features: Sequence[str],
    target: str,
    *,
    kind: str = "linear",
    standardize: bool = True,
    l2: float = 0.0,
    max_iter: int = 100,
    out_path: str | Path | None = None,
) -> LinearModel:
    """Train a LinearModel from a DataFrame with pure numpy — no sklearn.

    linear: ridge / least squares via the normal equations.
    logistic: deterministic IRLS (zero init, fixed iteration cap) on a 0/1
    target. Both are fully deterministic for identical inputs.
    """
    if kind not in {"linear", "logistic"}:
        raise ValueError(f"unsupported kind: {kind!r}")
    feature_names = [str(name) for name in features]
    clean = frame[[*feature_names, target]].dropna()
    X = clean[feature_names].to_numpy(dtype=float)
    y = clean[target].to_numpy(dtype=float)
    if len(X) == 0:
        raise ValueError("no rows left after dropping NaNs")

    mean = scale = None
    if standardize:
        mean = X.mean(axis=0)
        scale = X.std(axis=0)
        X = (X - mean) / np.where(scale == 0, 1.0, scale)

    design = np.hstack([X, np.ones((len(X), 1))])
    n_params = design.shape[1]
    if kind == "linear":
        penalty = l2 * np.eye(n_params)
        penalty[-1, -1] = 0.0  # never penalize the intercept
        theta = np.linalg.solve(design.T @ design + penalty, design.T @ y)
    else:
        theta = np.zeros(n_params)
        for _ in range(max_iter):
            z = design @ theta
            p = 1.0 / (1.0 + np.exp(-z))
            W = p * (1 - p)
            gradient = design.T @ (p - y) + l2 * np.r_[theta[:-1], 0.0]
            hessian = (design * W[:, None]).T @ design + l2 * np.eye(n_params)
            hessian[-1, -1] -= l2
            step = np.linalg.solve(hessian + 1e-9 * np.eye(n_params), gradient)
            theta = theta - step
            if float(np.max(np.abs(step))) < 1e-10:
                break

    model = LinearModel(
        kind=kind,
        features=feature_names,
        coef=theta[:-1],
        intercept=float(theta[-1]),
        mean=mean,
        scale=scale,
        metadata={"trained_rows": int(len(X)), "l2": float(l2)},
    )
    if out_path is not None:
        model.save(out_path)
    return model
