// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @notice Per-agent ERC-20 token (A).
contract AgentToken is ERC20, Ownable {
    constructor(
        string memory name_,
        string memory symbol_,
        address initialOwner,
        uint256 initialSupply,
        address initialRecipient
    ) ERC20(name_, symbol_) Ownable(initialOwner) {
        if (initialSupply > 0) {
            _mint(initialRecipient, initialSupply);
        }
    }

    function mint(address to, uint256 amount) external onlyOwner {
        _mint(to, amount);
    }
}

