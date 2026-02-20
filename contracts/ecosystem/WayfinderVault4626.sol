// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {ERC4626} from "@openzeppelin/contracts/token/ERC20/extensions/ERC4626.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @notice ERC-4626 vault whose shares act as the ecosystem "V" token.
/// @dev Supports async queued withdrawals + cross-chain NAV accounting for heterogeneous assets.
contract WayfinderVault4626 is ERC4626, Ownable, Pausable, ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ---------- Asset types (valuation haircuts) ----------

    struct AssetTypeConfig {
        bool enabled;
        uint16 haircutBps; // 0..10000 (e.g., 2000 = 20% haircut)
        string name;
    }

    mapping(bytes32 => AssetTypeConfig) public assetTypeConfig;

    event AssetTypeSet(bytes32 indexed typeId, bool enabled, uint16 haircutBps, string name);

    error AssetTypeDisabled(bytes32 typeId);
    error BadHaircut(uint16 haircutBps);

    function setAssetType(bytes32 typeId, bool enabled, uint16 haircutBps, string calldata name) external onlyOwner {
        if (haircutBps > 10_000) revert BadHaircut(haircutBps);
        assetTypeConfig[typeId] = AssetTypeConfig({enabled: enabled, haircutBps: haircutBps, name: name});
        emit AssetTypeSet(typeId, enabled, haircutBps, name);
    }

    function applyHaircut(bytes32 typeId, uint256 value) public view returns (uint256) {
        AssetTypeConfig memory c = assetTypeConfig[typeId];
        if (!c.enabled) revert AssetTypeDisabled(typeId);
        return (value * (10_000 - uint256(c.haircutBps))) / 10_000;
    }

    // ---------- Cross-chain accounting ----------

    struct ChainState {
        bool enabled;

        // Book value updated immediately on bridge out/in (prevents accounting gaps).
        uint256 expected;

        // Latest reported discounted NAV (after per-asset-type haircuts).
        uint256 reported;
        bool hasReport;

        uint64 lastReportAt;
        uint64 lastNonce;

        // Risk controls.
        uint64 maxStaleness; // seconds
        uint16 maxGainBps;   // cap upward delta vs expected

        // Value actually counted in totalAssets() (derived).
        uint256 accounted;
    }

    mapping(uint32 => ChainState) public chains;
    uint32[] public chainList;
    mapping(uint32 => bool) internal _chainKnown;

    uint256 public totalRemoteAccounted;

    address public messenger; // cross-chain messenger/router on this (hub) chain
    address public bridge;    // bridge router on this (hub) chain

    event ChainRegistered(uint32 indexed chainId, uint64 maxStaleness, uint16 maxGainBps);
    event ChainToggled(uint32 indexed chainId, bool enabled);
    event MessengerSet(address messenger);
    event BridgeSet(address bridge);

    event BridgedOut(uint32 indexed chainId, uint256 assets, uint256 newExpected);
    event BridgedIn(uint32 indexed chainId, uint256 assets, uint256 newExpected);
    event NavReported(uint32 indexed chainId, uint64 indexed nonce, uint256 discountedNav);

    error ChainDisabled(uint32 chainId);
    error BadNonce(uint64 nonce);
    error LengthMismatch();
    error Unauthorized();
    error ExpectedUnderflow(uint32 chainId, uint256 expected, uint256 assets);

    modifier onlyMessenger() {
        if (msg.sender != messenger && msg.sender != owner()) revert Unauthorized();
        _;
    }

    modifier onlyBridge() {
        if (msg.sender != bridge && msg.sender != owner()) revert Unauthorized();
        _;
    }

    function setMessenger(address messenger_) external onlyOwner {
        messenger = messenger_;
        emit MessengerSet(messenger_);
    }

    function setBridge(address bridge_) external onlyOwner {
        bridge = bridge_;
        emit BridgeSet(bridge_);
    }

    function registerChain(uint32 chainId, uint64 maxStaleness, uint16 maxGainBps) external onlyOwner {
        ChainState storage c = chains[chainId];
        c.enabled = true;
        c.maxStaleness = maxStaleness;
        c.maxGainBps = maxGainBps;
        if (!_chainKnown[chainId]) {
            _chainKnown[chainId] = true;
            chainList.push(chainId);
        }
        emit ChainRegistered(chainId, maxStaleness, maxGainBps);
    }

    function setChainEnabled(uint32 chainId, bool enabled) external onlyOwner {
        chains[chainId].enabled = enabled;
        emit ChainToggled(chainId, enabled);
    }

    function _computeAccounted(
        uint256 expected,
        uint256 reported,
        bool hasReport,
        uint16 maxGainBps
    ) internal pure returns (uint256) {
        if (!hasReport) return expected;
        uint256 cap = expected + (expected * uint256(maxGainBps)) / 10_000;
        return reported > cap ? cap : reported;
    }

    function _setAccounted(uint32 chainId, uint256 newAccounted) internal {
        ChainState storage c = chains[chainId];
        totalRemoteAccounted = totalRemoteAccounted - c.accounted + newAccounted;
        c.accounted = newAccounted;
    }

    /// @notice Book value increment for assets bridged from hub -> chain.
    function notifyBridgedOut(uint32 chainId, uint256 assets) external onlyBridge {
        ChainState storage c = chains[chainId];
        if (!c.enabled) revert ChainDisabled(chainId);

        c.expected += assets;
        uint256 newAcc = _computeAccounted(c.expected, c.reported, c.hasReport, c.maxGainBps);
        _setAccounted(chainId, newAcc);

        emit BridgedOut(chainId, assets, c.expected);
    }

    /// @notice Book value decrement for assets bridged from chain -> hub.
    function notifyBridgedIn(uint32 chainId, uint256 assets) external onlyBridge {
        ChainState storage c = chains[chainId];
        if (c.expected < assets) revert ExpectedUnderflow(chainId, c.expected, assets);

        c.expected -= assets;
        uint256 newAcc = _computeAccounted(c.expected, c.reported, c.hasReport, c.maxGainBps);
        _setAccounted(chainId, newAcc);

        emit BridgedIn(chainId, assets, c.expected);
    }

    /// @notice Submit a remote NAV report, providing a per-asset-type breakdown.
    /// @dev All values MUST be denominated in this vault's `asset()` units/decimals.
    function reportNav(
        uint32 chainId,
        uint64 nonce,
        bytes32[] calldata typeIds,
        uint256[] calldata values
    ) external onlyMessenger {
        if (typeIds.length != values.length) revert LengthMismatch();

        ChainState storage c = chains[chainId];
        if (!c.enabled) revert ChainDisabled(chainId);
        if (nonce <= c.lastNonce) revert BadNonce(nonce);

        uint256 discounted;
        for (uint256 i = 0; i < typeIds.length; i++) {
            discounted += applyHaircut(typeIds[i], values[i]);
        }

        c.lastNonce = nonce;
        c.lastReportAt = uint64(block.timestamp);
        c.reported = discounted;
        c.hasReport = true;

        uint256 newAcc = _computeAccounted(c.expected, c.reported, c.hasReport, c.maxGainBps);
        _setAccounted(chainId, newAcc);

        emit NavReported(chainId, nonce, discounted);
    }

    function _isFresh() internal view returns (bool) {
        for (uint256 i = 0; i < chainList.length; i++) {
            uint32 id = chainList[i];
            ChainState memory c = chains[id];
            if (!c.enabled) continue;
            if (c.expected == 0 && c.reported == 0) continue;

            if (!c.hasReport) return false;
            if (c.maxStaleness > 0 && block.timestamp > uint256(c.lastReportAt) + uint256(c.maxStaleness)) {
                return false;
            }
        }
        return true;
    }

    // Override ERC4626 accounting: local idle + remote accounted.
    function totalAssets() public view override returns (uint256) {
        return IERC20(asset()).balanceOf(address(this)) + totalRemoteAccounted;
    }

    // Block deposits/mints if remote accounting is stale (prevents mispriced share issuance).
    function maxDeposit(address) public view override returns (uint256) {
        if (paused() || !_isFresh()) return 0;
        return type(uint256).max;
    }

    function maxMint(address) public view override returns (uint256) {
        if (paused() || !_isFresh()) return 0;
        return type(uint256).max;
    }

    // Honest immediate exits: only what is locally liquid right now.
    function maxWithdraw(address owner_) public view override returns (uint256) {
        uint256 claim = convertToAssets(balanceOf(owner_));
        uint256 idle = IERC20(asset()).balanceOf(address(this));
        return claim > idle ? idle : claim;
    }

    function maxRedeem(address owner_) public view override returns (uint256) {
        uint256 idle = IERC20(asset()).balanceOf(address(this));
        uint256 maxSharesByIdle = convertToShares(idle);
        uint256 bal = balanceOf(owner_);
        return bal > maxSharesByIdle ? maxSharesByIdle : bal;
    }

    // ---------- Withdrawal queue (async) ----------

    enum RequestKind {
        RedeemShares,
        WithdrawAssets
    }

    enum RequestStatus {
        None,
        Pending,
        Cancelled,
        Fulfilled
    }

    struct WithdrawalRequest {
        address owner;
        address receiver;
        RequestKind kind;
        uint256 shares;       // escrowed shares (redeem: exact; withdraw: max)
        uint256 assets;       // withdraw target (exact); 0 for redeem
        uint256 minAssetsOut; // redeem slippage guard (ignored for withdraw)
        uint64 deadline;      // after this, owner can cancel
        uint64 createdAt;
        RequestStatus status;
    }

    uint256 public queueHead = 1;
    uint256 public queueTail = 0;
    mapping(uint256 => WithdrawalRequest) public requests;

    event RedeemRequested(uint256 indexed requestId, address indexed owner, address indexed receiver, uint256 shares);
    event WithdrawRequested(
        uint256 indexed requestId,
        address indexed owner,
        address indexed receiver,
        uint256 assets,
        uint256 maxSharesIn
    );
    event RedeemCancelled(uint256 indexed requestId);
    event RedeemFulfilled(uint256 indexed requestId, uint256 shares, uint256 assetsOut);
    event WithdrawFulfilled(
        uint256 indexed requestId,
        uint256 sharesBurned,
        uint256 assetsOut,
        uint256 sharesRefunded
    );
    event LiquidityShortfall(uint256 indexed nextRequestId, uint256 assetsNeeded);
    event SharesShortfall(uint256 indexed nextRequestId, uint256 sharesNeeded, uint256 sharesEscrowed);

    error Expired(uint64 deadline);
    error ZeroAmount();
    error NotOwner(address caller, address owner);
    error TooEarly(uint64 deadline);
    error NotPending(uint256 requestId);
    error Slippage(uint256 assetsOut, uint256 minAssetsOut);
    error TooManyShares(uint256 shares, uint256 maxSharesIn);

    function requestRedeem(
        uint256 shares,
        address receiver,
        address owner_,
        uint256 minAssetsOut,
        uint64 deadline
    ) external whenNotPaused returns (uint256 requestId) {
        if (block.timestamp > deadline) revert Expired(deadline);
        if (shares == 0) revert ZeroAmount();

        if (owner_ != msg.sender) {
            _spendAllowance(owner_, msg.sender, shares);
        }

        _transfer(owner_, address(this), shares);

        requestId = ++queueTail;
        requests[requestId] = WithdrawalRequest({
            owner: owner_,
            receiver: receiver,
            kind: RequestKind.RedeemShares,
            shares: shares,
            assets: 0,
            minAssetsOut: minAssetsOut,
            deadline: deadline,
            createdAt: uint64(block.timestamp),
            status: RequestStatus.Pending
        });

        emit RedeemRequested(requestId, owner_, receiver, shares);
    }

    function requestWithdraw(
        uint256 assets,
        address receiver,
        address owner_,
        uint256 maxSharesIn,
        uint64 deadline
    ) external whenNotPaused returns (uint256 requestId) {
        if (block.timestamp > deadline) revert Expired(deadline);
        if (assets == 0) revert ZeroAmount();

        uint256 shares = previewWithdraw(assets);
        if (shares > maxSharesIn) revert TooManyShares(shares, maxSharesIn);

        // Escrow the max shares; refund the remainder on fulfillment.
        if (owner_ != msg.sender) {
            _spendAllowance(owner_, msg.sender, maxSharesIn);
        }

        _transfer(owner_, address(this), maxSharesIn);

        requestId = ++queueTail;
        requests[requestId] = WithdrawalRequest({
            owner: owner_,
            receiver: receiver,
            kind: RequestKind.WithdrawAssets,
            shares: maxSharesIn,
            assets: assets,
            minAssetsOut: assets,
            deadline: deadline,
            createdAt: uint64(block.timestamp),
            status: RequestStatus.Pending
        });

        emit WithdrawRequested(requestId, owner_, receiver, assets, maxSharesIn);
    }

    function cancelRequest(uint256 requestId) external nonReentrant {
        WithdrawalRequest storage r = requests[requestId];
        if (r.status != RequestStatus.Pending) revert NotPending(requestId);
        if (msg.sender != r.owner) revert NotOwner(msg.sender, r.owner);
        if (block.timestamp <= r.deadline) revert TooEarly(r.deadline);

        r.status = RequestStatus.Cancelled;
        _transfer(address(this), r.owner, r.shares);

        emit RedeemCancelled(requestId);
    }

    function processQueue(uint256 maxToProcess) external nonReentrant returns (uint256 processed) {
        uint256 idle = IERC20(asset()).balanceOf(address(this));

        while (processed < maxToProcess && queueHead <= queueTail) {
            uint256 id = queueHead;
            WithdrawalRequest storage r = requests[id];

            // Skip non-pending slots.
            if (r.status != RequestStatus.Pending) {
                queueHead++;
                continue;
            }

            // If expired, let the owner cancel. Stop at the head to preserve FIFO semantics.
            if (block.timestamp > r.deadline) {
                break;
            }

            uint256 assetsOut;
            uint256 sharesBurned;
            uint256 sharesRefunded;

            if (r.kind == RequestKind.RedeemShares) {
                assetsOut = convertToAssets(r.shares);
                if (assetsOut < r.minAssetsOut) {
                    // Can't satisfy at current PPS; preserve FIFO and let owner cancel after deadline.
                    break;
                }
                sharesBurned = r.shares;
            } else {
                assetsOut = r.assets;
                sharesBurned = previewWithdraw(assetsOut); // rounds up
                if (sharesBurned > r.shares) {
                    emit SharesShortfall(id, sharesBurned, r.shares);
                    break;
                }
                sharesRefunded = r.shares - sharesBurned;
            }

            if (assetsOut > idle) {
                emit LiquidityShortfall(id, assetsOut - idle);
                break;
            }

            r.status = RequestStatus.Fulfilled;
            _burn(address(this), sharesBurned);
            IERC20(asset()).safeTransfer(r.receiver, assetsOut);
            if (sharesRefunded > 0) {
                _transfer(address(this), r.owner, sharesRefunded);
            }

            idle -= assetsOut;

            if (r.kind == RequestKind.RedeemShares) {
                emit RedeemFulfilled(id, sharesBurned, assetsOut);
            } else {
                emit WithdrawFulfilled(id, sharesBurned, assetsOut, sharesRefunded);
            }

            queueHead++;
            processed++;
        }
    }

    // ---------- Admin pause (deposit/mint + new requests) ----------

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    constructor(
        IERC20 asset_,
        string memory name_,
        string memory symbol_,
        address initialOwner
    ) ERC20(name_, symbol_) ERC4626(asset_) Ownable(initialOwner) {}
}
