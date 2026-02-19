# Compilation

## How it works

Solidity compilation uses `py-solc-x` with **standard JSON input** — no manual flattening.

- Compiler version: **solc 0.8.26** (auto-installed on first use via `solcx.install_solc()`)
- OpenZeppelin version: **@openzeppelin/contracts 5.4.0** (pinned for deterministic compilation + verification)
- OpenZeppelin imports: resolved by loading the required OZ sources into the compiler input
- OZ is auto-installed into an ignored cache dir via `npm install --prefix .cache/solidity/openzeppelin-5.4.0 @openzeppelin/contracts@5.4.0` if missing

## MCP tool: `compile_contract`

```
mcp__wayfinder__compile_contract(
    source_path="$WAYFINDER_SCRATCH_DIR/MyToken.sol",
    contract_name="MyToken"
)
```

- `source_path` — path to a `.sol` file (relative to repo root, or an absolute path inside the repo)
- `contract_name` — (optional) validate this contract exists in output
- Returns: `{contracts: {Name: {abi, bytecode, abi_summary}}}`
- This is a **read-only** tool (no confirmation needed)

## Python utility: `compile_solidity()`

```python
from wayfinder_paths.core.utils.solidity import compile_solidity

artifacts = compile_solidity(
    source_code,
    contract_name="MyToken",  # optional: validates presence
    optimize=True,
    optimize_runs=200,
)
# artifacts = {"MyToken": {"abi": [...], "bytecode": "0x..."}}
```

## Standard JSON (for Etherscan verification)

```python
from wayfinder_paths.core.utils.solidity import compile_solidity_standard_json

result = compile_solidity_standard_json(source_code)
# result["input"]  — the standard JSON input dict (submit to Etherscan)
# result["output"] — the compiler output
```

## Supported imports

OpenZeppelin imports work out of the box:

```solidity
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
```

The compiler also supports local imports from within this repo:

- Relative imports: `import "./Foo.sol";`, `import "../Foo.sol";` (resolved relative to the importing file)
- Repo-relative imports: `import "contracts/Foo.sol";`

Other npm packages (e.g. `solmate/...`) are not auto-installed — those imports will fail unless the sources are checked into this repo.
