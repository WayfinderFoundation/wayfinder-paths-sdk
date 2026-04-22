# Conditional Router Reference

Use this skill when the user describes a conditional macro, political, or thematic thesis and wants it converted into monitorable trades and a job.

Read `references/pipeline.md`, `references/signals.md`, and `references/risk.md` before starting.

Execution order:
1. Spawn `thesis-normalizer`, `poly-scout`, `proxy-mapper`, and `qual-researcher` in parallel.
2. Synthesize candidate expressions from their artifacts.
3. Run `null-skeptic`, then `risk-verifier`, then `job-compiler`.

Rules:
1. You are the only orchestrator.
2. Workers are leaf agents and must not spawn more agents.
3. Every worker writes exactly one artifact under `.wf-artifacts/$RUN_ID/`.
4. Never skip the null state, even when a thesis looks strong.
5. If market quality is weak or risk validation fails, degrade to `draft` or `null`.
6. The final output must contain:
   - `signal_snapshot`
   - `selected_playbook`
   - `candidate_expressions`
   - `null_state`
   - `risk_checks`
   - `job`
   - `next_invalidation`
