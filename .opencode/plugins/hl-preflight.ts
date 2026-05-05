import type { Plugin } from "@opencode-ai/plugin"

// Tools that mutate Hyperliquid state. Before any of these run, the agent
// gets a forced look at full HL state (perp + spot + outcome positions +
// asset id maps). Same shape as django's perpetual_agent._tag_user_state —
// the agent never has to guess what's already on the books.
const GUARDED_TOOLS = new Set([
  "wayfinder_hyperliquid_execute",
  "wayfinder_hyperliquid",
  "wayfinder_execute", // covers kind="hyperliquid_deposit"
])

const PREFLIGHT_BASE_URL =
  process.env.WAYFINDER_MCP_URL ?? "http://127.0.0.1:8000"

function fmt(value: unknown): string {
  return JSON.stringify(value, null, 2)
}

export const HLPreflight: Plugin = async () => ({
  "tool.execute.before": async (input, output) => {
    if (!GUARDED_TOOLS.has(input.tool)) return
    // Strip the synthetic `confirmed` flag before the tool sees it — the
    // underlying MCP tool signatures are strict and would reject an unknown
    // arg.
    if (output.args?.confirmed === true) {
      delete output.args.confirmed
      return
    }

    const label = output.args?.wallet_label
    if (typeof label !== "string" || label.length === 0) {
      throw new Error(
        `HL preflight: tool '${input.tool}' requires wallet_label in args.`,
      )
    }

    let preflight: unknown
    try {
      const res = await fetch(`${PREFLIGHT_BASE_URL}/preflight/${encodeURIComponent(label)}`)
      if (!res.ok) {
        throw new Error(
          `preflight HTTP ${res.status}: ${(await res.text()).slice(0, 500)}`,
        )
      }
      preflight = await res.json()
    } catch (cause) {
      throw new Error(
        `HL preflight: failed to fetch state for '${label}' from ${PREFLIGHT_BASE_URL}. ${
          cause instanceof Error ? cause.message : String(cause)
        }`,
      )
    }

    throw new Error(
      [
        `HL preflight for ${input.tool} (wallet: ${label}):`,
        "",
        "Verify the call below against current state before proceeding.",
        "",
        `Pending args:`,
        fmt(output.args),
        "",
        `Current state:`,
        fmt(preflight),
        "",
        "If everything checks out, re-call the tool with the same args plus `confirmed: true`.",
        "If anything looks wrong (insufficient balance, wrong surface, stale asset id), revise args first.",
      ].join("\n"),
    )
  },
})
