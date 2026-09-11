// Funding Radar — Wayfinder path panel (bridge protocol v1, plain JS).
//
// Lists Hyperliquid perps (equity perps from HIP-3 builder dexes by
// default), sortable by current 1h funding, 24h price change, and 24h
// funding drift. Clicking a row asks the host to switch the workspace's
// active market (wf:set_market) — the chart + trade ticket follow.
//
// Data flow: one markets snapshot on boot + every 30s (matches the
// backend cache TTL), then a slow per-coin funding-history backfill that
// stays inside the panel's rate budget (burst 10, 30/min).

const PROTOCOL_VERSION = 1;
const MARKETS_RESOURCE = "/blockchain/hyperliquid/markets/";
const FUNDING_RESOURCE = "/blockchain/hyperliquid/funding/";
const REFRESH_MS = 30_000;
const BACKFILL_SPACING_MS = 3_000;
const DAY_MS = 24 * 3600 * 1000;

const pending = new Map();
let seq = 0;

// --- UI + data state -------------------------------------------------------
let rows = []; // [{coin, symbol, dex, mark, funding, priceD24, fundingD24}]
let sortKey = "funding";
let sortDir = -1; // -1 = descending (hot side first)
let scope = "equities"; // "equities" | "all"
let activeSymbol = null; // context market symbol, highlighted in the table
let fundingCache = {}; // coin -> {d24: number, at: ms} — persisted panel state
let backfillQueue = [];
let backfillTimer = null;
let hostCaps = new Set();

// --- Bridge plumbing -------------------------------------------------------
function post(type, payload) {
  window.parent?.postMessage({ type, v: PROTOCOL_VERSION, ...payload }, "*");
}

function request(type, payload) {
  const requestId = `r${++seq}`;
  return new Promise((resolve, reject) => {
    pending.set(requestId, { resolve, reject });
    post(type, { requestId, ...payload });
  });
}

const fetchData = (capability, resource, params) =>
  request("wf:fetch", { capability, resource, params });

const setMarket = (symbol) =>
  request("wf:set_market", { symbol }).then(
    () => true,
    () => false,
  );

function saveState() {
  // Keep well under the 32KB cap: cache only what re-renders need fast.
  post("wf:set_state", {
    state: { sortKey, sortDir, scope, fundingCache },
  });
}

// --- Rendering -------------------------------------------------------------
// Coin/dex names come from upstream market metadata (builder-dex names are
// third-party-controlled) — escape anything interpolated into markup.
const escapeHtml = (s) =>
  String(s).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

const fmtPct = (value, digits = 4) =>
  `${value >= 0 ? "+" : ""}${(value * 100).toFixed(digits)}%`;

function signedCell(value, digits) {
  const cls = value > 0 ? "pos" : value < 0 ? "neg" : "dim";
  return `<span class="${cls}">${fmtPct(value, digits)}</span>`;
}

function fmtMark(px) {
  if (px >= 1000) return px.toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (px >= 10) return px.toFixed(2);
  return px.toFixed(4);
}

function visibleRows() {
  const filtered = scope === "equities" ? rows.filter((r) => r.dex) : rows;
  return [...filtered].sort((a, b) => {
    const av = a[sortKey];
    const bv = b[sortKey];
    // Rows without a backfilled Δ24h funding sink to the bottom.
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    return sortDir === -1 ? bv - av : av - bv;
  });
}

function render() {
  const body = document.getElementById("rows");
  const html = visibleRows()
    .map((r) => {
      const apr = r.funding * 24 * 365;
      const active = r.symbol === activeSymbol ? " active-market" : "";
      const coin = escapeHtml(r.coin);
      const symbol = escapeHtml(r.symbol);
      return (
        `<tr data-coin="${coin}" data-symbol="${symbol}" class="row${active}">` +
        `<td><span class="sym">${symbol}</span>` +
        (r.dex ? `<span class="dexchip">${escapeHtml(r.dex)}</span>` : "") +
        `</td>` +
        `<td>${fmtMark(r.mark)}</td>` +
        `<td>${signedCell(r.funding, 5)}<span class="apr dim">${fmtPct(apr, 0)}/y</span></td>` +
        `<td>${signedCell(r.priceD24, 2)}</td>` +
        `<td>${r.fundingD24 == null ? '<span class="dim">…</span>' : signedCell(r.fundingD24, 5)}</td>` +
        `</tr>`
      );
    })
    .join("");
  body.innerHTML = html || `<tr><td colspan="5" class="dim">No markets in scope.</td></tr>`;

  document.querySelectorAll(".pill.sort").forEach((btn) => {
    const isActive = btn.dataset.key === sortKey;
    btn.classList.toggle("active", isActive);
    btn.querySelector(".arrow").textContent = isActive ? (sortDir === -1 ? "↓" : "↑") : "";
  });
  document.getElementById("scope-toggle").textContent =
    scope === "equities" ? "Equities" : "All perps";
}

function setStatus(text) {
  document.getElementById("status").firstChild.textContent = text;
}

function setNote(text) {
  const note = document.getElementById("note");
  note.textContent = text;
  if (text) setTimeout(() => (note.textContent = ""), 2500);
}

