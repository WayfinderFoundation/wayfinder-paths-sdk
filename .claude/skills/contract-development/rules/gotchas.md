# Gotchas

## 1. npm/Node.js is required for OpenZeppelin imports

`compile_solidity()` supports `@openzeppelin/...` imports by auto-installing **@openzeppelin/contracts@5.4.0** into an ignored cache directory (`.cache/solidity/openzeppelin-5.4.0/node_modules/`) on first use. This requires `npm` and `node` on the PATH, but does **not** create a repo-root `node_modules/`.

## 2. Local imports are supported, but only from within this repo

The compiler supports:

- `@openzeppelin/*` imports (auto-installed into `.cache/solidity/...` on first use)
- Relative imports (`./Foo.sol`, `../Foo.sol`) between Solidity files checked into this repo
- Repo-relative imports (`contracts/Foo.sol`)

Other npm packages are not auto-installed, so imports like `import "solmate/tokens/ERC20.sol";` will fail unless you vendor the sources into this repository.

## 3. solcx installs the solc binary on first use

The first call to `compile_solidity()` downloads the `solc` 0.8.26 binary via `solcx.install_solc()`. This is a one-time ~10MB download. Subsequent compilations use the cached binary.

## 4. Only solc 0.8.26 is supported

All compilation uses solc 0.8.26. If your source requires a different pragma, update the `SOLC_VERSION` constant in `wayfinder_paths/core/utils/solidity.py` and the compiler version string in `verify_on_etherscan()`.

## 5. Constructor args are auto-cast via `abi_caster`

When passing constructor args, they're automatically cast to match the ABI types. This means:
- Strings like `"1000"` become `int(1000)` for `uint256`
- Hex strings like `"0xabc..."` become checksummed addresses for `address`
- `"true"` / `"false"` strings become Python `bool` for Solidity `bool`

If you're passing args via the MCP tool, pass a JSON array (preferred): `["0xaddr", 1000, true]` (a JSON array string also works: `'["0xaddr", 1000, true]'`).

## 6. MCP contract tools take a file path (`source_path`)

Use `source_path` to point at a `.sol` file inside this repo (committed or under `$WAYFINDER_SCRATCH_DIR`). Avoid passing giant `source_code` strings.

## 7. Escape hatch adds Ownable dependency

When `escape_hatch=True`, the source is modified to inherit `Ownable(msg.sender)` (deployer becomes owner). If your contract already uses a custom ownership pattern, this may conflict. Disable with `escape_hatch=False`.

## 8. Ownable v5 requires an initial owner argument

If you inherit `Ownable` directly in your contract (not via `escape_hatch` injection), you must pass the base constructor arg:

- `contract X is Ownable(msg.sender) { ... }`, or
- `constructor(address initialOwner) Ownable(initialOwner) { ... }`

## 9. Etherscan API key is optional (verification only)

Deployments work without an Etherscan API key. The key is only needed when `verify=true` and you want the source auto-verified on the explorer.

- If you don’t have a key, set `verify=false` (or ignore `verified=false` + `verification_error` in the deploy result).
- To enable verification: set `config.json` → `system.etherscan_api_key` or `ETHERSCAN_API_KEY`. Get a free key at etherscan.io.

## 10. Etherscan V2 `constructorArguements` has a typo

The Etherscan API parameter is intentionally misspelled as `constructorArguements` (not `Arguments`). The SDK handles this internally.

## 11. Fork deploys should skip verification

When deploying to a Gorlami fork, always set `verify=False`. Etherscan can't verify contracts on forked networks.

## 12. The compile MCP tool is read-only, deploy is fund-moving

- `compile_contract` — auto-allowed (no confirmation)
- `deploy_contract` — gated by safety review hook (shows wallet, chain, contract name)
