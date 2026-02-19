// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/// @notice Simple fee accumulator / treasury vault for ERC20 + native ETH.
/// @dev Intended as the recipient of protocol fees (e.g., pool fees / hook fees).
contract FeeVault is Ownable {
    constructor(address initialOwner) Ownable(initialOwner) {}

    receive() external payable {}

    function sweepERC20(address token, address to, uint256 amount) external onlyOwner {
        IERC20(token).transfer(to, amount);
    }

    function sweepNative(address payable to, uint256 amount) external onlyOwner {
        to.transfer(amount);
    }
}

