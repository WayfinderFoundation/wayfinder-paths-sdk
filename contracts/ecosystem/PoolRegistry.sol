// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @notice Minimal onchain registry for canonical ecosystem pools and per-agent markets.
/// @dev Stores Uniswap v4-style pool keys + ids (pools are identified by PoolId, not a pool address).
contract PoolRegistry is Ownable {
    address public immutable promptToken; // P
    address public immutable vaultShare; // V (ERC4626 share token)
    address public immutable underlying; // U (e.g. USDC)

    struct PoolKey {
        address currency0;
        address currency1;
        uint24 fee;
        int24 tickSpacing;
        address hooks;
    }

    // Canonical ecosystem pools
    PoolKey public pvKey; // P/V
    bytes32 public pvId;
    PoolKey public vuKey; // V/U
    bytes32 public vuId;

    // Agent token => A/V pool key/id
    mapping(address => PoolKey) public avKey;
    mapping(address => bytes32) public avId;

    event BasePoolsSet(bytes32 pvId, bytes32 vuId);
    event AgentPoolRegistered(address indexed agentToken, bytes32 avId);

    constructor(
        address initialOwner,
        address promptToken_,
        address vaultShare_,
        address underlying_
    ) Ownable(initialOwner) {
        promptToken = promptToken_;
        vaultShare = vaultShare_;
        underlying = underlying_;
    }

    function poolId(PoolKey memory key) public pure returns (bytes32) {
        return keccak256(
            abi.encode(key.currency0, key.currency1, key.fee, key.tickSpacing, key.hooks)
        );
    }

    function setBasePools(PoolKey calldata pvKey_, PoolKey calldata vuKey_) external onlyOwner {
        require(uint160(pvKey_.currency0) < uint160(pvKey_.currency1), "UNSORTED");
        require(uint160(vuKey_.currency0) < uint160(vuKey_.currency1), "UNSORTED");

        pvKey = pvKey_;
        vuKey = vuKey_;
        pvId = poolId(pvKey_);
        vuId = poolId(vuKey_);
        emit BasePoolsSet(pvId, vuId);
    }

    function registerAgentPool(address agentToken, PoolKey calldata avKey_) external onlyOwner {
        require(uint160(avKey_.currency0) < uint160(avKey_.currency1), "UNSORTED");
        avKey[agentToken] = avKey_;
        bytes32 id = poolId(avKey_);
        avId[agentToken] = id;
        emit AgentPoolRegistered(agentToken, id);
    }
}
