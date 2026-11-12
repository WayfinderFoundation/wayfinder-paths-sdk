# Security notes (MVP)

This MVP intentionally favors “works end-to-end” over a full sandboxed hosting pipeline.

Current safe defaults for path creators:
- Never put secrets, private keys or seed phrases in a path.
- Prefer read-only “monitor/dashboard” paths first; add wallet flows only with clear UX + allowlists.

Applets are retired; don't add one (see [applet.md](applet.md)).
