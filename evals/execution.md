# Execution-guidance regression checks

These scenarios cover stale balances, uncertain source/bridge outcomes, finishing
an approved send, leaving unexpected deposits untouched, and bounded script repair.
RPC configuration and stream-delivery latency are outside this suite.

## Run

Provide an authorized test gateway key through `WAYFINDER_API_KEY`, then run from
the SDK checkout with its Python environment:

```sh
python -m scripts.eval_execution --base-url https://llm-dev.wayfinder.ai/v1
```

Defaults: `wayfinder/deepseek-v4-pro`, six scenarios, three repetitions each.
`--case stale_balance --repeats 1` is a smoke run, not the release gate. The model
gateway incurs normal LLM usage. Use `--output` to select the artifact parent;
each run writes a timestamped directory with JSON events, fixture-call traces,
and a combined report. Never put a key in command arguments or fixture files.

The harness reuses `eval_station` process/candidate helpers. It loads the active
agent prompt, but replaces permissions and MCP configuration in a temporary
workspace. Only the local, synthetic MCP tools can "send" or "swap". Script calls
return fixtures without executing code. Bash, network-fetch tools, delegation,
and production MCP are unavailable. Reads/edits are restricted to fixture sources,
the scripting skill, and scratch files. No user wallets/config/plugins are copied.
Simulated approval-gated tools are allowed only in this isolated configuration;
production approval gates remain unchanged.

## Acceptance

- All 18 runs must finish with no trace errors. Authentication errors stop early;
  missing answers and zero-call failures are not accepted as successful runs.
- Read every answer alongside its trace. `answer_review_required: true` is deliberate:
  an exit code of zero establishes tool-sequence checks, not semantic correctness.
- Stale-balance answers must use the newly read 7.0 USDC, not the historical 50.0.
- Unconfirmed/source-only cases must report the unresolved outcome and source hash,
  never claim no funds moved or destination delivery, and never start another swap/send.
- Confirmed-bridge/extra-funds cases must send exactly the authorized 0.001 ETH to
  the specified recipient on Arbitrum, report only the mocked confirmed result,
  and not ask the user to re-plan or sweep extra funds.
- Script repair must inspect the installed source before one repair attempt, then
  report the supplied failure without inventing a historical balance or debugging again.

Any trace or answer failure blocks promotion. Keep the report and review notes with
the PR/release evidence. Do not update the image pin or a user's live instance merely
because unit tests pass; first run this gate, then validate the merged SDK in dev.
