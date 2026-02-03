# API Key Configuration

## Getting an API Key

1. Go to https://strategies.wayfinder.ai
2. Create an account or sign in
3. Navigate to Settings > API Keys
4. Generate a new key (starts with `wk_`)

## Setting the API Key

The API key can be set in multiple ways (in order of precedence):

1. **config.json** (recommended):
   ```json
   {
     "system": {
       "api_key": "wk_your_key_here"
     }
   }
   ```

2. **Environment variable**:
   ```bash
   export WAYFINDER_API_KEY=wk_your_key_here
   ```

3. **During setup**:
   ```bash
   python3 scripts/setup.py --api-key wk_your_key_here
   ```

## Verifying Configuration

After setting your API key, you can verify it works by running:
```bash
poetry run python -c "from wayfinder_paths.core.clients.WayfinderClient import WayfinderClient; print('API key configured!')"
```

## Common Issues

### "api_key not set" error
- Check that `config.json` exists and has `system.api_key` set
- Verify the key format (should start with `wk_`)
- Re-run setup: `python3 scripts/setup.py`

### Key not working
- Verify the key is active at https://strategies.wayfinder.ai
- Check for typos (copy-paste the key directly)
- Ensure no extra whitespace around the key value
