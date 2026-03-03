# `wfpack.yaml` (Pack Manifest)

The Pack manifest is the source-of-truth for publishing a Pack bundle.

For MVP, the backend requires **at minimum**:

- `slug` (URL-safe)
- `name`
- `version` (semver-like string)

Recommended fields:

- `schema_version`
- `summary`
- `primary_kind` (e.g. `monitor`, `strategy`, `contract`, `bundle`)
- `tags` (list)

## Minimal example

```yaml
schema_version: "0.1"

slug: "basis-board"
name: "Basis Board"
version: "0.1.0"
summary: "Monitor basis spreads and publish a live signal feed."

primary_kind: "monitor"
tags:
  - hyperliquid
  - perps
  - monitor
```

## With an applet

If you include an applet UI, add an `applet` block:

```yaml
applet:
  build_dir: "applet/dist"
  manifest: "applet/applet.manifest.json"
```

Notes:
- Prefer `applet/dist` (not a top-level `dist/`) so the pack bundler doesn’t accidentally exclude your UI build output.
- `build_dir` must contain the `entry` HTML file declared by `applet.manifest.json` (default `index.html`).

## Optional: components

You can include additional metadata for your own tooling (the backend stores the full YAML as JSON):

```yaml
components:
  - id: "main-strategy"
    kind: "strategy"
    path: "strategies/my_strategy"
```

MVP behavior:
- The backend does not strictly validate `components` yet, but it will be persisted in `manifest`.

