// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

import {AgentToken} from "./AgentToken.sol";

/// @notice Owner-gated factory for creating per-agent ERC-20 tokens (A).
contract AgentTokenFactory is Ownable {
    event AgentTokenCreated(
        address indexed token,
        address indexed tokenOwner,
        address indexed initialRecipient,
        string name,
        string symbol,
        uint256 initialSupply
    );

    constructor(address initialOwner) Ownable(initialOwner) {}

    function createAgentToken(
        string memory name_,
        string memory symbol_,
        address tokenOwner,
        uint256 initialSupply,
        address initialRecipient
    ) external onlyOwner returns (address token) {
        AgentToken t = new AgentToken(
            name_,
            symbol_,
            tokenOwner,
            initialSupply,
            initialRecipient
        );
        token = address(t);
        emit AgentTokenCreated(
            token,
            tokenOwner,
            initialRecipient,
            name_,
            symbol_,
            initialSupply
        );
    }
}

