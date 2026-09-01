# Evolution A/B harness

This harness answers two different questions without changing production gates:

- `race` compares two already-built strategy bundles on one frozen holdout.
- `run` compares two complete self-improvement processes. Each arm gets its own
  SDK workspace, OpenCode data directory, campaign state, and lifecycle-only MCP
  server. The model sees only the chronological development prefix; the runner
  replays the sealed tail through the production paper probation controller.

Nothing in this package applies, approves, trades, or bypasses the economic or
owner promotion gates.

## 1. Freeze a real world

Choose a generation cutoff and a 14–21 day holdout. Keep the sealed directory
outside the world directory and outside every arm workspace.

```bash
wayfinder-bench prepare-world \
  /path/to/.wayfinder/jobs/majors-5m-lab \
  --out .wayfinder_runs/bench/worlds/majors-losing \
  --sealed .wayfinder_runs/bench/sealed/majors-losing \
  --generation-cutoff 2026-08-10T00:00:00Z \
  --holdout-end 2026-08-31T00:00:00Z \
  --world-id majors-losing
```

`world.json`, `bars.json`, the incumbent bundle, and truncated feature stores
are agent-visible. The holdout bytes remain only in the owner-supplied sealed
directory; `world.json` contains their commitment, never their path or values.

## 2. Get a fast side-by-side bundle scorecard

```bash
wayfinder-bench race /path/to/candidate \
  --world .wayfinder_runs/bench/worlds/majors-losing \
  --sealed .wayfinder_runs/bench/sealed/majors-losing \
  --out .wayfinder_runs/bench/races/candidate-v-incumbent
```

The B arm defaults to the world's frozen incumbent. `race.json` is written
before either simulation. Re-run the exact registered question with:

```bash
wayfinder-bench race rerun \
  .wayfinder_runs/bench/races/candidate-v-incumbent
```

The verdict is fixed: A wins only when the 90% paired daily utility-delta LCB is
positive, both sides meet the 10-trade floor, and A's maximum drawdown is no
more than 1.25 times B's.

## 3. Run the first full-loop model experiment

Copy and edit
[`deepseek-v4-pro-vs-flash-max.json`](deepseek-v4-pro-vs-flash-max.json), then:

```bash
wayfinder-bench run \
  examples/bench/deepseek-v4-pro-vs-flash-max.json
```

The first arm is `wayfinder/deepseek-v4-pro`. “DeepSeek Flash Max” is expressed
using model `wayfinder/deepseek-v4-flash` with OpenCode variant `max`; both
interfaces are preflighted before campaign work starts. Four repeats per
arm/world are mandatory for a decision-grade aggregate.

Each race stores copied bundles beside a `results/` directory containing both
full side outputs, `compare.json`, and `report.txt`.

The experiment directory contains the frozen experiment question, one scorecard
per arm/world/seed, prompt and runtime identity hashes, token/tool accounting,
identity and cost-parity checks, the pooled paired-LCB endpoint, and a plain-text
report. If runtime identity differs outside the declared model/variant fields or
token cost differs by more than the registered ratio, the result is invalid—not
a win for either arm.

If a campaign stages no candidate, that run keeps the incumbent and contributes
an exact zero candidate-minus-incumbent endpoint. A staged but invalid holdout
(including the 10-trade participation floor or invariance failure) invalidates
the run instead of being silently scored as zero.

An arm may set its own `sdk_root`. The controller is launched from a private
copy of that SDK, with only the neutral `jobs/bench` adapter overlaid, so prompt
or process comparisons actually execute the named worktree. Any SDK, prompt,
agent, or temperature difference must also be pre-registered in
`allowed_identity_differences`; undeclared drift invalidates the experiment.
