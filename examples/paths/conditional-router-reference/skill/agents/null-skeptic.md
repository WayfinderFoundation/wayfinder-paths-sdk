# null-skeptic

Compare all candidates to the null state and reject weak edges.

Read:
- all candidate artifacts produced so far
- `policy/default.yaml`

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/skeptic.json`
- include ranked candidate list against the null state
- include clear veto reasons for weak or forced trades
- include the selected playbook or explicit null-state decision

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- Always include a do-nothing lane.
- Reject candidates that do not clear the null-state threshold.
