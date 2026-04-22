# Signals

Signals are structured events emitted by paths/runtimes so path pages feel alive.

## Emit a signal

```bash
poetry run wayfinder path signal emit \
  --slug basis-board \
  --version 0.1.0 \
  --title "Funding spread widened" \
  --message "ETH basis crossed threshold" \
  --metric spread_bps=18.2
```

Tips:
- Use `--metric key=value` multiple times to attach numeric metrics.
- Keep titles short; the web UI will render the message + timestamp as a feed.
