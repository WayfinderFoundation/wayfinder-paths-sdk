# RewardParticipationAdapter

Read-only program observations for objective strategy jobs. `observe()`
returns `(ok, observation_or_error)`; `close()` releases its HTTP client.
See `examples.json` for configuration and the Path README for readiness blockers.

RISEx uses a caller-supplied, account-scoped JWT and verifies chain 4153 and the
returned wallet. IMD public reads verify seat ownership and earnings wallet.
FLOP and PERPTools return explicit blockers without invented network calls.
No registration, signing, deposits, orders, inference submissions or claims.
The protection hook does not manage live exposure: this adapter never opens any.
