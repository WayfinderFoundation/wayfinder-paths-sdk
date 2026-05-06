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

async function fetchContext(): Promise<string> {
  try {
    const client = await getClient();
    const res = await client.callTool({
      name: "core_get_context",
      arguments: {},
    });
    return (res.content as Array<{ text: string }>)[0].text;
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
    const ctx = await fetchContext();
    output.system.push(
      [
        "<wayfinder-context>",
        "Live result of wayfinder_core_get_context — refreshed on every LLM call.",
        "Coin balances, HL positions/open-orders/top-markets, Polymarket positions/open-orders.",
        "USD values and mark prices are intentionally excluded — fetch live via the rich tools when needed.",
        ctx,
        "</wayfinder-context>",
      ].join("\n"),
    );
  },
});
