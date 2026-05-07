---
name: using-shells-notify
description: How to email the Wayfinder Shells instance owner from agents/scripts via the notify MCP tool or NotifyClient (Markdown body rendered to themed HTML, throttled).
metadata:
  tags: wayfinder, shells, notify, email, opencode
---

## TL;DR

Email the user who owns this Wayfinder Shells instance. Markdown in, themed HTML email out.

**MCP tool (preferred from agents):**

```
shells_notify(
  title="Rebalance complete",
  message="Moved **50 USDC** from Aave → Morpho.\n\n- tx: 0x…\n- new APY: 7.4%",
)
```

**Python client (from scripts):**

```python
from wayfinder_paths.core.clients.NotifyClient import NOTIFY_CLIENT

await NOTIFY_CLIENT.notify(
    title="Rebalance complete",
    message="Moved **50 USDC** from Aave → Morpho.\n\n- tx: 0x…\n- new APY: 7.4%",
)
```

Both POST to `{api_base}/opencode/notify/` with the configured `WAYFINDER_API_KEY`.

## Limits & gotchas

- **Title:** ≤ 200 chars (required, non-empty after strip).
- **Message:** ≤ 20 000 chars (required). Rendered as Markdown — headings, lists, tables, fenced code, links all work.
- **Delivery gate:** Only sent if the owner has `email_verified: true` on vault-backend. No verified email = silently dropped.
- **Throttle:** Backend caps at **4 emails / user / day**. Budget your sends; don't spam progress updates.
- **Shells-only:** No-op (or HTTP error) outside a Wayfinder Shells instance. The MCP tool gates on `is_opencode_instance()` indirectly via the API; the client just hits the URL. Detection: `OPENCODE_INSTANCE_ID` env var is set, or the health probe at `http://localhost:4096/global/health` returns `healthy: true`.
- Client returns the parsed JSON dict directly (no `(ok, data)` tuple — it's a `WayfinderClient`, not an adapter).

## When to use

- Report completed fund-moving work to the owner ("rebalance done", "withdraw confirmed").
- Surface decisions that need them ("APY dropped below threshold — pause strategy?").
- Flag failures you can't auto-resolve.
- Escalate `job_result` events from scheduled jobs that need owner attention.

Don't use for chatty progress updates — the daily quota is small.

## Markdown formatting tips

- Use `**bold**` for amounts and statuses.
- Code-fence tx hashes / addresses so they don't wrap mid-string.
- Tables work for multi-step rebalance summaries.
- Links: `[Block explorer](https://...)` → clickable in the rendered email.

## Error shape

MCP tool returns `{"ok": false, "error": {"code": ..., "message": ...}}` for:
- `invalid_request` — title/message empty or exceeds limit.
- `notify_http_error` — backend rejected (check `details` for body).
- `notify_error` — transport failure.
