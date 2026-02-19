// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {IERC721} from "@openzeppelin/contracts/token/ERC721/IERC721.sol";
import {IERC721Receiver} from "@openzeppelin/contracts/token/ERC721/IERC721Receiver.sol";

/// @notice Minimal interface for Uniswap v4 PositionManager's liquidity actions.
interface IUniswapV4PositionManager {
    function modifyLiquidities(bytes calldata unlockData, uint256 deadline) external payable;
}

/// @notice Holds Uniswap v4 position NFTs, time-locks them, collects fees, and splits revenue.
/// @dev Designed for "agent seeds A/V with small V; cannot rug; fees feed FeeVault -> buy PROMPT".
contract LiquidityLockerV4 is Ownable, IERC721Receiver {
    using SafeERC20 for IERC20;

    // Uniswap v4-periphery Actions constants (v4-periphery/src/libraries/Actions.sol)
    uint8 internal constant ACTION_DECREASE_LIQUIDITY = 0x01;
    uint8 internal constant ACTION_BURN_POSITION = 0x03;
    uint8 internal constant ACTION_TAKE_PAIR = 0x11;

    IUniswapV4PositionManager public immutable posm;
    address public immutable feeVault;

    struct LockInfo {
        address beneficiary;
        address token0;
        address token1;
        uint64 unlockTime;
        uint16 feeShareBps;
        uint16 earlyExitBps;
        bool initialized;
    }

    mapping(uint256 => LockInfo) public locks;

    event LockRegistered(uint256 indexed tokenId, address indexed beneficiary, uint64 unlockTime);
    event FeesCollected(uint256 indexed tokenId, uint256 amount0, uint256 amount1);
    event PositionExited(uint256 indexed tokenId, bool early, uint256 out0, uint256 out1);

    error LockNotInitialized();
    error NotBeneficiary();
    error NativeNotSupported();
    error InvalidBps();
    error PositionNotOwnedByLocker(uint256 tokenId);

    constructor(address initialOwner, IUniswapV4PositionManager posm_, address feeVault_)
        Ownable(initialOwner)
    {
        posm = posm_;
        feeVault = feeVault_;
    }

    function registerLock(
        uint256 tokenId,
        address token0,
        address token1,
        address beneficiary,
        uint64 unlockTime,
        uint16 feeShareBps,
        uint16 earlyExitBps
    ) external onlyOwner {
        if (token0 == address(0) || token1 == address(0)) revert NativeNotSupported();
        if (beneficiary == address(0)) revert NotBeneficiary();
        if (feeShareBps > 10_000 || earlyExitBps > 10_000) revert InvalidBps();

        // Ensure the locker actually owns the position NFT.
        address ownerOf = IERC721(address(posm)).ownerOf(tokenId);
        if (ownerOf != address(this)) revert PositionNotOwnedByLocker(tokenId);

        locks[tokenId] = LockInfo({
            beneficiary: beneficiary,
            token0: token0,
            token1: token1,
            unlockTime: unlockTime,
            feeShareBps: feeShareBps,
            earlyExitBps: earlyExitBps,
            initialized: true
        });

        emit LockRegistered(tokenId, beneficiary, unlockTime);
    }

    function collectFees(uint256 tokenId) public {
        LockInfo memory li = locks[tokenId];
        if (!li.initialized) revert LockNotInitialized();

        uint256 b0Before = IERC20(li.token0).balanceOf(address(this));
        uint256 b1Before = IERC20(li.token1).balanceOf(address(this));

        bytes memory actions = abi.encodePacked(uint8(ACTION_DECREASE_LIQUIDITY), uint8(ACTION_TAKE_PAIR));
        bytes[] memory params = new bytes[](2);

        // Collect fees: liquidity=0, mins=0.
        params[0] = abi.encode(tokenId, uint128(0), uint128(0), uint128(0), bytes(""));
        // Currency ABI is `address` for ERC20 tokens.
        params[1] = abi.encode(li.token0, li.token1, address(this));

        posm.modifyLiquidities(abi.encode(actions, params), block.timestamp + 600);

        uint256 b0After = IERC20(li.token0).balanceOf(address(this));
        uint256 b1After = IERC20(li.token1).balanceOf(address(this));

        uint256 got0 = b0After - b0Before;
        uint256 got1 = b1After - b1Before;

        _split(li.token0, got0, li.beneficiary, li.feeShareBps);
        _split(li.token1, got1, li.beneficiary, li.feeShareBps);

        emit FeesCollected(tokenId, got0, got1);
    }

    function exitPosition(uint256 tokenId) external {
        LockInfo memory li = locks[tokenId];
        if (!li.initialized) revert LockNotInitialized();
        if (msg.sender != li.beneficiary) revert NotBeneficiary();

        // Harvest any pending fees before exiting.
        collectFees(tokenId);

        bool early = block.timestamp < li.unlockTime;

        uint256 b0Before = IERC20(li.token0).balanceOf(address(this));
        uint256 b1Before = IERC20(li.token1).balanceOf(address(this));

        bytes memory actions = abi.encodePacked(uint8(ACTION_BURN_POSITION), uint8(ACTION_TAKE_PAIR));
        bytes[] memory params = new bytes[](2);

        // Burn the position (mins=0 for simplicity).
        params[0] = abi.encode(tokenId, uint128(0), uint128(0), bytes(""));
        params[1] = abi.encode(li.token0, li.token1, address(this));

        posm.modifyLiquidities(abi.encode(actions, params), block.timestamp + 600);

        uint256 b0After = IERC20(li.token0).balanceOf(address(this));
        uint256 b1After = IERC20(li.token1).balanceOf(address(this));

        uint256 out0 = b0After - b0Before;
        uint256 out1 = b1After - b1Before;

        if (early && li.earlyExitBps > 0) {
            uint256 pen0 = (out0 * li.earlyExitBps) / 10_000;
            uint256 pen1 = (out1 * li.earlyExitBps) / 10_000;

            if (pen0 > 0) IERC20(li.token0).safeTransfer(feeVault, pen0);
            if (pen1 > 0) IERC20(li.token1).safeTransfer(feeVault, pen1);

            out0 -= pen0;
            out1 -= pen1;
        }

        if (out0 > 0) IERC20(li.token0).safeTransfer(li.beneficiary, out0);
        if (out1 > 0) IERC20(li.token1).safeTransfer(li.beneficiary, out1);

        delete locks[tokenId];

        emit PositionExited(tokenId, early, out0, out1);
    }

    function _split(address token, uint256 amount, address beneficiary, uint16 feeShareBps) internal {
        if (amount == 0) return;

        uint256 toBeneficiary = (amount * feeShareBps) / 10_000;
        uint256 toVault = amount - toBeneficiary;

        if (toBeneficiary > 0) IERC20(token).safeTransfer(beneficiary, toBeneficiary);
        if (toVault > 0) IERC20(token).safeTransfer(feeVault, toVault);
    }

    function onERC721Received(address, address, uint256, bytes calldata) external pure override returns (bytes4) {
        return IERC721Receiver.onERC721Received.selector;
    }
}

