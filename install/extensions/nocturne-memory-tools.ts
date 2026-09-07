/**
 * Nocturne Memory Tools — native pi extension.
 *
 * Registers the full Nocturne memory toolset (previously exposed via MCP) as
 * native pi tools using pi.registerTool(). Each tool calls a single long-lived
 * HTTP backend (`python main.py`, port 8233) via /api/pi-tools/invoke, so pi
 * sessions start with zero Python startup cost while all business logic stays
 * in the Python backend untouched.
 *
 * Requires the Nocturne Memory web server to be running (systemd service).
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type, type TSchema } from "typebox";
import { StringEnum } from "@earendil-works/pi-ai";
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

const MEMORY_API =
	process.env.NOCTURNE_MEMORY_API?.trim() || EXT_CFG.apiBaseUrl || "http://127.0.0.1:8233";
const API_TOKEN = process.env.NOCTURNE_API_TOKEN || EXT_CFG.apiToken || "";

// ── Invoke helper ───────────────────────────────────────────────────────────

async function invoke(
	name: string,
	params: Record<string, unknown>,
	signal?: AbortSignal,
	namespace?: string,
): Promise<{ message: string; data: Record<string, unknown> }> {
	const res = await fetch(`${MEMORY_API}/api/pi-tools/invoke`, {
		method: "POST",
		headers: {
			"Content-Type": "application/json",
			...(API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {}),
		},
		signal,
		body: JSON.stringify({
			name,
			params,
			namespace: namespace ?? process.env.NOCTURNE_NAMESPACE?.trim() ?? "",
			// Per-request world-clock mode, read from the pi process env that the
			// role launch script exports (WORLD_CLOCK_ENABLED=true/false). This
			// replaces the MCP env-passing for clock isolation.
			world_clock: { enabled: process.env.WORLD_CLOCK_ENABLED !== "false" },
		}),
	});
	if (!res.ok) {
		throw new Error(`Nocturne Memory API ${res.status}: ${await res.text()}`);
	}
	const json = (await res.json()) as {
		ok: boolean;
		message: string;
		data?: Record<string, unknown>;
	};
	if (!json.ok) {
		throw new Error(json.message);
	}
	return { message: json.message, data: json.data ?? {} };
}

// ── Tool definitions ────────────────────────────────────────────────────────

interface MemoryToolDef {
	name: string;
	label: string;
	description: string;
	/** TypeBox parameter schema shown to the LLM. */
	parameters: TSchema;
}

