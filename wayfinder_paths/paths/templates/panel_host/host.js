// Panel dev-sandbox host: emulates the Wayfinder workspace bridge (protocol
// v1) so authors iterate on a panel with mock data before publishing. The
// message contract here mirrors the real host — keep it in sync with
// PATH_PANEL_BRIDGE.md.
import { THEME, WALLET, MARKETS, FETCH_FIXTURES, RESOURCE_CAPABILITY } from "./fixtures.js";

// WF_CONFIG is injected by the Python server (panel id, slug, granted caps…).
const CONFIG = window.WF_CONFIG || {};
const PROTOCOL_VERSION = 1;
const iframe = document.getElementById("panel-frame");
const grantedCaps = new Set(CONFIG.capabilities || []);

let marketIndex = 0;
let ticking = true;
let panelState = null;
let acked = false;

function log(dir, type, detail) {
  const box = document.getElementById("messages");
  const row = document.createElement("div");
  row.className = "log-row";
  const arrow = dir === "in" ? "← panel" : "host →";
  row.innerHTML =
    `<span class="dir ${dir}">${arrow}</span> <b>${type}</b> ` +
    `<span class="dir">${detail ? escapeHtml(detail) : ""}</span>`;
  box.prepend(row);
}
function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

function post(type, payload) {
  iframe.contentWindow?.postMessage({ type, v: PROTOCOL_VERSION, ...payload }, "*");
  log("out", type, "");
}

function currentContext() {
  const m = MARKETS[marketIndex];
  return {
    instanceId: "wf-dev-instance",
    theme: THEME,
    wallet: WALLET,
    market: { id: m.id, symbol: m.symbol, name: m.name, venue: m.venue },
  };
}

function sendContext() {
  const ctx = currentContext();
  post("wf:context", { context: ctx });
  document.getElementById("context-json").textContent = JSON.stringify(ctx, null, 2);
}

// --- Data proxy emulation: mirror the host's allowlist + grant gates. ---
function handleFetch(msg) {
  const { requestId, capability, resource } = msg;
  const required = RESOURCE_CAPABILITY[resource];
  const rowDetail = `${capability} ${resource}`;
  let allowed = true;
  let error = null;
  if (required === undefined) {
    allowed = false;
    error = { code: "denied", message: "resource not on allowlist" };
  } else if (capability !== required) {
    allowed = false;
    error = { code: "denied", message: `resource requires capability '${required}'` };
  } else if (!grantedCaps.has(capability)) {
    allowed = false;
    error = { code: "denied", message: `capability '${capability}' not granted` };
  }

  logData(rowDetail, allowed);
  if (!allowed) {
    post("wf:fetch_result", { requestId, ok: false, error });
    return;
  }
  const data = FETCH_FIXTURES[resource] ?? { note: "no fixture; returned empty" };
  post("wf:fetch_result", { requestId, ok: true, data });
}

function logData(detail, allowed) {
  const box = document.getElementById("data-log");
  const row = document.createElement("div");
  row.className = "log-row";
  row.innerHTML =
    `<span class="pill ${allowed ? "ok" : "deny"}">${allowed ? "allow" : "deny"}</span> ` +
    `<span class="dir">${escapeHtml(detail)}</span>`;
  box.prepend(row);
}

// --- Ask-agent confirmation (mock). ---
function handleAskAgent(msg) {
  const backdrop = document.getElementById("ask-modal");
  document.getElementById("ask-prompt").textContent = msg.prompt || "";
  backdrop.classList.add("show");
  const finish = (status) => {
    backdrop.classList.remove("show");
    post("wf:ask_agent_result", { requestId: msg.requestId, status });
  };
  document.getElementById("ask-approve").onclick = () => finish("sent");
  document.getElementById("ask-deny").onclick = () => finish("dismissed");
}

window.addEventListener("message", (event) => {
  if (event.source !== iframe.contentWindow) return; // source-window boundary
  const msg = event.data;
  if (!msg || typeof msg !== "object") return;
  log("in", msg.type || "?", "");
  switch (msg.type) {
    case "wf:hello_ack":
      acked = true;
      post("wf:state", { state: panelState });
      sendContext();
      break;
    case "wf:set_state": {
      const bytes = new Blob([JSON.stringify(msg.state ?? null)]).size;
      if (bytes > 32768) {
        log("in", "wf:set_state", `dropped: ${bytes}B > 32KB`);
        return;
      }
      panelState = msg.state ?? null;
      document.getElementById("state-json").textContent = JSON.stringify(panelState, null, 2);
      document.getElementById("state-bytes").textContent = `${bytes} B / 32768 B`;
      post("wf:state_ack", { ok: true, bytes });
      break;
    }
    case "wf:fetch":
      handleFetch(msg);
      break;
    case "wf:ask_agent":
      handleAskAgent(msg);
      break;
    default:
      break; // ignore unknown types
  }
});

// Kick off the handshake once the iframe loads, retrying until ack.
function helloLoop() {
  let attempts = 0;
  const timer = setInterval(() => {
    if (acked || attempts++ > 20) return clearInterval(timer);
    post("wf:hello", {
      protocolVersion: PROTOCOL_VERSION,
      panelId: CONFIG.panelId,
      pathSlug: CONFIG.slug,
      capabilities: ["context", "state", "fetch", "ask_agent"],
    });
  }, 500);
}
iframe.addEventListener("load", () => {
  acked = false;
  helloLoop();
});

// Ticking market price → periodic context refresh (identity is stable; this
// mirrors "context re-sent on change" without spamming on every tick).
setInterval(() => {
  if (!ticking || !acked) return;
  const m = MARKETS[marketIndex];
  m.price = +(m.price * (1 + (Math.random() - 0.5) * 0.004)).toFixed(4);
}, 1000);

// --- Inspector controls ---
document.querySelectorAll(".tabs button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tabpage").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(`tab-${btn.dataset.tab}`).classList.add("active");
  });
});
document.getElementById("market-switch").addEventListener("change", (e) => {
  marketIndex = Number(e.target.value);
  if (acked) sendContext();
});
document.getElementById("tick-toggle").addEventListener("click", (e) => {
  ticking = !ticking;
  e.target.textContent = ticking ? "Pause ticker" : "Resume ticker";
});
document.getElementById("state-reset").addEventListener("click", () => {
  panelState = null;
  if (acked) post("wf:state", { state: null });
  document.getElementById("state-json").textContent = "null";
});
document.querySelectorAll(".presets button").forEach((btn) => {
  btn.addEventListener("click", () => {
    const panel = document.querySelector(".panel");
    panel.style.width = btn.dataset.w + "px";
    panel.style.height = btn.dataset.h + "px";
  });
});

// Populate the market switcher.
const sel = document.getElementById("market-switch");
MARKETS.forEach((m, i) => {
  const opt = document.createElement("option");
  opt.value = String(i);
  opt.textContent = `${m.symbol} · ${m.venue}`;
  sel.appendChild(opt);
});
