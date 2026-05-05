import type { Plugin } from "@opencode-ai/plugin"
import { Client } from "@modelcontextprotocol/sdk/client/index.js"
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js"

type Args = Record<string, unknown>
type Call = {
  name: string
  args?: Args
  key?: string
}

const AGENT_PREFLIGHT: Record<string, Call[]> = {
  hyperliquid: [
    { key: "wallets", name: "wayfinder_onchain_list_wallets" },
    { key: "perp_markets", name: "wayfinder_hyperliquid_get_markets" },
    { key: "spot_assets", name: "wayfinder_hyperliquid_get_spot_assets" },
    { key: "outcomes", name: "wayfinder_hyperliquid_get_outcomes" },
  ],
  polymarket: [{ key: "wallets", name: "wayfinder_onchain_list_wallets" }],
  onchain: [{ key: "wallets", name: "wayfinder_onchain_list_wallets" }],
}

let clientPromise: Promise<Client> | null = null
function getClient(): Promise<Client> {
  if (!clientPromise) {
    clientPromise = (async () => {
      const transport = new StreamableHTTPClientTransport(
        new URL("http://127.0.0.1:8000/mcp"),
      )
      const c = new Client({ name: "wayfinder-preflight", version: "0.1.0" })
      await c.connect(transport)
      return c
    })()
  }
  return clientPromise
}

async function runCalls(
  client: Client,
  calls: Call[],
): Promise<Record<string, unknown>> {
  const results = await Promise.all(
    calls.map(async (c) => {
      try {
        const res = await client.callTool({
          name: c.name,
          arguments: c.args ?? {},
        })
        return [c.key ?? c.name, res.content] as const
      } catch (err) {
        return [
          c.key ?? c.name,
          { error: err instanceof Error ? err.message : String(err) },
        ] as const
      }
    }),
  )
  return Object.fromEntries(results)
}

export const Preflight: Plugin = async () => ({
  "chat.message": async (input, output) => {
    const calls = input.agent ? AGENT_PREFLIGHT[input.agent] : undefined
    if (!calls) return

    const client = await getClient()
    const bundle = await runCalls(client, calls)

    output.parts.push({
      id: `prt_preflight_${Date.now()}`,
      sessionID: input.sessionID,
      messageID: input.messageID ?? "",
      type: "text",
      synthetic: true,
      text: [
        "<system-reminder>",
        `Current state for the ${input.agent} surface — verify against user intent before any execute call:`,
        JSON.stringify(bundle, null, 2),
        "</system-reminder>",
      ].join("\n"),
    })
  },
})
