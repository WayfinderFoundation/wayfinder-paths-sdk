// Wayfinder memory plugin.
//
// Shows the agent its persistent memory index (memory/MEMORY_INDEX.md, one line per
// saved fact) once per session by appending a synthetic text part to the first
// user message: the model sees it, the chat UI hides it, and because parts are
// persisted with the message it becomes fixed history instead of a prompt that
// changes from turn to turn. Topic files are read on demand and written with
// the normal file tools — see the Memory section of the agent prompt.

import { randomBytes } from "node:crypto"
import { readFile } from "node:fs/promises"
import path from "node:path"
import type { Plugin } from "@opencode-ai/plugin"

const INDEX_LIMIT = 16_000
const BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

// Parts are stored ordered by id, and opencode's ascending id helper isn't
// exported from the plugin SDK, so mirror its shape (prt_ + 12 hex chars of
// timestamp*4096+counter + 14 random) with the counter pinned to its maximum so
// this part sorts after the user's own parts from the same millisecond.
function partId(): string {
  const time = ((BigInt(Date.now()) * 4096n + 4095n) & 0xffffffffffffn).toString(16).padStart(12, "0")
  const rand = Array.from(randomBytes(14), (b) => BASE62[b % 62]).join("")
  return `prt_${time}${rand}`
}

export const WayfinderMemory: Plugin = async ({ directory }) => {
  const index = path.join(directory, "memory", "MEMORY_INDEX.md")
  // Sessions that already carry the index this process lifetime. Cleared on
  // compaction so the next message re-injects a fresh copy; a process restart
  // just re-injects once into sessions that are still active.
  const injected = new Set<string>()
  return {
    event: async ({ event }) => {
      if (event.type === "session.compacted") injected.delete(event.properties.sessionID)
    },
    "chat.message": async ({ sessionID }, output) => {
      if (injected.has(sessionID)) return
      injected.add(sessionID)
      const raw = await readFile(index, "utf8").catch(() => "")
      // An explicit empty marker stops the model from listing memory/ to find out
      // whether it exists before its first save.
      const body = !raw.trim()
        ? "memory/ is empty: no memories saved yet."
        : raw.length > INDEX_LIMIT
          ? `${raw.slice(0, INDEX_LIMIT)}\n[memory/MEMORY_INDEX.md is over ${INDEX_LIMIT} characters; only part of it was loaded. Trim the index: one line per entry, detail in topic files.]`
          : `What you remember about this user, from memory/MEMORY_INDEX.md. Read a topic file when its line is relevant.\n${raw}`
      output.parts.push({
        id: partId(),
        sessionID,
        messageID: output.message.id,
        type: "text",
        synthetic: true,
        text: `<memory>\n${body}\n</memory>`,
      })
    },
  }
}
