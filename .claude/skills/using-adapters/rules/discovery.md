# Adapter discovery

## MCP resources (quickest)

- List adapters: `wayfinder://adapters`
- Describe one adapter (capabilities + entrypoint): `wayfinder://adapters/{name}`

## Repo sources of truth

- Adapter code: `wayfinder_paths/adapters/<adapter_name>/adapter.py`
- Adapter manifest: `wayfinder_paths/adapters/<adapter_name>/manifest.yaml`

Rules:
- Don’t guess method names, argument names, or return shapes — read the adapter file or load the matching protocol skill.

