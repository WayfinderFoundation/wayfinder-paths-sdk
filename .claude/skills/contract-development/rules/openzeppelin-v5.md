# OpenZeppelin v5+ quick reference

This SDK pins and caches OpenZeppelin so compilation + verification are deterministic.

- Pinned version: `@openzeppelin/contracts@5.4.0`
- Cache path: `.cache/solidity/openzeppelin-5.4.0/node_modules/@openzeppelin/contracts/`

## Ownable (important)

In OpenZeppelin v5, `Ownable` requires an initial owner:

- `constructor(address initialOwner)`

So you **must** pass the base constructor arg when inheriting.

Valid patterns:

```solidity
import "@openzeppelin/contracts/access/Ownable.sol";

contract MyContract is Ownable(msg.sender) {
    // ...
}
```

or:

```solidity
import "@openzeppelin/contracts/access/Ownable.sol";

contract MyContract is Ownable {
    constructor(address initialOwner) Ownable(initialOwner) {}
}
```

The SDK’s `escape_hatch=true` injection uses `Ownable(msg.sender)` so deployer becomes owner.

## Common import path changes (v4 -> v5)

- `ReentrancyGuard`
  - v4: `@openzeppelin/contracts/security/ReentrancyGuard.sol`
  - v5: `@openzeppelin/contracts/utils/ReentrancyGuard.sol`
- `Pausable`
  - v4: `@openzeppelin/contracts/security/Pausable.sol`
  - v5: `@openzeppelin/contracts/utils/Pausable.sol`

Most other commonly used paths are unchanged:

```solidity
import "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import "@openzeppelin/contracts/access/AccessControl.sol";
```

## How to inspect OZ sources locally

If you need authoritative context (e.g. constructor signatures, modifiers), read the cached sources:

- `Ownable`: `.cache/solidity/openzeppelin-5.4.0/node_modules/@openzeppelin/contracts/access/Ownable.sol`

