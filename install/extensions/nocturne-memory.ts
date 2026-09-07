import type { ExtensionAPI, SlotRenderContext } from "@earendil-works/pi-coding-agent";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// ── Machine-independent config ─────────────────────────────────────────────

// All machine-specific settings live in one JSON file next to these
// extensions: <agent-dir>/extensions/nocturne-memory.config.json (override
// the location with NOCTURNE_CONFIG_PATH). Precedence per key: environment
// variable > config file > built-in default. This replaces the old
// install-time {{PLACEHOLDER}} substitution, which silently baked one
// machine's paths into the extension files.
interface NocturneExtConfig {
	memoryDir?: string;
	piAgentDir?: string;
	apiBaseUrl?: string;
	apiToken?: string;
	embeddingApiKey?: string;
}

const EXT_CONFIG_PATH =
	process.env.NOCTURNE_CONFIG_PATH?.trim() ||
	join(process.env.HOME ?? "", ".pi", "agent", "extensions", "nocturne-memory.config.json");

function loadExtConfig(): NocturneExtConfig {
	try {
		return JSON.parse(readFileSync(EXT_CONFIG_PATH, "utf-8")) as NocturneExtConfig;
	} catch {
		return {};
	}
}

const EXT_CFG = loadExtConfig();

// ── Config ──────────────────────────────────────────────────────────────────


// Same backend the native tools extension calls, so the /set-world-time command
// shares one code path with the set_world_time tool (isolated by namespace).
const MEMORY_API =
	process.env.NOCTURNE_MEMORY_API?.trim() || EXT_CFG.apiBaseUrl || "http://127.0.0.1:8233";
const API_TOKEN = process.env.NOCTURNE_API_TOKEN || EXT_CFG.apiToken || "";

/** Call the Nocturne memory backend like the set_world_time tool does. */
async function setWorldTime(
  time: string,
  characterId?: string,
): Promise<string> {
  const res = await fetch(`${MEMORY_API}/api/pi-tools/invoke`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {}),
    },
    body: JSON.stringify({
      name: "set_world_time",
      params: {
        time,
        ...(characterId ? { character_id: characterId } : {}),
      },
      namespace: process.env.NOCTURNE_NAMESPACE?.trim() ?? "",
      world_clock: { enabled: process.env.WORLD_CLOCK_ENABLED !== "false" },
    }),
  });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`Nocturne Memory API ${res.status}: ${txt}`);
  }
  const json = (await res.json()) as { ok: boolean; message: string };
  if (!json.ok) {
    throw new Error(json.message);
  }
  return json.message;
}
// ── Helper: Async memory slot query via the Nocturne Memory API ─────────────

/**
 * Render a memory slot through the long-lived backend server
 * (POST /api/pi-tools/slot). pi-rp 0.84.2+ supports async slots: the compiler
 * detects `async: true` on the SlotDefinition and uses the async compile
 * path (parallel rendering). The server keeps the Python runtime warm, so a
 * slot render costs milliseconds instead of a full venv cold start per call
 * (the previous execSync query_slot.py path measured ~3s per render).
 */
async function queryMemorySlot(slotType: string, namespace: string): Promise<string> {
  try {
    const res = await fetch(`${MEMORY_API}/api/pi-tools/slot`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {}),
      },
      body: JSON.stringify({ slot_type: slotType, namespace }),
      signal: AbortSignal.timeout(10000),
    });
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(`Nocturne Memory API ${res.status}: ${txt}`);
    }
    const json = (await res.json()) as { ok: boolean; content: string };
    if (!json.ok) {
      throw new Error(json.content);
    }
    return json.content.trim();
  } catch (err) {
    console.error(`Nocturne Memory Slot Error (${slotType} - ${namespace}):`, err);
    return `[Error loading ${slotType} memory for ${namespace}]`;
  }
}


// ── Slot Registration ───────────────────────────────────────────────────────

