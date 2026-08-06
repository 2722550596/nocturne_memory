import type { ExtensionAPI, SlotRenderContext } from "@earendil-works/pi-coding-agent";
import { execSync } from "node:child_process";
import { join } from "node:path";

// ── Config ──────────────────────────────────────────────────────────────────

// The installation script will replace this placeholder with the actual project path.
const MEMORY_DIR = "{{MEMORY_DIR}}";
const PYTHON_BIN = join(MEMORY_DIR, "venv", "bin", "python3");
const QUERY_SCRIPT = join(MEMORY_DIR, "query_slot.py");

// ── Helper: Execute synchronous memory query ────────────────────────────────

function queryMemorySync(slotType: string, namespace: string = "default"): string {
  try {
    const cmd = `${PYTHON_BIN} ${QUERY_SCRIPT} ${slotType} ${namespace}`;
    const output = execSync(cmd, { encoding: "utf-8", timeout: 10000 });
    return output.trim();
  } catch (err) {
    console.error(`Nocturne Memory Slot Error (${slotType} - ${namespace}):`, err);
    return `[Error loading ${slotType} memory for ${namespace}]`;
  }
}

// ── Slot Registration ───────────────────────────────────────────────────────

export default function nocturneMemoryExtension(pi: ExtensionAPI): void {
  // Register Boot Slot
  pi.registerSlot({
    name: "nocturne-memory-boot",
    description: "Initial memory boot content from Nocturne Memory",
    render: (ctx: SlotRenderContext): string => {
      const ns = (ctx.item.options?.namespace as string) || "default";
      return queryMemorySync("boot", ns);
    },
  });

  // Register History Slot
  pi.registerSlot({
    name: "nocturne-memory-history",
    description: "Recent conversation history summaries from Nocturne Memory",
    render: (ctx: SlotRenderContext): string => {
      const ns = (ctx.item.options?.namespace as string) || "default";
      return queryMemorySync("history", ns);
    },
  });

  // Register State Slot
  pi.registerSlot({
    name: "nocturne-memory-state",
    description: "Current state/scene records from Nocturne Memory",
    render: (ctx: SlotRenderContext): string => {
      const ns = (ctx.item.options?.namespace as string) || "default";
      return queryMemorySync("state", ns);
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
    if (!structured) return;
    // FastMCP wraps the Pydantic model dump in a "result" key
    const revId = structured.result?.revision_id ?? structured.revision_id;
    
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
        const res = await fetch(`http://127.0.0.1:8233/review/revisions/${targetRevId}/checkout`, {
          method: "POST",
        });
        if (!res.ok) {
          const txt = await res.text();
          ctx.ui.print(`\n[Nocturne Memory] Checkout to revision ${targetRevId} failed: ${txt}`);
        } else {
          ctx.ui.print(`\n[Nocturne Memory] Synced memory DB to revision ${targetRevId}.`);
        }
      } catch (err) {
        ctx.ui.print(`\n[Nocturne Memory] Error connecting to API: ${err}`);
      }
    }
  });
}
