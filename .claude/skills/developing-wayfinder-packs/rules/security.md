# Security notes (MVP)

This MVP intentionally favors “works end-to-end” over a full sandboxed hosting pipeline.

Current safe defaults for pack creators:
- Treat applets as untrusted: no secrets, no private keys, no seed phrases.
- Prefer read-only “monitor/dashboard” packs first; add wallet flows only with clear UX + allowlists.

Known limitations right now:
- Applets are served from the backend API path (not a dedicated isolated origin yet).
- There is no automated static scan / preview screenshot pipeline yet.

If you’re authoring an applet:
- Keep network access minimal and declare external origins in your applet manifest (future review hooks will rely on this).

