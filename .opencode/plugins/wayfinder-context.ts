import type { Plugin } from "@opencode-ai/plugin"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js"

let clientPromise: Promise<Client> | null = null
function getClient(): Promise<Client> {
  if (!clientPromise) {
    clientPromise = (async () => {
      const transport = new StreamableHTTPClientTransport(
        new URL("http://127.0.0.1:8000/mcp"),
      )
      const c = new Client({ name: "wayfinder-context", version: "0.1.0" })
      await c.connect(transport)
      return c
    })()
  }
  return clientPromise
}

async function fetchWallets(): Promise<string> {
  try {
    const client = await getClient()
    const res = await client.callTool({ name: "wayfinder_core_get_wallets", arguments: {} })
    return JSON.stringify(res.content, null, 2)
  } catch (err) {
    return JSON.stringify({ error: err instanceof Error ? err.message : String(err) })
  }
}

const COMPACTION_RULES = [
  "Compaction rules — preserve verbatim, do NOT paraphrase or summarize:",
  "- Every wallet label, address, and the protocols each wallet has touched.",
  "- Every scheduled job: name, type, interval, payload, last status.",
  "- Every transaction hash, order ID, cloid, condition_id, and chain id from tool outputs.",
  "- Every strategy lifecycle state change (deposit/update/withdraw/exit) with amounts and timestamps.",
  "- Every user decision about which wallet, chain, slippage, or amount to use — these are commitments, not preferences.",
  "Do summarize: high-level reasoning, search results that were rejected, intermediate quote comparisons that didn't lead to action.",
].join("\n")

export const WayfinderContext: Plugin = async () => ({
  "experimental.chat.system.transform": async (_input, output) => {
    const wallets = await fetchWallets()
    output.system.push(
      [
        "<wallet-state>",
        "Live result of wayfinder_core_get_wallets — refreshed on every LLM call.",
        "Verify against user intent before any execute tool call (correct label, sufficient balance on the right chain).",
        wallets,
        "</wallet-state>",
      ].join("\n"),
    )
  },
  "experimental.session.compacting": async (_input, output) => {
    const wallets = await fetchWallets()
    output.context.push(COMPACTION_RULES)
    output.context.push(
      [
        "<wallet-state-at-compaction>",
        "Snapshot of wayfinder_core_get_wallets at the moment of compaction — keep these labels and addresses intact in the summary.",
        wallets,
        "</wallet-state-at-compaction>",
      ].join("\n"),
    )
  },

  // EXAMPLE: pre-tool-call arg mutation. Default wallet_label to "main" if
  // the agent forgot to pass one to a wayfinder_core_execute call.
  "tool.execute.before": async (input, output) => {
    if (input.tool !== "wayfinder_core_execute") return
    if (output.args && typeof output.args === "object" && !output.args.wallet_label) {
      output.args.wallet_label = "main"
    }
  },

  // EXAMPLE: post-tool-call context injection. Append a fresh wallet snapshot
  // to the tool's output string so the next LLM turn sees the impact of the
  // call without needing a separate fetch.
  "tool.execute.after": async (input, output) => {
    if (!input.tool.endsWith("_execute") && !input.tool.endsWith("_run_strategy")) {
      return
    }
    const wallets = await fetchWallets()
    output.output = [
      output.output,
      "",
      "<wallet-state-after-call>",
      wallets,
      "</wallet-state-after-call>",
    ].join("\n")
  },
})
