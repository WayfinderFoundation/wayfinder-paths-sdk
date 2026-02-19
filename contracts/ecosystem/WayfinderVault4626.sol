// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC4626} from "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";

/// @notice ERC-4626 vault whose shares act as the ecosystem "V" token.
/// @dev This is a minimal vault (no strategy). Users can deposit/withdraw the underlying.
contract WayfinderVault4626 is ERC4626, Ownable {
    constructor(
        IERC20 asset_,
        string memory name_,
        string memory symbol_,
        address initialOwner
    ) ERC20(name_, symbol_) ERC4626(asset_) Ownable(initialOwner) {}
}

