// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

/// @notice Optional rate-limited funding escrow to reduce "agent runs off with funds" risk.
/// @dev Depositors can withdraw their unspent deposits at any time; the agent can only draw
///      at a configured rate (tokens per second).
contract AgentFundingEscrow {
    IERC20 public immutable V;
    address public immutable agent;

    uint256 public drawRatePerSecond;
    uint256 public lastDrawTs;
    uint256 public accruedDraw;

    mapping(address => uint256) public depositorBalance;
    uint256 public totalDeposits;

    error NotAgent();

    constructor(IERC20 vToken, address agent_, uint256 drawRatePerSecond_) {
        V = vToken;
        agent = agent_;
        drawRatePerSecond = drawRatePerSecond_;
        lastDrawTs = block.timestamp;
    }

    function deposit(uint256 amount) external {
        V.transferFrom(msg.sender, address(this), amount);
        depositorBalance[msg.sender] += amount;
        totalDeposits += amount;
    }

    function withdraw(uint256 amount) external {
        depositorBalance[msg.sender] -= amount;
        totalDeposits -= amount;
        V.transfer(msg.sender, amount);
    }

    function _accrue() internal {
        uint256 dt = block.timestamp - lastDrawTs;
        accruedDraw += dt * drawRatePerSecond;
        lastDrawTs = block.timestamp;

        uint256 bal = V.balanceOf(address(this));
        if (accruedDraw > bal) accruedDraw = bal;
    }

    function draw(address to, uint256 amount) external {
        if (msg.sender != agent) revert NotAgent();
        _accrue();
        require(amount <= accruedDraw, "RATE_LIMIT");
        accruedDraw -= amount;
        V.transfer(to, amount);
    }
}

