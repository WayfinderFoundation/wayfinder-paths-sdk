"""Benchmark strategies (source strings written to temp files at run time —
the same file-entrypoint path production candidates use)."""

from __future__ import annotations

DIP_BUYER = '''
def build_strategy(params):
    mode = str(params.get("mode") or "dip")
    dip_pct = float(params.get("dip_pct") or 1.2) / 100.0
    hold_bars = int(params.get("hold_bars") or 2)

    class Strategy:
        def decide(self, ctx):
            state = ctx.strategy_state
            bar = ctx.view.latest()
            close = float(bar["close"])
            closes = state.setdefault("closes", [])
            closes.append(close)
            del closes[:-6]
            symbol = str(bar["symbol"])
            position = ctx.ledger.positions.get(symbol)
            if position is not None:
                state["held"] = int(state.get("held") or 0) + 1
                if state["held"] >= hold_bars:
                    state["held"] = 0
                    return [
                        {
                            "action": "CLOSE",
                            "venue": "hyperliquid",
                            "symbol": symbol,
                            "side": "sell",
                            "size": position.size,
                            "reduce_only": True,
                        }
                    ]
                return []
            entry = False
            if mode == "lucky_hour":
                hour = int(str(ctx.timestamp)[11:13])
                entry = hour == int(params.get("entry_hour") or 17)
            elif len(closes) >= 4:
                entry = closes[-1] / closes[-4] - 1.0 <= -dip_pct
            if entry:
                state["held"] = 0
                return [
                    {
                        "action": "OPEN",
                        "venue": "hyperliquid",
                        "symbol": symbol,
                        "side": "buy",
                        "size": 1,
                    }
                ]
            return []

    return Strategy()
'''

CHURNER = '''
def build_strategy(params):
    class Strategy:
        def decide(self, ctx):
            bar = ctx.view.latest()
            symbol = str(bar["symbol"])
            position = ctx.ledger.positions.get(symbol)
            if position is not None:
                return [
                    {
                        "action": "CLOSE",
                        "venue": "hyperliquid",
                        "symbol": symbol,
                        "side": "sell",
                        "size": position.size,
                        "reduce_only": True,
                    }
                ]
            return [
                {
                    "action": "OPEN",
                    "venue": "hyperliquid",
                    "symbol": symbol,
                    "side": "buy",
                    "size": 1,
                }
            ]

    return Strategy()
'''

TREND_HOLDER = '''
def build_strategy(params):
    direction = str(params.get("direction") or "long")

    class Strategy:
        def decide(self, ctx):
            bar = ctx.view.latest()
            symbol = str(bar["symbol"])
            if symbol not in ctx.ledger.positions:
                return [
                    {
                        "action": "OPEN",
                        "venue": "hyperliquid",
                        "symbol": symbol,
                        "side": "sell" if direction == "short" else "buy",
                        "size": 1,
                    }
                ]
            return []

    return Strategy()
'''
