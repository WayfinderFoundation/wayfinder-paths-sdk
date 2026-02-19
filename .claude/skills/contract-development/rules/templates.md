# Templates (Solidity + OZ v5)

All templates assume:

- Solidity: `pragma solidity ^0.8.26;`
- OpenZeppelin: `@openzeppelin/contracts@5.4.0`

## Use with MCP (recommended)

1. Save a template as a `.sol` file (usually under `$WAYFINDER_SCRATCH_DIR`).
2. Compile / deploy by passing `source_path` (not giant inline strings).

Compile:

```
mcp__wayfinder__compile_contract(source_path="$WAYFINDER_SCRATCH_DIR/Counter.sol", contract_name="Counter")
```

Deploy:

```
mcp__wayfinder__deploy_contract(wallet_label="main", source_path="$WAYFINDER_SCRATCH_DIR/Counter.sol", contract_name="Counter", chain_id=8453, constructor_args=[], verify=false, escape_hatch=true)
```

## Minimal Ownable counter

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import "@openzeppelin/contracts/access/Ownable.sol";

contract Counter is Ownable(msg.sender) {
    uint256 public count;

    function increment() external onlyOwner {
        count += 1;
    }
}
```

## ERC20 (mintable, Ownable)

`initialOwner` is explicit so you can deploy directly to a multisig.

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/access/Ownable.sol";

contract MyToken is ERC20, Ownable {
    constructor(
        address initialOwner,
        uint256 initialSupply
    ) ERC20("MyToken", "MTK") Ownable(initialOwner) {
        _mint(initialOwner, initialSupply);
    }

    function mint(address to, uint256 amount) external onlyOwner {
        _mint(to, amount);
    }
}
```

## Vault skeleton (nonReentrant + SafeERC20)

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import "@openzeppelin/contracts/access/Ownable.sol";
import "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";

contract TokenVault is Ownable(msg.sender), ReentrancyGuard {
    using SafeERC20 for IERC20;

    IERC20 public immutable token;

    constructor(IERC20 _token) {
        token = _token;
    }

    function deposit(uint256 amount) external nonReentrant {
        token.safeTransferFrom(msg.sender, address(this), amount);
    }

    function withdraw(uint256 amount) external onlyOwner nonReentrant {
        token.safeTransfer(msg.sender, amount);
    }
}
```

## Using `escape_hatch=true` instead of hand-writing rescue logic

If you deploy via the SDK’s `deploy_contract(..., escape_hatch=true)`, it injects:

- `Ownable(msg.sender)` inheritance (if missing)
- `IERC20` import (if missing)
- `escapeHatch(address token, uint256 amount)` guarded by `onlyOwner`

So you usually should **not** include a custom rescue function unless you need special behavior.
