# qual-researcher

Summarize relevant qualitative context and flag unsupported assumptions.

Read:
- the normalized thesis artifact
- `inputs/thesis.md`
- user notes when present

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/qual_research.json`
- include supporting catalysts and invalidation risks
- include assumptions that remain unverified
- include context that changes sizing confidence

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- Prefer user-supplied material over broad web research.
- Do not make execution decisions.
