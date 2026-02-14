# Configuration Guide

This guide explains how to configure Wayfinder Paths for local development and testing.

## Quick Setup

```bash
# One-command setup (installs Poetry + deps, prompts for your Wayfinder API key, updates .mcp.json)
python3 scripts/setup.py

# Or: deterministic wallets from a generated mnemonic (saved to config.json)
python3 scripts/setup.py --mnemonic

# Remote two-stage setup (stage 1 installs deps + writes config.json)
python3 scripts/remote_setup_stage1.py --api-key wk_...
# Stage 2 option A (recommended): generate + persist a mnemonic (prints once)
python3 scripts/remote_setup_stage2.py --mnemonic
# Stage 2 option B: load mnemonic from file (avoids shell history)
python3 scripts/remote_setup_stage2.py --mnemonic-file /path/to/mnemonic.txt

# Run a strategy
poetry run python -m wayfinder_paths.run_strategy stablecoin_yield_strategy --action status --config config.json
```

## Configuration File Structure

The `config.json` file has three main sections:

```json
{
  "system": {
    "api_base_url": "https://api.wayfinder.ai",
    "api_key": "sk_live_..."
  },
  "strategy": {
    "rpc_urls": {
      "1": "https://eth.llamarpc.com",
      "8453": "https://mainnet.base.org",
      "42161": "https://arb1.arbitrum.io/rpc"
    }
  },
  "wallet_mnemonic": "abandon ...",
  "wallets": [
    {
      "label": "main",
      "address": "0x...",
      "private_key_hex": "0x..."
    },
    {
      "label": "stablecoin_yield_strategy",
      "address": "0x...",
      "private_key_hex": "0x..."
    }
  ]
}
```

## System Configuration

| Field | Required | Description |
|-------|----------|-------------|
| `api_key` | Yes | Wayfinder API key (sent as `X-API-KEY` header) |
| `api_base_url` | No | API endpoint (default: `https://wayfinder.ai/api`) |

The API key is automatically loaded and included in all API requests (including Gorlami dry-runs). You don't need to pass it explicitly to strategies or clients.

## Strategy Configuration

The `strategy` section contains strategy-specific settings:

| Field | Description |
|-------|-------------|
| `rpc_urls` | Map of chain IDs to RPC endpoints |

### RPC URLs

This repo ships with example RPC endpoints in `config.example.json` for common chains (Ethereum/Base/Arbitrum/etc). These are primarily public endpoints and may rate limit under load.

Override them in your `config.json` with your own provider(s) if needed:

```json
{
  "strategy": {
    "rpc_urls": {
      "1": "https://your-ethereum-rpc.com",
      "8453": "https://your-base-rpc.com"
    }
  }
}
```

Notes:

- If `strategy.rpc_urls` is not set for a chain, `web3_from_chain_id(...)` defaults to the Wayfinder proxy RPC at `${system.api_base_url}/blockchain/rpc/<chain_id>/` (requires `api_key`).
- If you provide a list, `web3_from_chain_id(...)` uses the first entry for reads; put your best RPC first.
- If a script appears to be using a public RPC, print `resolve_config_path()` and `get_rpc_urls()` to confirm which config file was loaded.

## Wallet Configuration

Optional: you can add a `wallet_mnemonic` (BIP-39) to deterministically derive wallets using MetaMask's default derivation path (`m/44'/60'/0'/0/N`) when generating local dev wallets.
Newly generated mnemonics are 12 words by default.

Wallets are stored in the `wallets` array. Each wallet has:

| Field | Description |
|-------|-------------|
| `label` | Wallet identifier (e.g., `"main"`, `"stablecoin_yield_strategy"`) |
| `address` | Ethereum address |
| `private_key_hex` | Private key (hex format with `0x` prefix) |

### Wallet Lookup

The system automatically matches wallets to strategies by label:

1. **Main wallet**: Wallet with `label: "main"`
2. **Strategy wallet**: Wallet with label matching the strategy directory name

For example, when running `stablecoin_yield_strategy`, the system looks for:
- Main wallet: `wallets[].label == "main"`
- Strategy wallet: `wallets[].label == "stablecoin_yield_strategy"`

### Creating Wallets

```bash
# Create main wallet
just create-wallets
# Or: poetry run python scripts/make_wallets.py -n 1

# Create deterministic wallets from a generated mnemonic (saved to config.json)
poetry run python scripts/make_wallets.py -n 1 --mnemonic

# Create strategy-specific wallet
just create-wallet stablecoin_yield_strategy
# Or: poetry run python scripts/make_wallets.py --label stablecoin_yield_strategy

# Create multiple wallets
poetry run python scripts/make_wallets.py -n 3

# Create keystore files (geth/web3 compatible)
poetry run python scripts/make_wallets.py -n 1 --keystore-password "your-password"
```

## Accessing Configuration in Strategies

Strategies receive configuration automatically through the `config` attribute:

```python
class MyStrategy(Strategy):
    async def deposit(self, **kwargs):
        # Access wallet addresses
        main_wallet = self.config.get("main_wallet", {})
        main_address = main_wallet.get("address")

        strategy_wallet = self.config.get("strategy_wallet", {})
        strategy_address = strategy_wallet.get("address")

        # Access RPC URLs
        rpc_urls = self.config.get("rpc_urls", {})
        base_rpc = rpc_urls.get("8453")

        # Access strategy-specific config
        custom_param = self.config.get("my_custom_param", "default")
```

## Environment-Specific Configuration

For different environments, create separate config files:

```bash
# Development
poetry run python -m wayfinder_paths.run_strategy my_strategy --config config.dev.json

# Production
poetry run python -m wayfinder_paths.run_strategy my_strategy --config config.prod.json
```

## Security Best Practices

1. **Never commit `config.json`** - Add it to `.gitignore`
2. **Use test wallets** - Generated wallets are for testing only
3. **Rotate API keys** - Change keys if compromised
4. **Protect private keys** - Never share or expose them

## Troubleshooting

### "Authentication failed"
- Verify `system.api_key` is set correctly
- Check that the API key has proper permissions
- Ensure the key hasn't expired

### "Wallet not found"
- Run `just create-wallets` to generate wallets
- Check that `config.json` exists in the repository root
- Verify wallet labels match strategy directory names

### "Invalid config"
- Ensure `config.json` is valid JSON
- Check that all required fields are present
- Verify the file structure matches the examples above

### "RPC error"
- Check RPC URL is correct and accessible
- Verify the chain ID matches the RPC endpoint
- Try a different RPC provider if rate limited
