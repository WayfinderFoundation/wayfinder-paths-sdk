# Research source verification

Run the existing station with `WAYFINDER_API_KEY` set in the environment (never
put a key in this directory). Isolate OpenCode's global config so no user MCP
servers or plugins are loaded:

```sh
export XDG_CONFIG_HOME="$(mktemp -d)"
export OPENCODE_DISABLE_CLAUDE_CODE=1
export OPENCODE_DISABLE_DEFAULT_PLUGINS=1
export WAYFINDER_EVAL_LLM_URL="https://llm.wayfinder.ai/v1"
export OPENCODE_CONFIG_CONTENT="$(< evals/fixtures/research_source_verification/runtime.json)"
unset WAYFINDER_EVAL_MCP_URL
poetry run python scripts/eval_station.py evals/stations/research_source_verification.yaml --candidate-timeout-seconds 180
```

For a dev API key, use `https://llm-dev.wayfinder.ai/v1` instead. The key must
belong to the selected gateway environment. Authentication errors are failed
evals, regardless of the OpenCode process exit code; inspect the logs.

The runtime overlay is applied after agent markdown; both agents have all tools
denied, no Wayfinder MCP, and the current crypto-research skill loaded as an
instruction. Only LLM inference is live. The station retains separate desktop and
mobile prompts and captures answers under `.wayfinder_runs/eval_station/`.
Review every answer against `rubric.md`; empty `judge_pairs` intentionally skips
pairwise judging because this is an absolute safety regression, not a style vote.

This tests response behavior on supplied evidence, not live retrieval, skill
discovery, notification delivery, or end-to-end deployed instance behavior. Those
still require a dev smoke test after the SDK image bump. No prompt test can
guarantee future model behavior.
