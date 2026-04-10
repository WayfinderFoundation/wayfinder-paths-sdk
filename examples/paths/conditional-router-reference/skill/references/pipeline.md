# Pipeline

This path compiles a conditional trade thesis into a fixed, phase-ordered workflow.

Ordered phases:
1. `intake`
2. `normalize_thesis`
3. parallel fan-out: `market_research`, `proxy_mapping`, `qual_research`
4. `synthesize`
5. `skeptic`
6. `risk_gate`
7. `compile_job`
8. `finalize`

Failure policy:
- retry `market_research` once on retryable errors
- if market research is exhausted, continue into skeptic with partial inputs
- if `risk_gate` fails, stop at `draft` or `null`
- if `compile_job` fails, stop without arming the job

Artifact rule:
- every worker owns exactly one JSON artifact under `.wf-artifacts/$RUN_ID/`
- the orchestrator reads artifacts and owns final synthesis
