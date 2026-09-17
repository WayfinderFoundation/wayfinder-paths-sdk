// Mock workspace data for the panel dev sandbox. Shapes here MUST mirror the
// host's real bridge payloads (see PATH_PANEL_BRIDGE.md in vault-frontend) so
// a panel that works against these fixtures works in the real workspace.

export const THEME = {
  mode: "dark",
  tokens: {
    background: "#17171a",
    foreground: "#e9eef2",
    card: "rgba(255,255,255,0.03)",
    popover: "#252529",
    primary: "#98f283",
    "primary-foreground": "#06210c",
    muted: "rgba(255,255,255,0.06)",
    "muted-foreground": "rgba(233,238,242,0.6)",
    border: "rgba(255,255,255,0.08)",
    destructive: "#f4626e",
    signal: "#4dc7fa",
  },
};

export const WALLET = { address: "0x1111111111111111111111111111111111111111" };

// Markets the sandbox cycles / lets you switch between (wf:context.market).
// The equity entries mirror HIP-3 builder-dex perps: their HL coin name is
// "xyz:TSLA"-style, which is also what panels pass to bridge.setMarket().
export const MARKETS = [
  { id: "HYPE", symbol: "HYPE", name: "Hyperliquid", venue: "hyperliquid", price: 55.4 },
  { id: "BTC", symbol: "BTC", name: "Bitcoin", venue: "hyperliquid", price: 64800 },
  { id: "ETH", symbol: "ETH", name: "Ethereum", venue: "hyperliquid", price: 1863 },
  { id: "xyz:TSLA", symbol: "TSLA", name: "Tesla (xyz)", venue: "hyperliquid", price: 288.4 },
  { id: "xyz:NVDA", symbol: "NVDA", name: "Nvidia (xyz)", venue: "hyperliquid", price: 176.2 },
  { id: "xyz:HOOD", symbol: "HOOD", name: "Robinhood (xyz)", venue: "hyperliquid", price: 101.7 },
  { id: "xyz:COIN", symbol: "COIN", name: "Coinbase (xyz)", venue: "hyperliquid", price: 312.5 },
];

// Canned wf:fetch responses, keyed by resource. The sandbox reports which
// capability a panel requested and whether it would be allowed, then returns
// the matching mock (or a not-found error) — so authors build the data flow
// entirely offline.
//
// The markets fixture uses the REAL /blockchain/hyperliquid/markets/ response
// shape: perp[] entries per dex (dex: null = main; "xyz" = a HIP-3 builder
// dex hosting equity perps), each zipping meta.universe[i] with ctxs[i].
export const FETCH_FIXTURES = {
  "/blockchain/hyperliquid/markets/": {
    perp: [
      {
        index: 0,
        dex: null,
        meta: {
          universe: [
            { name: "HYPE", szDecimals: 2, maxLeverage: 25, onlyIsolated: false },
            { name: "BTC", szDecimals: 5, maxLeverage: 50, onlyIsolated: false },
            { name: "ETH", szDecimals: 4, maxLeverage: 50, onlyIsolated: false },
          ],
        },
        ctxs: [
          { funding: "0.0000131", markPx: "55.4", prevDayPx: "53.9", openInterest: "13420000", dayNtlVlm: "88200000" },
          { funding: "0.0000078", markPx: "64800", prevDayPx: "65400", openInterest: "2310000000", dayNtlVlm: "1904000000" },
          { funding: "-0.0000019", markPx: "1863", prevDayPx: "1830", openInterest: "561000000", dayNtlVlm: "742000000" },
        ],
      },
      {
        index: 1,
        dex: "xyz",
        meta: {
          universe: [
            { name: "xyz:TSLA", szDecimals: 2, maxLeverage: 10, onlyIsolated: true },
            { name: "xyz:NVDA", szDecimals: 2, maxLeverage: 10, onlyIsolated: true },
            { name: "xyz:HOOD", szDecimals: 2, maxLeverage: 5, onlyIsolated: true },
            { name: "xyz:COIN", szDecimals: 2, maxLeverage: 5, onlyIsolated: true },
          ],
        },
        ctxs: [
          { funding: "0.0000892", markPx: "288.4", prevDayPx: "296.1", openInterest: "51000000", dayNtlVlm: "12800000" },
          { funding: "0.0000315", markPx: "176.2", prevDayPx: "171.6", openInterest: "84000000", dayNtlVlm: "31500000" },
          { funding: "-0.0000441", markPx: "101.7", prevDayPx: "97.4", openInterest: "12500000", dayNtlVlm: "5400000" },
          { funding: "0.0000127", markPx: "312.5", prevDayPx: "309.9", openInterest: "22000000", dayNtlVlm: "9800000" },
        ],
      },
    ],
    spot: [],
  },
  "/blockchain/balances/wallet/": {
    balances: [
      { symbol: "USDC", balance: "778.64", usdValue: 778.64 },
      { symbol: "HYPE", balance: "12.5", usdValue: 692.5 },
    ],
  },
  "/blockchain/hyperliquid/portfolio-state/": {
    clearinghouseState: {
      assetPositions: [
        { position: { coin: "HYPE", szi: "10.0", unrealizedPnl: "42.10" } },
      ],
    },
  },
};

// Param-aware fixtures: resource -> (params) => data. Checked before the
// static FETCH_FIXTURES map. The funding-history generator is deterministic
// per coin so sort orders are stable across refreshes.
export const FETCH_FIXTURE_FNS = {
  "/blockchain/hyperliquid/funding/": (params) => {
    const coin = String(params?.coin || "BTC");
    const end = Number(params?.end_ms || 1735689600000);
    // Cheap stable hash -> base rate + drift so every coin differs.
    let hash = 0;
    for (const ch of coin) hash = (hash * 31 + ch.charCodeAt(0)) % 9973;
    const base = ((hash % 200) - 100) / 1e7; // ±0.00001
    const drift = ((hash % 37) - 18) / 1e8;
    const rows = [];
    for (let i = 24; i >= 1; i -= 1) {
      rows.push({
        time: end - i * 3600_000,
        fundingRate: (base + drift * (24 - i)).toFixed(9),
      });
    }
    return { coin, rows };
  },
};

// Which capability each allowlisted resource requires. Resource names are
// the stable PANEL-FACING contract (vendor-neutral — e.g. wallet PnL lives
// under /blockchain/portfolio/*, whatever provider serves it upstream).
// Mirrors the production host's pathPanelDataAllowlist.ts AND the --live
// proxy's allowlist in preview.py — keep all three in lockstep. Resources
// without a FETCH_FIXTURES entry return a "no fixture" note in mock mode but
// serve real data in --live mode.
export const RESOURCE_CAPABILITY = {
  "/blockchain/balances/wallet/": "wallet_read",
  "/blockchain/balances/aggregated-chart": "wallet_read",
  "/blockchain/balances/activity/": "wallet_read",
  "/blockchain/hyperliquid/portfolio-state/": "positions.read",
  "/blockchain/portfolio/balance-chart": "pnl.read",
  "/blockchain/portfolio/pnl": "pnl.read",
  "/blockchain/hyperliquid/markets/": "market.read",
  "/blockchain/hyperliquid/funding/": "market.read",
  "/blockchain/tokens/discover/": "market.read",
  "/blockchain/tokens/security/": "market.read",
};
