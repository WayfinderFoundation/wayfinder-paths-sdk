# Boros Carry Demo Path

This is a demo **Wayfinder Path** that ships a static applet and a small script to generate a Delta Lab-shaped dataset for **Boros** carry (fixed vs floating) versus underlying price.

## Quickstart (local demo)

```bash
# (optional) regenerate the applet dataset
python scripts/boros_backtest.py --mode demo --out applet/dist/data/boros_demo.json

# build bundle.zip
poetry run wayfinder path build --path . --out dist/bundle.zip

# publish to local vault-backend (SERVICE_MODE=test allows anon publish)
export WAYFINDER_PATHS_API_URL=http://127.0.0.1:8000
poetry run wayfinder path publish --path . --out dist/bundle.zip

# emit a signal so the path page feels alive
poetry run wayfinder path signal emit --slug boros-carry-demo --title "Backtest updated" --message "Regenerated demo dataset"
```

## Real Delta Lab data (optional)

Set:

- `WAYFINDER_API_KEY=wk_...`
- `system.api_base_url=https://strategies.wayfinder.ai/api/v1` (in `config.json`, or via your SDK setup)

Then run:

```bash
python scripts/boros_backtest.py --mode delta-lab --symbol ETH --lookback-days 90
```