const TOOLS: MemoryToolDef[] = [
	{
		name: "browse_memory",
		label: "浏览记忆",
		description:
			"查看一段记忆的内容。这是回想起某件事的主要方式。输入 URI 即可看到内容、子节点和触发词关联。depth 可展开子树（-1 = 整棵，N = N 层全文），适合一次读完一个主题。系统视图：system://boot、system://wakeup/<N>、system://index/<domain>、system://recent/<N>、system://timeline/<domain>/<N>（按世界时间的事件轴）、system://forgotten/<domain>/<N>（沉睡最久的记忆）、system://glossary、system://diagnostic/<domain>。",
		parameters: Type.Object({
			uri: Type.String({ description: "记忆 URI，如 core://identity/habits，或 system://boot" }),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
			depth: Type.Optional(Type.Number({ description: "展开子树的层数。0（默认）= 本节点全文 + 直接子节点 URI 列表；1 = 本节点 + 直接子节点全文；N = 递归 N 层；-1 = 展开整棵子树。" })),
			max_nodes: Type.Optional(Type.Number({ description: "子树模式下最多渲染多少条节点的正文，防止一次读取刷爆上下文。到达上限后剩余节点只列 URI 并标注「内容省略」。默认 200。" })),
		}),
	},
	{
		name: "search_memory",
		label: "搜索记忆",
		description:
			"搜索记忆。想不起 URI 时用这个来找。默认全文搜索（词法）；semantic=true 启用语义检索（需配置 embedding API，未配置自动退化词法）。",
		parameters: Type.Object({
			query: Type.String({ description: "搜索关键词" }),
			domain: Type.Optional(Type.String({ description: "限定域名，如 core、history" })),
			limit: Type.Optional(Type.Number({ description: "最多返回条数，默认 10" })),
			sort_by_world: Type.Optional(Type.Boolean({ description: "是否按世界时间排序" })),
			semantic: Type.Optional(Type.Boolean({ description: "是否启用语义检索" })),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "remember_memory",
		label: "记下记忆",
		description:
			"记下一段新的记忆。Events（需时间线追踪的事件）可传 time；Static（背景/性格/规则）无需传时间。",
		parameters: Type.Object({
			uri: Type.String({ description: "记忆 URI，如 core://identity" }),
			content: Type.String({ description: "记忆的具体内容" }),
			time: Type.Optional(Type.String({ description: "世界时间 YYYY-MM-DD 或相对位移如 -1d、+1y" })),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "set_world_time",
		label: "设置世界时间",
		description:
			"设置世界时间（按 namespace 隔离）。改变后该世界新记忆自动关联到新时间，查看记忆时更新 N 天前参考。GM 可用 character_id 指定目标世界（如 elias 调 magnolia），不影响其他世界。",
		parameters: Type.Object({
			time: Type.String({ description: "世界观日期（如 2024-06-05）或相对偏移（如 +1d）" }),
			character_id: Type.Optional(Type.String({ description: "目标角色 ID/namespace（GM 调其他世界时钟），留空用当前会话" })),
		}),
	},
	{
		name: "remember_child_memory",
		label: "记下子记忆",
		description:
			"把一段新记忆放在某个已有父节点下。when 是想起条件（外部信号/对话情境，写「当对方…/当你…」这类具体触发），必填。",
		parameters: Type.Object({
			parent_uri: Type.String({ description: "父节点 URI，如 core://" }),
			content: Type.String({ description: "记忆内容" }),
			importance: Type.Optional(Type.Number({ description: "重要性 0=最重要，5=普通，10=边角料" })),
			when: Type.String({ description: "什么时候会想起的外部信号" }),
			title: Type.Optional(Type.String({ description: "标题，仅字母数字连字符下划线" })),
			time: Type.Optional(Type.String({ description: "世界时间 YYYY-MM-DD 或相对位移" })),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "edit_memory",
		label: "编辑记忆",
		description:
			"修改一段记忆的内容。三种方式三选一：替换（old_text→new_text，old_text 须唯一）、追加（append）、行编辑（line+line_content）。也可只改 importance/when/time（time 传 \"\" 清除该记忆的世界时间）。",
		parameters: Type.Object({
			uri: Type.String({ description: "要修改的记忆 URI" }),
			old_text: Type.Optional(Type.String({ description: "[替换] 要改掉的原文" })),
			new_text: Type.Optional(Type.String({ description: "[替换] 改成什么" })),
			append: Type.Optional(Type.String({ description: "[追加] 追加到末尾的文字" })),
			line: Type.Optional(Type.Number({ description: "[行编辑] 行号（从 1 开始）" })),
			line_content: Type.Optional(Type.String({ description: "[行编辑] 该行新内容" })),
			importance: Type.Optional(Type.Number({ description: "修改重要性" })),
			when: Type.Optional(Type.String({ description: "修改想起条件" })),
			time: Type.Optional(Type.String({ description: "修改世界时间；传 \"\" 清除时间" })),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "forget_memory",
		label: "忘掉记忆",
		description:
			"忘掉一段记忆。删除前自动备份到 staging/。若该记忆有其他别名只拆这个入口；最后一个入口则记忆本身被删除。有子节点需先清理。",
		parameters: Type.Object({
			uri: Type.String({ description: "要删除的 URI，如 core://items/old_book" }),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "link_memory",
		label: "关联记忆",
		description:
			"同一条记忆多放一个入口（别名），不是复制。两个入口共享内容，改一个另一个也变，子节点自动继承。",
		parameters: Type.Object({
			target_uri: Type.String({ description: "已有的目标记忆 URI" }),
			new_uri: Type.String({ description: "新入口放哪" }),
			importance: Type.Number({ description: "从这个入口想起的重要性" }),
			when: Type.String({ description: "从这入口什么时候会想起来" }),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "tag_memory",
		label: "贴标签",
		description:
			"给一段记忆贴上触发词标签。标签与内容绑定（所有别名共享）。查看全部标签用 browse_memory('system://glossary')。",
		parameters: Type.Object({
			uri: Type.String({ description: "要贴标签的记忆 URI" }),
			add: Type.Optional(Type.Array(Type.String(), { description: "要加的标签词列表" })),
			remove: Type.Optional(Type.Array(Type.String(), { description: "要删的标签词列表" })),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "merge_memories",
		label: "合并记忆",
		description:
			"把多条记忆合并成一条。读取源记忆、用新内容创建目标、转移标签、删除源记忆（带备份）。",
		parameters: Type.Object({
			uris: Type.Array(Type.String(), { description: "要合并的源记忆 URI 列表（至少 2 条）" }),
			target_uri: Type.String({ description: "合并后放在哪" }),
			content: Type.String({ description: "合并后的完整内容" }),
			reason: Type.Optional(Type.String({ description: "合并原因（可选）" })),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "organize_memory",
		label: "整理记忆",
		description:
			"把几段相关记忆整理成一个主题。mode=move（默认）：建主题摘要+源记忆移到主题下+删旧入口；link：建主题+加主题入口+保留原位置；keep：只建主题不动源记忆。",
		parameters: Type.Object({
			target_uri: Type.String({ description: "主题放在哪" }),
			source_uris: Type.Array(Type.String(), { description: "要整理的相关记忆 URI" }),
			content: Type.String({ description: "主题总结" }),
			mode: Type.Optional(StringEnum(["move", "link", "keep"] as const, { description: "整理方式" })),
			importance: Type.Optional(Type.Number({ description: "主题重要性" })),
			when: Type.Optional(Type.String({ description: "什么时候想到这主题" })),
			tags: Type.Optional(Type.Array(Type.String(), { description: "主题标签词" })),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "rename_memory",
		label: "改名记忆",
		description:
			"给一段记忆改名字（路径最后一段）。内容和子节点都会跟着搬，标签不动。改名是「移动」的特例：只改名字、不换位置；若还想换域或换目录，用 move_memory。新名字只能用字母、数字、连字符和下划线。",
		parameters: Type.Object({
			uri: Type.String({ description: "要改名的记忆 URI，如 core://events/luckin_0922" }),
			new_title: Type.String({ description: "新名字（路径最后一段），仅字母数字连字符下划线" }),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "move_memory",
		label: "移动记忆",
		description:
			"把一段记忆（连同子节点）搬到新位置。可以跨域、可以改名。适合整理时把节点从一处迁到另一处——如把退役场景从 history 归档到 archive 域。目标目录不存在时会自动创建。",
		parameters: Type.Object({
			uri: Type.String({ description: "要移动的记忆 URI，如 history://scenes/warm_water_aftermath_0908_1836" }),
			target_uri: Type.String({ description: "目标位置的完整 URI，如 archive://scenes/warm_water_0908" }),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "batch_move_memories",
		label: "批量移动",
		description:
			"批量移动记忆。把一组记忆各自搬到新位置（可跨域、可改名），适合整批整理（如批量归档 history_raw → archive）。每条独立执行、单条失败不阻断其他条，目标目录不存在自动创建。dry_run=true 时只预览不落库。",
		parameters: Type.Object({
			moves: Type.Array(Type.Object({
				source_uri: Type.String({ description: "从哪里" }),
				target_uri: Type.String({ description: "到哪里" }),
			}), { description: "移动清单，每项 {source_uri, target_uri}" }),
			dry_run: Type.Optional(Type.Boolean({ description: "True 只预览（检查源/目标/冲突），不执行" })),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "batch_forget_memories",
		label: "批量删除",
		description:
			"批量忘掉记忆。一组 URI 一次清理，适合清创（退役/重复节点）。带防呆：子节点连坐、被同批更靠前的删除覆盖的自动跳过（按深度先删叶子）、单条失败不阻断。dry_run=true 只预览每条是否存在及会连坐哪些子节点。",
		parameters: Type.Object({
			uris: Type.Array(Type.String(), { description: "要删除的 URI 列表" }),
			dry_run: Type.Optional(Type.Boolean({ description: "True 只预览（是否存在、会连坐哪些子节点），不执行" })),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "batch_edit_memories",
		label: "批量编辑",
		description:
			"批量修改一组记忆的元数据或追加内容。适合全库重分级（importance 0-10）、批量改想起条件（when）、批量补/删时间（time 传 \"\" 批量清除）等维护操作。append 是往每条内容末尾追加同一段文字。至少提供一种修改。dry_run=true 只预览每条当前值 → 将改为什么。",
		parameters: Type.Object({
			uris: Type.Array(Type.String(), { description: "要修改的 URI 列表" }),
			importance: Type.Optional(Type.Number({ description: "新的重要性（0=最重要，数字越大越次要）" })),
			when: Type.Optional(Type.String({ description: "新的想起条件" })),
			append: Type.Optional(Type.String({ description: "追加到每条内容末尾的文字" })),
			time: Type.Optional(Type.String({ description: "新的世界时间 YYYY-MM-DD 或相对位移如 -1d；传 \"\" 批量清除" })),
			dry_run: Type.Optional(Type.Boolean({ description: "True 只预览当前值 → 将改为什么，不执行" })),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "archive_history",
		label: "存档记忆",
		description:
			"把刚才发生的事记录到 history 域，之后可用 system://wakeup 回想最近发生的事。注意：这是「记录新场景」，不是把旧记忆移进 archive 域（那是 move_memory）。GM 可用 character_id 指定目标世界，不影响其他世界。",
		parameters: Type.Object({
			title: Type.String({ description: "场景标题（仅字母数字下划线连字符）" }),
			history: Type.String({ description: "场景摘要，这段场景发生了什么（也可传文件路径）" }),
			mode: Type.Optional(StringEnum(["char", "gm"] as const, { description: "char=角色视角，gm=GM视角" })),
			raw: Type.Optional(Type.String({ description: "原始完整记录（可选，也可传文件路径）" })),
			time: Type.Optional(Type.String({ description: "世界时间 YYYY-MM-DD 或相对位移" })),
			character_id: Type.Optional(Type.String({ description: "目标角色 ID/namespace（GM 调其他世界），留空用当前会话" })),
		}),
	},
	{
		name: "recent_memories",
		label: "最近记忆",
		description: "看看最近发生了什么——列出最近新增或修改的记忆，按时间倒序。",
		parameters: Type.Object({
			limit: Type.Optional(Type.Number({ description: "最多显示条数，默认 10，最多 50" })),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
	{
		name: "boot_memory",
		label: "管理醒来记忆",
		description:
			"管理「醒来记忆」——每次重新进入世界时最先想起的记忆。action=list 查看，set 完全替换，add 追加，remove 移除。",
		parameters: Type.Object({
			action: StringEnum(["list", "set", "add", "remove"] as const, { description: "操作类型" }),
			uris: Type.Optional(Type.Array(Type.String(), { description: "set/add/remove 时操作的 URI 列表" })),
			character_id: Type.Optional(Type.String({ description: "角色 ID/namespace（记忆隔离），留空用当前会话" })),
		}),
	},
];

// ── Extension ───────────────────────────────────────────────────────────────

export default function nocturneMemoryToolsExtension(pi: ExtensionAPI): void {
	for (const tool of TOOLS) {
		pi.registerTool({
			name: tool.name,
			label: tool.label,
			description: tool.description,
			promptSnippet: tool.description.split("\n")[0],
			parameters: tool.parameters,
			async execute(_toolCallId, params, signal, _onUpdate) {
				const p = params as Record<string, unknown>;
				// GM 可指定目标 namespace（如 set_world_time 调整其他世界时钟）；
				// 仅在显式给出 character_id 时作为该次调用的 namespace，否则沿用当前会话。
				const targetNs =
					typeof p.character_id === "string" && p.character_id.trim()
						? p.character_id.trim()
						: undefined;
				const result = await invoke(tool.name, p, signal ?? undefined, targetNs);
				return {
					content: [{ type: "text", text: result.message }],
					details: result.data,
				};
			},
		});
	}
}
