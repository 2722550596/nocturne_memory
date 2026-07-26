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
}
