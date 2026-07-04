<!-- wayfinder-path:override-demo:claude-rules:start -->
# Override Demo Claude Rules

This generated section orchestrates the `pipeline` workflow.

Rules:
- The main-thread skill owns orchestration.
- Worker agents are leaf-only and write one artifact each.
- Runtime artifacts live under `.wf-artifacts`.
- Null-state evaluation is mandatory before any job is armed.
- If risk checks fail, downgrade to draft or null-state.
<!-- wayfinder-path:override-demo:claude-rules:end -->
