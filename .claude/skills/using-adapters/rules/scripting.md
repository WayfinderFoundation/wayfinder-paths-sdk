# Scripting with adapters

## Where scripts live

- Put ad-hoc scripts in `.wayfinder_runs/` (ignored by git).

## `get_adapter()` patterns

Use `get_adapter()` to wire config + (optionally) a signing wallet:

```python
from wayfinder_paths.mcp.scripting import get_adapter
from wayfinder_paths.adapters.moonwell_adapter import MoonwellAdapter

# Read-only (no wallet needed)
adapter = get_adapter(MoonwellAdapter)

# Execution (wallet_address + sign_callback wired from config.json)
adapter_exec = get_adapter(MoonwellAdapter, wallet_label="main")
```

Notes:
- Many execution methods are decorated with `@require_wallet` and will return `ok=False` if no wallet is configured.

