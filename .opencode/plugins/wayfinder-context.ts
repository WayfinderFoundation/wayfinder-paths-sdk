import type { Plugin } from "@opencode-ai/plugin";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

let clientPromise: Promise<Client> | null = null;
function getClient(): Promise<Client> {
  if (!clientPromise) {
    clientPromise = (async () => {
      const transport = new StreamableHTTPClientTransport(
        new URL("http://127.0.0.1:8000/mcp"),
      );
      const c = new Client({ name: "wayfinder-context", version: "0.1.0" });
      await c.connect(transport);
      return c;
    })();
  }
  return clientPromise;
}

async function fetchWallets(): Promise<string> {
  try {
    const client = await getClient();
    const res = await client.callTool({
      name: "wayfinder_core_get_wallets",
      arguments: {},
    });
    return JSON.stringify(res.content, null, 2);
  } catch (err) {
    return JSON.stringify({
      error: err instanceof Error ? err.message : String(err),
    });
  }
}

const COMPACTION_RULES = [
  "COMPACTION RULES:",
  "- Compact user preferences, tendencies and common actions and parameters",
  "- Compact a list of previous transactions, and relevant information",
  "EXCLUDE:",
  "- Wallet balances (too volatile, better to fetch live)",
].join("\n");

export const WayfinderContext: Plugin = async () => ({
  "experimental.session.compacting": async (_input, output) => {
    output.context.push(COMPACTION_RULES);
  },
  "experimental.chat.system.transform": async (_input, output) => {
    const wallets = await fetchWallets();
    output.system.push(
      [
        "<wallet-state>",
        "Live result of wayfinder_core_get_wallets — refreshed on every LLM call.",
        "Verify against user intent before any execute tool call (correct label, sufficient balance on the right chain).",
        wallets,
        "</wallet-state>",
      ].join("\n"),
    );
  },
  // EXAMPLE: pre-tool-call arg mutation. Default wallet_label to "main" if
  // the agent forgot to pass one to a wayfinder_core_execute call.
  "tool.execute.before": async (input, output) => {
    if (input.tool !== "wayfinder_core_execute") return;
    if (
      output.args &&
      typeof output.args === "object" &&
      !output.args.wallet_label
    ) {
      output.args.wallet_label = "main";
    }
  },
});