// --- Data ------------------------------------------------------------------
function flattenMarkets(data) {
  const out = [];
  for (const entry of data?.perp ?? []) {
    const universe = entry?.meta?.universe ?? [];
    const ctxs = entry?.ctxs ?? [];
    universe.forEach((asset, i) => {
      const ctx = ctxs[i];
      if (!ctx) return;
      const mark = Number(ctx.markPx);
      const prev = Number(ctx.prevDayPx);
      const coin = String(asset.name);
      out.push({
        coin,
        symbol: coin.includes(":") ? coin.split(":")[1] : coin,
        dex: entry.dex || null,
        mark,
        funding: Number(ctx.funding),
        priceD24: prev > 0 ? (mark - prev) / prev : 0,
        fundingD24: fundingCache[coin]?.d24 ?? null,
      });
    });
  }
  return out;
}

async function refreshMarkets() {
  try {
    const data = await fetchData("market.read", MARKETS_RESOURCE);
    rows = flattenMarkets(data);
    setStatus(`${rows.length} markets · updated ${new Date().toLocaleTimeString()}`);
    render();
    scheduleBackfill();
  } catch (err) {
    setStatus(`markets fetch failed: ${err.message}`);
  }
}

// Δ24h funding: one history call per coin, spaced out so the radar plus
// its 30s refresh stay inside the host's rate budget. Fresh cache entries
// (<30min) are skipped so panel re-opens don't refetch the world.
function scheduleBackfill() {
  const now = Date.now();
  const inScope = scope === "equities" ? rows.filter((r) => r.dex) : rows;
  backfillQueue = inScope
    .filter((r) => now - (fundingCache[r.coin]?.at ?? 0) > 30 * 60_000)
    .map((r) => r.coin);
  if (backfillTimer === null) pumpBackfill();
}

async function pumpBackfill() {
  const coin = backfillQueue.shift();
  if (!coin) {
    backfillTimer = null;
    return;
  }
  const now = Date.now();
  try {
    const data = await fetchData("market.read", FUNDING_RESOURCE, {
      coin,
      start_ms: now - DAY_MS,
      end_ms: now,
    });
    const first = data?.rows?.[0];
    const row = rows.find((r) => r.coin === coin);
    if (first && row) {
      const d24 = row.funding - Number(first.fundingRate);
      fundingCache[coin] = { d24, at: now };
      row.fundingD24 = d24;
      render();
      saveState();
    }
  } catch {
    // Rate-limited or upstream error — drop this coin for now; the next
    // scheduleBackfill pass retries anything still stale.
  }
  backfillTimer = setTimeout(pumpBackfill, BACKFILL_SPACING_MS);
}

// --- Interactions ----------------------------------------------------------
document.getElementById("rows").addEventListener("click", async (event) => {
  const tr = event.target.closest("tr[data-coin]");
  if (!tr) return;
  if (!hostCaps.has("set_market")) {
    setNote("host can't switch markets");
    return;
  }
  const ok = await setMarket(tr.dataset.coin);
  if (ok) {
    tr.classList.add("flash");
    setTimeout(() => tr.classList.remove("flash"), 600);
  } else {
    setNote(`${tr.dataset.symbol} not available here`);
  }
});

document.querySelectorAll(".pill.sort").forEach((btn) => {
  btn.addEventListener("click", () => {
    const key = btn.dataset.key;
    if (sortKey === key) sortDir *= -1;
    else {
      sortKey = key;
      sortDir = -1;
    }
    render();
    saveState();
  });
});

document.getElementById("scope-toggle").addEventListener("click", () => {
  scope = scope === "equities" ? "all" : "equities";
  render();
  saveState();
  scheduleBackfill();
});

// --- Bridge message pump ---------------------------------------------------
function applyTheme(theme) {
  for (const [name, value] of Object.entries(theme?.tokens ?? {})) {
    document.documentElement.style.setProperty(`--wf-${name}`, value);
  }
}

window.addEventListener("message", (event) => {
  const msg = event.data;
  if (!msg || typeof msg !== "object") return;
  switch (msg.type) {
    case "wf:hello":
      hostCaps = new Set(msg.capabilities || []);
      post("wf:hello_ack", { protocolVersion: PROTOCOL_VERSION });
      break;
    case "wf:context":
      applyTheme(msg.theme);
      activeSymbol = msg.market?.symbol ?? null;
      render();
      break;
    case "wf:state": {
      const state = msg.state;
      if (state && typeof state === "object") {
        if (typeof state.sortKey === "string") sortKey = state.sortKey;
        if (state.sortDir === 1 || state.sortDir === -1) sortDir = state.sortDir;
        if (state.scope === "all" || state.scope === "equities") scope = state.scope;
        if (state.fundingCache && typeof state.fundingCache === "object") {
          fundingCache = state.fundingCache;
        }
      }
      refreshMarkets();
      setInterval(refreshMarkets, REFRESH_MS);
      break;
    }
    case "wf:fetch_result":
    case "wf:set_market_result": {
      const entry = pending.get(msg.requestId);
      if (!entry) return;
      pending.delete(msg.requestId);
      if (msg.ok) entry.resolve(msg.data);
      else entry.reject(new Error(msg.error?.message || msg.error?.code || "failed"));
      break;
    }
    default:
      break; // forward compatibility
  }
});
