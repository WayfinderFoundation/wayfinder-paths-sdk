// Compaction prompt baseline — copied VERBATIM from opencode upstream so we have
// an exact starting point to modify from.
//
// Source: sst/opencode  packages/core/src/session/compaction.ts
// Commit: 14a5529793a91001ca81c80e96f39533eab79127  (2026-07-07)
// https://github.com/sst/opencode/blob/main/packages/core/src/session/compaction.ts
//
// How opencode compacts today: there is NO system-role prompt. Compaction is a
// single user message assembled by `buildPrompt()` = a one-line preamble +
// `SUMMARY_TEMPLATE` + the conversation context. The two constants below are
// that prompt, word for word. Nothing here is wired in yet — this is the
// unmodified baseline; we decide how to diverge from it next.

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
- Do not mention the summary process or that context was compacted.`

export const buildPrompt = (input: { readonly previousSummary?: string; readonly context: readonly string[] }) =>
  [
    input.previousSummary
      ? `Update the anchored summary below using the conversation history above.\nPreserve still-true details, remove stale details, and merge in the new facts.\n<previous-summary>\n${input.previousSummary}\n</previous-summary>`
      : "Create a new anchored summary from the conversation history.",
    SUMMARY_TEMPLATE,
    ...input.context,
  ].join("\n\n")