export default function nocturneMemoryExtension(pi: ExtensionAPI): void {
  // ── Set world time command ────────────────────────────────────────────────

  // /set-world-time <time> [character_id] — convenience wrapper over the
  // set_world_time tool so users can advance the world clock without the agent.
  pi.registerCommand("set-world-time", {
    description:
      "设置世界时间。用法: /set-world-time <日期或偏移> [角色ID] — 如 /set-world-time 2024-06-05 或 /set-world-time +1d；角色ID 留空用当前会话。",
    handler: async (args, ctx) => {
      const trimmed = (args ?? "").trim();
      if (!trimmed) {
        ctx.ui.notify(
          "用法: /set-world-time <日期或偏移> [角色ID]，如 /set-world-time 2024-06-05 或 /set-world-time +1d",
          "error",
        );
        return;
      }
      const [time, characterId] = trimmed.split(/\s+/);
      try {
        const message = await setWorldTime(time, characterId || undefined);
        ctx.ui.notify(`[Nocturne Memory] ${message}`, "info");
      } catch (err) {
        ctx.ui.notify(`[Nocturne Memory] 设置世界时间失败: ${(err as Error).message}`, "error");
      }
    },
  });

  // Register Boot Slot (async: pi-rp 0.84.2+ async compile path)
  pi.registerSlot({
    name: "nocturne-memory-boot",
    description: "Initial memory boot content from Nocturne Memory",
    async: true,
    render: (ctx: SlotRenderContext): Promise<string> => {
      const opts = ctx.item.options as { namespace?: string } | undefined;
      const ns = opts?.namespace?.trim() || process.env.NOCTURNE_NAMESPACE?.trim() || "";
      return queryMemorySlot("boot", ns);
    },
  });

  // Register History Slot (async: pi-rp 0.84.2+ async compile path)
  pi.registerSlot({
    name: "nocturne-memory-history",
    description: "Recent conversation history summaries from Nocturne Memory",
    async: true,
    render: (ctx: SlotRenderContext): Promise<string> => {
      const opts = ctx.item.options as { namespace?: string } | undefined;
      const ns = opts?.namespace?.trim() || process.env.NOCTURNE_NAMESPACE?.trim() || "";
      return queryMemorySlot("history", ns);
    },
  });


  // Register State Slot (async: pi-rp 0.84.2+ async compile path)
  pi.registerSlot({
    name: "nocturne-memory-state",
    description: "Current state/scene records from Nocturne Memory",
    async: true,
    render: (ctx: SlotRenderContext): Promise<string> => {
      const opts = ctx.item.options as { namespace?: string } | undefined;
      const ns = opts?.namespace?.trim() || process.env.NOCTURNE_NAMESPACE?.trim() || "";
      return queryMemorySlot("state", ns);
    },
  });

  // ── Rollback Integration ──────────────────────────────────────────────────

  const WRITE_TOOLS = [
    "remember_child_memory", "edit_memory", "forget_memory", "link_memory",
    "tag_memory", "merge_memories", "organize_memory", "archive_memory",
  ];

  pi.on("tool_result", (event) => {
    if (event.isError) return;
    if (!WRITE_TOOLS.includes(event.toolName)) return;

    const details: unknown = (event as { details?: unknown }).details;
    const structured = (details as { structuredContent?: { result?: { revision_id?: number }; revision_id?: number } } | null | undefined)?.structuredContent;
    const flatRevId = (details as { revision_id?: number } | null | undefined)?.revision_id;
    // FastMCP wraps the Pydantic model dump in a "result" key; native pi tools
    // (nocturne-memory-tools.ts) expose revision_id flat in details.
    const revId = structured?.result?.revision_id ?? structured?.revision_id ?? flatRevId;
    
    if (revId != null) {
      pi.appendEntry("nocturne_memory_checkpoint", { revision_id: revId });
    }
  });

  pi.on("session_tree", async (event, ctx) => {
    const newLeafId = event.newLeafId;
    if (!newLeafId) return;

    const branch = ctx.sessionManager.getBranch(newLeafId);
    let targetRevId: number | null = null;
    
    for (let i = branch.length - 1; i >= 0; i--) {
      const entry = branch[i];
      if (entry.type === "custom" && entry.customType === "nocturne_memory_checkpoint") {
        targetRevId = (entry as { data?: { revision_id?: number } }).data?.revision_id ?? null;
        break;
      }
    }

    if (targetRevId != null) {
      try {
        const res = await fetch(`${MEMORY_API}/review/revisions/${targetRevId}/checkout`, {
          method: "POST",
        });
        if (!res.ok) {
          const txt = await res.text();
          ctx.ui.notify(`[Nocturne Memory] Checkout to revision ${targetRevId} failed: ${txt}`, "error");
        } else {
          ctx.ui.notify(`[Nocturne Memory] Synced memory DB to revision ${targetRevId}.`, "info");
        }
      } catch (err) {
        ctx.ui.notify(`[Nocturne Memory] Error connecting to API: ${err}`, "error");
      }
    }
  });
}
