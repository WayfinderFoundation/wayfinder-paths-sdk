# poly-scout

Find candidate Polymarket markets and score liquidity, spread, history, and clarity.

Read:
- the normalized thesis artifact
- `policy/default.yaml`

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/market_research.json`
- include candidate market title and condition id
- include implied probability, spread, and liquidity score
- include history quality, rule clarity, and rejection reasons

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- Reject markets that fail liquidity or spread checks.
- Do not compile jobs or proxy trades.
