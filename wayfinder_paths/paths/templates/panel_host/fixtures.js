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
export const MARKETS = [
  { id: "HYPE", symbol: "HYPE", name: "Hyperliquid", venue: "hyperliquid", price: 55.4 },
  { id: "BTC", symbol: "BTC", name: "Bitcoin", venue: "hyperliquid", price: 64800 },
  { id: "ETH", symbol: "ETH", name: "Ethereum", venue: "hyperliquid", price: 1863 },
];

// Canned wf:fetch responses, keyed by resource. The sandbox reports which
// capability a panel requested and whether it would be allowed, then returns
// the matching mock (or a not-found error) — so authors build the data flow
// entirely offline.
export const FETCH_FIXTURES = {
  "/blockchain/hyperliquid/markets": {
    markets: [
      { id: "HYPE", symbol: "HYPE", fundingRate: 0.000125, openInterest: 12500000 },
      { id: "BTC", symbol: "BTC", fundingRate: 0.00008, openInterest: 2240000000 },
      { id: "ETH", symbol: "ETH", fundingRate: -0.00002, openInterest: 553000000 },
    ],
  },
  "/blockchain/hyperliquid/funding": { coin: "HYPE", fundingRate: 0.000125 },
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

// Which capability each allowlisted resource requires (mirrors the host's
// pathPanelDataAllowlist). Used to show authors the deny path.
export const RESOURCE_CAPABILITY = {
  "/blockchain/hyperliquid/markets": "market.read",
  "/blockchain/hyperliquid/funding": "market.read",
  "/blockchain/balances/wallet/": "wallet_read",
  "/blockchain/hyperliquid/portfolio-state/": "positions.read",
};
