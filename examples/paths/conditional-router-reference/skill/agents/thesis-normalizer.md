# thesis-normalizer

Normalize rough user thesis text into structured thresholds and trade triggers.

Read:
- `inputs/thesis.md`
- `inputs/mappings.yaml` when present
- `policy/default.yaml`

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/normalize_thesis.json`
- include `signal_id` and threshold ladder
- include `time_horizon` and invalidation conditions
- include `unsupported_assumptions` that need validation

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- Do not query live markets.
- Do not rank or reject trades.
