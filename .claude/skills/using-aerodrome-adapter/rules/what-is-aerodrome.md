# What is Aerodrome?

Aerodrome is a Base-native DEX built around **ve(3,3)** mechanics (a Solidly/Velodrome-style design):

- Liquidity is provided to pools and represented by an LP token.
- LPs can **stake** their LP in a **gauge** to earn **AERO emissions**.
- AERO can be **locked** into a voting-escrow NFT (“veAERO”, a veNFT) to gain voting power.
- veAERO holders **vote** each epoch to direct AERO emissions across gauges, and earn **fees + bribes**.

## Pool types (two surfaces)

### v2 pools (stable / volatile)

v2 pools use the Solidly-style “stable vs volatile” design.

- **Emissions**: Only LPs who have deposited into the pool’s **gauge** earn AERO emissions.
- **Fees + bribes**: Voters (veAERO) earn trading fees and any incentives (“bribes”) attached to pools.

### Slipstream (concentrated liquidity, “CL”)

Slipstream is a Uniswap v3-style concentrated liquidity system:

- Positions are **NFTs** (tick ranges).
- Pools expose `tickSpacing`, current `tick`, active liquidity, and fee parameters.
- Positions can optionally be staked in gauges (if the pool has a gauge) to earn emissions.

## Epochs (weekly cadence)

Aerodrome operates on weekly epochs:

- Each epoch starts **Thursday 00:00 UTC**.
- A veNFT can typically vote **once per epoch** (see `lastVoted` in the Voter contract).

## veAERO locking (VotingEscrow)

Locking AERO into VotingEscrow mints a **veNFT** (`tokenId`) representing a lock:

- Voting power is proportional to lock size × time remaining (linear decay).
- Max lock is 4 years.

## Rewards

Voters earn:

- **Trading fees** from the previous epoch
- **Bribes/incentives** currently attached to pools they vote for
- A **rebase** (a portion of emissions distributed to veAERO), claimable per veNFT

Rebase is proportional to voting power vs total votes for the epoch:

```
rebase ≈ (veAERO / totalVotes) * emissionsToVe
```

For precise contract addresses and modules, see Aerodrome’s published “Security / Contracts” page.

## References

- https://aerodrome.limited/pages/docs.html
- https://aerodrome.limited/pages/security.html
