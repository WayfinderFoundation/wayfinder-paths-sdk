# Manifests and tests (non-negotiables)

## Manifests are the source of truth

Each adapter/strategy directory has a `manifest.yaml` that must stay correct:
- Adapters: `entrypoint`, `capabilities`, `dependencies`
- Strategies: `entrypoint`, `name`, `permissions.policy`, `adapters`

Validation:
- Run `just validate-manifests` early and often.

## Strategy testing contract

For strategies in `wayfinder_paths/strategies/<strategy>/`:
- Maintain `examples.json` and load test inputs from it (never hardcode example values in tests).
- Provide smoke coverage for the lifecycle: `deposit → update → status → withdraw`.
- Optional read-only methods: `quote()`, `analyze()`, `build_batch_snapshot()` - implement these to support APY queries and batch scoring.

## Adapter testing contract

For adapters in `wayfinder_paths/adapters/<adapter>/`:
- Cover key read paths with mocked clients.
- If there are execution methods, ensure tests mock the underlying clients.

