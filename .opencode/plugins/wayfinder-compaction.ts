// Wayfinder compaction plugin.
//
// Loading: opencode auto-discovers `.opencode/{plugin,plugins}/*.{ts,js}`
// (sst/opencode packages/core/src/config/plugin/external.ts) — the same
// auto-discovery the old `.opencode/plugins/wayfinder-context.ts` relied on, so
// dropping a `Plugin` export in this directory is all it takes to wire it up.
//
// Baseline prompt is copied VERBATIM from opencode upstream so we diverge from
// an exact reference:
//   Source: sst/opencode packages/core/src/session/compaction.ts
//   Commit: 14a5529793a91001ca81c80e96f39533eab79127 (2026-07-07)
//
// Hookup: the `experimental.session.compacting` hook appends via
// `output.context` — the same mechanism the old wayfinder-context plugin used.
// We append rather than set `output.prompt` on purpose: opencode consumes the
// hook as `compacting.prompt ?? buildPrompt({ previousSummary, context })`
// (packages/opencode/src/session/compaction.ts), and the hook is only handed
// `{ sessionID }`. Setting `output.prompt` would replace buildPrompt entirely
// and drop the anchored-summary merge (the <previous-summary> incremental
// update). Appending keeps that intact and layers our rules on top.
//
// This is still the unmodified opencode baseline — next step is editing
// SUMMARY_TEMPLATE (e.g. re-adding "exclude wallet balances, fetch live").

import type { Plugin } from "@opencode-ai/plugin"

export const SUMMARY_TEMPLATE = `Output exactly the Markdown structure shown inside <template> and keep the section order unchanged. Do not include the <template> tags in your response.
<template>
## Objective
- [one or two brief sentences describing what the user is trying to accomplish]

## Important Details
- [constraints/preferences, decisions and why, important facts/assumptions, exact context needed to continue, or "(none)"]

## Work State
### Completed
- [finished work, verified facts, or changes made; otherwise "(none)"]

### Active
- [current work, partial changes, or investigation state; otherwise "(none)"]

### Blocked
- [blockers, failing commands, or unknowns; otherwise "(none)"]

## Next Move
1. [immediate concrete action, or "(none)"]
2. [next action if known, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
</template>

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, symbols, commands, error strings, URLs, and identifiers when known.
- Do not mention the summary process or that context was compacted.
- Do not carry wallet balances into the summary — the user holds the EOA key and can move funds at any time, so any balance goes stale immediately; always re-pull balances live before reporting them.`

export const WayfinderCompaction: Plugin = async () => ({
  "experimental.session.compacting": async (_input, output) => {
    output.context.push(SUMMARY_TEMPLATE)
  },
})
