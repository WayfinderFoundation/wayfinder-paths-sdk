from __future__ import annotations

import contextlib
import random
import socket
import time
from collections.abc import Iterator
from typing import Any

import numpy as np


class PurityViolation(RuntimeError):
    """Raised when decide() touches wall-clock, RNG, or the network."""


def _violation(name: str):
    def _raise(*args: Any, **kwargs: Any):
        raise PurityViolation(
            f"decide() called {name} — strategies must be deterministic. "
            "Use ctx.timestamp for time, seed your own RNG via params, and read "
            "market data only from ctx.view."
        )

    return _raise


@contextlib.contextmanager
def purity_sandbox(
    *,
    network_policy: str = "warn",
    violations: list[str] | None = None,
) -> Iterator[None]:
    """Catch wall-clock / RNG / network calls inside decide().

    The same decide() runs in backtest, paper, and live; anything it reads
    outside ctx makes those modes diverge. time/random violations always raise.
    Network connections raise only under ``network_policy="strict"``; the
    default records the violation into ``violations`` and lets the call through
    (telemetry libraries inside strategies would otherwise hard-break).

    Cannot patch ``datetime.datetime.now`` (immutable C type) — same limitation
    as the core/perps sandbox this is ported from. Similarly, only the
    module-level ``np.random.random``/``np.random.rand`` convenience functions
    (the global unseeded RandomState) are guarded — a reference captured
    before the sandbox (``from numpy.random import random``) escapes the
    patch, and seeded ``np.random.Generator``/``default_rng`` instances are
    deliberately untouched: a strategy seeding its own RNG from params is
    deterministic and allowed.
    """
    real_time = time.time
    real_mono = time.monotonic
    real_rand = random.random
    real_np_random = np.random.random
    real_np_rand = np.random.rand
    real_connect = socket.socket.connect
    time.time = _violation("time.time")  # type: ignore[assignment]
    time.monotonic = _violation("time.monotonic")  # type: ignore[assignment]
    random.random = _violation("random.random")  # type: ignore[assignment]
    np.random.random = _violation("np.random.random")  # type: ignore[assignment]
    np.random.rand = _violation("np.random.rand")  # type: ignore[assignment]

    # named def: installed as socket.socket.connect, needs self + closure state
    def _guarded_connect(self: socket.socket, *args: Any, **kwargs: Any) -> Any:
        if network_policy == "strict":
            raise PurityViolation(
                "decide() opened a network connection — market data must come "
                "from ctx.view; the driver owns all I/O."
            )
        if violations is not None:
            violations.append("decide() opened a network connection")
        return real_connect(self, *args, **kwargs)

    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    try:
        yield
    finally:
        time.time = real_time  # type: ignore[assignment]
        time.monotonic = real_mono  # type: ignore[assignment]
        random.random = real_rand  # type: ignore[assignment]
        np.random.random = real_np_random  # type: ignore[assignment]
        np.random.rand = real_np_rand  # type: ignore[assignment]
        socket.socket.connect = real_connect  # type: ignore[method-assign]
