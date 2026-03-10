# Writing Pack Skills (`skill/SKILL.md`)

A pack skill is a `SKILL.md` file inside a published pack that lets AI agents (Claude Code, OpenClaw, Codex) understand what the pack does and how to operate it. It is distinct from a README — a README is for humans browsing a repo; SKILL.md is structured for agent consumption.

## File location

The skill file **must** live at `skill/SKILL.md` inside the pack root (next to `wfpack.yaml`).

- `wayfinder pack init` auto-creates it from a template.
- `wayfinder pack doctor --fix` will scaffold a missing one.

## Frontmatter schema

Every SKILL.md starts with YAML frontmatter:

```yaml
---
name: my-pack                       # required — matches wfpack.yaml slug
description: "One-line summary"     # required — what the pack does
license: MIT                        # optional
compatibility: "Requires X and Y"  # optional — runtime/data dependencies
metadata:
  tags: [wayfinder, defi, lending]  # required — discovery tags
---
```

## Required sections

These three sections are the minimum for an agent to discover and use the pack:

### `## When to use`

Bullet list of concrete scenarios. This is the most important section — agents use it for skill discovery and routing.

```markdown
## When to use

- Evaluating whether a VIRTUAL delta-neutral position outperforms USDC lending
- Building yield comparison strategies with regime switching
- Deploying an automated delta-neutral strategy via the Wayfinder SDK
```

### `## What it does`

2–4 sentence plain-English explanation of the pack's behavior. No code, no jargon soup.

### `## Quick start`

A concrete CLI or code snippet that an agent (or human) can run immediately:

```markdown
## Quick start

\```bash
poetry run python scripts/run.py --action update
\```
```

## Optional sections

Include these when they add meaningful detail:

| Section | Use when… |
|---|---|
| `## Strategy logic` | The pack has decision rules, thresholds, or state machines |
| `## Execution` | There are deployment, scheduling, or adapter-wiring details |
| `## Data sources` | The pack consumes external APIs or data feeds |
| `## Customization` | Users can tune parameters, swap assets, or fork the logic |
| `## Signals` | The pack emits signals via `wayfinder pack signal emit` |

## The `references/` pattern

When SKILL.md alone would exceed ~150 lines, push detail into `skill/references/*.md` and link from the relevant section:

```markdown
## Strategy logic

The regime decision compares DN net yield against USDC supply APR…

See [references/strategy-logic.md](references/strategy-logic.md) for the full algorithm and parameter table.
```

Convention:
- **SDK skills** use `rules/` for sub-documents (e.g. `.claude/skills/my-skill/rules/`)
- **Pack skills** use `references/` for sub-documents (e.g. `skill/references/`)

Add a `## References` section at the bottom linking all reference files.

## Anti-patterns

- **Don't just list files.** An agent can read a directory — SKILL.md should explain *intent*, not *structure*.
- **Don't duplicate the README.** README covers install/contributing/license; SKILL.md covers what an agent needs to operate the pack.
- **Don't leave the template unmodified.** The `<!-- placeholder -->` comments from `pack init` must be replaced with real content before publishing.

## Example

Condensed example for a lending monitor pack:

```markdown
---
name: lending-monitor
description: "Monitors lending rates across venues and emits alerts when spreads exceed thresholds."
metadata:
  tags: [wayfinder, lending, monitor, alerts]
---

## When to use

- Tracking lending rate changes across multiple DeFi venues
- Setting up automated alerts when rate spreads exceed a threshold
- Comparing supply APRs across Aave, Compound, and Moonwell

## What it does

Polls lending rates from configured venues every 15 minutes. When the spread
between the best and worst rate exceeds a configurable threshold, it emits a
signal with the current rates and spread.

## Quick start

wayfinder pack signal emit --slug lending-monitor --title "Rate Alert" --message "USDC spread: 2.1%"

## Signals

- `rate-alert` — emitted when spread exceeds threshold (includes venue, asset, spread fields)
```

For a full reference implementation with `references/` files, see the `virtual-delta-neutral` example pack in vault-backend at `examples/packs/virtual-delta-neutral/`.
