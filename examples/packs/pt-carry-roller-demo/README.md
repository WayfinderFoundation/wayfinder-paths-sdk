# PT Carry Roller (Delta Lab Demo)

This demo Pack showcases a simple “roll into the best PT” carry strategy using
Delta Lab’s Pendle PT timeseries.

It ships with:

- A static applet (no backend code) that fetches live data from the local
  `vault-backend` Delta Lab proxy and renders:
  - the selected PT
  - the “why” (top candidate ladder)
  - a NAV backtest curve (stylized; no fees/slippage)
- A helper script to generate/inspect the same backtest logic from Python.

## Local dev

1) Start `vault-backend` on `http://127.0.0.1:8000`
2) Publish this pack:

```bash
export WAYFINDER_PACKS_API_URL=http://127.0.0.1:8000
poetry run wayfinder pack publish --path examples/packs/pt-carry-roller-demo --owner-wallet 0x000000000000000000000000000000000000dead
```

3) Open:

- `http://localhost:3003/packs`
- `http://localhost:3003/packs/pt-carry-roller-demo`

