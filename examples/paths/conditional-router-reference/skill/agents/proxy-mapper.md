# proxy-mapper

Map candidate conditions to direct and proxy expressions using the declared playbooks.

Read:
- the normalized thesis artifact
- `inputs/mappings.yaml`
- `policy/default.yaml`

Write:
- exactly one JSON object to `.wf-artifacts/$RUN_ID/proxy_mapping.json`
- include direct, proxy, and relative-value expressions
- include expression sizing hints from the policy playbooks
- include dependencies on signals or market availability

Rules:
- Do not spawn other agents.
- Do not compile the final answer.
- Do not score market quality.
- Do not skip the null-state lane.
