# Installed Paths as jobs

Install first: `poetry run wayfinder path install <slug>` (records version and `bundle_sha256` in `.wayfinder/paths.lock.json`). Then `core_jobs(action="create_from_path", path_slug="<slug>", path_version=…, path_component=…, interval_seconds=…)` pins that version into a paused `path_v1` job.

## The pin

`job.yaml source` carries `{slug, version, component, component_kind, bundle_sha256, tree_sha256, install_dir, dry_run, params}`; a copy sits at `workspace/config/path.json` and params at `workspace/config/params.json`, so the job revision covers the pin. The Path runs from its install directory and is never copied or edited in place: it is published third-party code. Every tick re-hashes `bundle.zip` and the extracted tree; a mismatch refuses the tick (`path_pin_mismatch`) and records a failed run.

## What the manifest may declare

An optional `job:` block in `wfpath.yaml`:

```yaml
job:
  component: main          # which component runs
  kind: exec               # exec (subprocess) or freestyle (a tick(ctx) module)
  schedule: {interval_seconds: 900, timeout_seconds: 300}
  params: {…}              # defaults merged under create_from_path params
  risk: {max_daily_loss_usd: 25, max_drawdown: -0.1}   # written to workspace/risk_limits.json
  dry_run: supported       # the component honours WAYFINDER_PATH_DRY_RUN=1
```

- `kind: freestyle` components run through the freestyle runtime: paper-capable, full telemetry, the same validation dry run.
- `kind: exec` components run as a subprocess (like `wayfinder path exec`) with `WAYFINDER_JOB_MODE`, `WAYFINDER_PATH_DRY_RUN`, `WAYFINDER_PATH_PARAMS`, `WAYFINDER_PATH_STATE_DIR` in the environment. They report by printing `WAYFINDER_PATH_EVENT {"type": "order|fill|trade_close|tick", "payload": {…}}` lines, which land in the forward ledger. Without `dry_run: supported` the component is not executed by validation and has no paper mode: a paper launch records skipped runs, and live needs the `no_dry_run` flag acknowledged.

## Validation

`source_declared`, `install_lock_matches` (warn when the lock moved on), `bundle_sha256_verified`, `installed_tree_matches_pin`, `manifest_loads`, `component_exists`, `path_eval_passed` (fixture evals; missing `tests/evals` is a warning), then the dry run for the kind.

## Upgrades

`wayfinder path update <slug>` moves the lock; the job keeps its pin and records `state/path_upgrade.json` plus a `path_upgrade_available` journal row (`status.path_upgrade`). Moving is a new `create_from_path` on the new version; the pin never moves on its own.
