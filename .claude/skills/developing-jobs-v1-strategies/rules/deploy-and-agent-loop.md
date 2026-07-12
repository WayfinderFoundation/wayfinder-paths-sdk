# Deploy & the agent loop — what happens after a strategy goes live

A deployed job is more than a scheduled script: it can carry its own agent
that wakes on the runner schedule, reads the live (forward) results, and acts
according to its **agent mode**. Present the mode choice as part of every
deploy offer — it is the user's main lever over how much autonomy they grant.

## The four modes (and how to describe them to a user)

| Mode | Say it as | What the wake agent may do |
|---|---|---|
| `off` | "just run it" | Nothing — the script trades on its schedule; no agent wakes. |
| `monitor` | "watch and report" | Read-only except reports/memory: reviews forward runs, trades, orders, fills each wake; writes an honest structured finding (PnL vs expectation, drift, warnings). **Recommended default.** |
| `intervene` | "watch and suggest" | Monitor, plus it may draft **candidate proposals** under the job bundle. It cannot activate them — the user approves or rejects. |
| `auto` | "fully automatic, within limits" | May execute live trades, but only inside the configured `auto_limits`. Never moves funds, never sends on-chain transactions. |

Set with `core_jobs(action="set_agent_mode", job_id=…, mode=…)` (CLI:
`wayfinder job agent set-mode <id> monitor [--wake N]`). The UI shows the mode buttons
plus "Latest Check" (the last wake's finding) and "Proposals" (pending count)
on the job panel — explain those in the same plain terms.

## The proposal lifecycle (intervene/auto) — the user stays the gate

1. The wake agent drafts a proposal carrying the candidate revision, backtest
   stats, and gate state (`core_jobs(action="propose")` — never hand-written
   JSON).
2. A **pending proposal changes nothing** — the job keeps running.
3. User approval only **queues** the application; the runner loop pauses only
   after the apply worker claims it.
4. The claimed application is validated against the real engine
   (`validate_application`) before it can complete and go live
   (`complete_application`). A candidate that fails validation never
   activates.

So even in `intervene`, live behavior changes only after: agent proposes →
user approves → engine validates. Say exactly that to a cautious user.

## Forward-evidence discipline (why the reports can be trusted)

The wake prompt enforces the same honesty bar as backtesting: with zero
forward runs/trades/orders/fills, the agent is explicitly forbidden from
reporting any win rate, PnL, or trade count as a forward result. Wakes are
non-amnesic — candidate/decision ledgers persist across wakes, so a logged
dead idea is not re-explored unchanged.

## What to recommend

- Fresh deploy of a validated strategy → `monitor`. Revisit after a week of
  forward reports.
- User wants the strategy to improve itself over time → `intervene`, with the
  proposal lifecycle explained (they approve everything).
- `auto` only when the user asks for it, `auto_limits` are set, and they
  understand it acts without asking. It still cannot move funds.
- Forward results ≠ backtest: the first days of forward data are usually too
  few trades to judge — the monitor's reports will say so rather than invent
  a verdict.
