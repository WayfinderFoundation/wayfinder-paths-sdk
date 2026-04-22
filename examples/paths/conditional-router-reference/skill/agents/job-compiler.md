# job-compiler

Compile the validated policy into a monitorable runner job artifact.

Read:
- the risk gate artifact
- `policy/default.yaml`

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/job.json`
- include a runner-compatible job payload
- include poll interval, cooldown, and entry signal names
- include the exact mode approved by the risk gate

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- Do not arm the job if the risk gate returned `draft` or `null`.
- Write the final artifact only after validation passes.
