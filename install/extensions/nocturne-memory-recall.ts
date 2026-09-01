import type { BeforeAgentStartEvent, ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { DatabaseSync } from "node:sqlite";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";

// ── Config ──────────────────────────────────────────────────────────────────

// The installation script will replace these placeholders with actual paths.
const MEMORY_DIR = "{{MEMORY_DIR}}";
const PI_AGENT_DIR = "{{PI_AGENT_DIR}}";

const CONFIG_PATH = join(MEMORY_DIR, "config.json");

// Resolve the source DB from config.json (database_url) instead of hardcoding
// a file name, so recall stays in sync with whichever DB the MCP servers are
// actually using. Falls back to the historical default when unreadable.
function resolveDbPath(): string {
	try {
		const cfg = JSON.parse(readFileSync(CONFIG_PATH, "utf-8"));
		const url = String(cfg?.database_url ?? "");
		const m = url.match(/sqlite\+aiosqlite:\/\/\/(.+)$/);
		if (m) return m[1];
	} catch {
		// fall through to default
	}
	return join(MEMORY_DIR, "data", "nocturne_data_fc852c.db");
}
const DB_PATH = resolveDbPath();

// Embedding API. The key MUST come from the environment (NOCTURNE_EMBEDDING_API_KEY) —
// this repo is public, never hardcode credentials. When the key is missing,
// embed() returns null and recall degrades to keyword-only mode.
const EMBEDDING_MODEL = "BAAI/bge-large-zh-v1.5";
const EMBEDDING_API_URL = "https://api.siliconflow.cn/v1";
const EMBEDDING_API_KEY = process.env.NOCTURNE_EMBEDDING_API_KEY ?? "";

// Vector cache location (next to source DB, not in pi agent dir)
const CACHE_DIR = join(dirname(DB_PATH), "recall-cache");
const CACHE_PATH = join(CACHE_DIR, "embeddings.sqlite");

// Recall tuning
const TOP_K = 3;
const MIN_SCORE = 0.35;
const MAX_SUMMARY_LEN = 80;

// BAAI/bge-large-zh-v1.5 max sequence is 512 tokens; ~1 zh char per token.
// Truncate embedding inputs so long memories don't 400 the whole batch.
// Long memories are chunked (see chunkText) with this overlap to keep
// sentence-level semantics intact across segment boundaries.
const EMBED_INPUT_MAX = 500;
const EMBED_CHUNK_OVERLAP = 80;

// Scoring weights (aligned with omp mnemopi hybrid recall)
const W_VECTOR = 0.5;
const W_KEYWORD = 0.3;
const W_PRIORITY = 0.2;

interface SearchDoc {
	uri: string;
	content: string;
	disclosure: string;
	searchTerms: string;
	priority: number;
	worldTimestamp: string | null;
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function md5(text: string): string {
	return createHash("md5").update(text, "utf-8").digest("hex");
}

function readNamespaceFromMcp(filePath: string): string | null {
	try {
		const raw = JSON.parse(readFileSync(filePath, "utf-8"));
		const servers = raw?.mcpServers ?? {};
		const names = Object.keys(servers);
		// Prefer the unnamed server (tools registered without prefix)
		const un = servers[""];
		if (un?.env?.NAMESPACE) return un.env.NAMESPACE;
		// A single named server is unambiguous (e.g. meta with "nocturne")
		if (names.length === 1 && servers[names[0]]?.env?.NAMESPACE) {
			return servers[names[0]].env.NAMESPACE;
		}
		// Multiple named servers (e.g. magnolia elias+mingrui): cannot
		// auto-pick — the caller must set NOCTURNE_NAMESPACE explicitly.
		return null;
	} catch {
		return null;
	}
}

function getProjectConfigDirName(): string {
	return process.env.PI_PROJECT_CONFIG_DIR?.trim() || ".pi";
}

function detectNamespace(): string {
	// 1. Explicit per-process override — required for multi-server worlds
	//    (magnolia: elias + mingrui) and always wins.
	const explicit = process.env.NOCTURNE_NAMESPACE?.trim();
	if (explicit) return explicit;

	// 2. Project-level configs (cwd), matching the pi config discovery order:
	//    .pi/mcp.json then .mcp.json. Authoritative for multi-role setups.
	const cwd = process.cwd();
	const projectCandidates = [
		join(cwd, getProjectConfigDirName(), "mcp.json"),
		join(cwd, ".mcp.json"),
	];
	for (const p of projectCandidates) {
		if (existsSync(p)) {
			const ns = readNamespaceFromMcp(p);
			if (ns) return ns;
		}
	}

	// 3. Global pi mcp.json (legacy single-role setups)
	const globalNs = readNamespaceFromMcp(join(PI_AGENT_DIR, "mcp.json"));
	if (globalNs) return globalNs;

	// 4. No usable namespace: return "" so recall degrades to a no-op instead
	//    of guessing (the old database-hash fallback was never a namespace).
	return "";
}

function loadBootUrisSync(db: DatabaseSync, namespace: string): Set<string> {
	const uris = new Set<string>();
	try {
		const row = db
			.prepare("SELECT boot_uris FROM presets WHERE is_active = 1")
			.get() as { boot_uris: string } | undefined;
		if (row?.boot_uris) {
			const map = JSON.parse(row.boot_uris) as Record<string, string[]>;
			const list = map[namespace] ?? map[""] ?? [];
			for (const uri of list) uris.add(uri);
		}
	} catch {}
	return uris;
}

function loadSearchDocuments(db: DatabaseSync, namespace: string): SearchDoc[] {
	return db
		.prepare(
			"SELECT uri, content, disclosure, search_terms, priority, world_timestamp FROM search_documents WHERE namespace = ?",
		)
		.all(namespace)
		.map((r: Record<string, unknown>) => ({
			uri: String(r.uri),
			content: String(r.content ?? ""),
			disclosure: String(r.disclosure ?? ""),
			searchTerms: String(r.search_terms ?? ""),
			priority: Number(r.priority ?? 0),
			worldTimestamp: r.world_timestamp ? String(r.world_timestamp) : null,
		}));
}

function firstLineSummary(content: string): string {
	const first = content
		.split(/\n+/)
		.map((s) => s.trim())
		.find((s) => s.length > 0) ?? "";
	return first.length > MAX_SUMMARY_LEN ? `${first.slice(0, MAX_SUMMARY_LEN)}……` : first;
}

// Chunk text into overlapping segments of EMBED_INPUT_MAX chars. Overlap keeps
// semantic units (sentences) intact across chunk boundaries. Returns [""] for
// empty input so callers always get >=1 segment.
function chunkText(text: string, maxLen: number, overlap: number): string[] {
	if (text.length === 0) return [""];
	if (text.length <= maxLen) return [text];
	const chunks: string[] = [];
	const step = maxLen - overlap;
	for (let start = 0; start < text.length; start += step) {
		chunks.push(text.slice(start, start + maxLen));
	}
	return chunks;
}

function nowMs(): number {
	return Date.now();
}

// ── Embedding client (with FTS fallback) ────────────────────────────────────

async function embed(texts: string[]): Promise<Float32Array[] | null> {
	if (texts.length === 0) return [];
	// No API key configured -> keyword fallback, skip the doomed request.
	if (!EMBEDDING_API_KEY) return null;
	// Batch limit 32 for siliconflow
	const vectors: Float32Array[] = [];
	for (let i = 0; i < texts.length; i += 32) {
		const batch = texts.slice(i, i + 32);
		try {
			const res = await fetch(`${EMBEDDING_API_URL}/embeddings`, {
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					Authorization: `Bearer ${EMBEDDING_API_KEY}`,
				},
				body: JSON.stringify({ model: EMBEDDING_MODEL, input: batch }),
				signal: AbortSignal.timeout(30000),
			});
			if (!res.ok) return null;
			const json = (await res.json()) as { data?: Array<{ embedding: number[] }> };
			if (!json?.data || json.data.length !== batch.length) return null;
			for (const d of json.data) {
				const v = new Float32Array(d.embedding);
				// normalize
				let norm = 0;
				for (const x of v) norm += x * x;
				norm = Math.sqrt(norm);
				if (norm > 0) for (let j = 0; j < v.length; j++) v[j] /= norm;
				vectors.push(v);
			}
		} catch {
			return null;
		}
	}
	return vectors;
}

// ── Keyword scoring (FTS fallback + always-on term overlap) ─────────────────

function tokenizeForMatch(text: string): string[] {
	// CJK bigrams + latin words - works well enough as keyword scoring
	const tokens = new Set<string>();
	const latin = text.toLowerCase().match(/[a-z0-9]+/g) ?? [];
	for (const w of latin) tokens.add(w);
	const cjk = text.match(/[\u4e00-\u9fff]/g) ?? [];
	for (let i = 0; i < cjk.length - 1; i++) tokens.add(cjk[i] + cjk[i + 1]);
	if (cjk.length === 1) tokens.add(cjk[0]);
	return [...tokens];
}

function keywordScore(queryTokens: string[], doc: SearchDoc): number {
	if (queryTokens.length === 0) return 0;
	const docTokens = new Set(tokenizeForMatch(`${doc.uri} ${doc.disclosure} ${doc.content}`));
	let hits = 0;
	for (const t of queryTokens) if (docTokens.has(t)) hits++;
	return hits / queryTokens.length;
}

// ── Vector cache (node:sqlite) ─────────────────────────────────────────────

class VectorCache {
	private db: DatabaseSync | null = null;

	open(): void {
		if (this.db) return;
		mkdirSync(CACHE_DIR, { recursive: true });
		this.db = new DatabaseSync(CACHE_PATH);
		this.db.exec(`
			CREATE TABLE IF NOT EXISTS embeddings (
				uri TEXT NOT NULL,
				seg_index INTEGER NOT NULL,
				content_hash TEXT NOT NULL,
				vector TEXT NOT NULL,
				priority INTEGER NOT NULL DEFAULT 0,
				world_timestamp TEXT,
				updated_at INTEGER NOT NULL,
				PRIMARY KEY (uri, seg_index)
			)
		`);
		// Schema migration: the pre-chunking layout had a single vector per
		// uri (no seg_index). The cache is a pure acceleration layer - drop and
		// rebuild when the shape doesn't match, so stale rows never poison
		// segment lookups.
		const cols = this.db.prepare("PRAGMA table_info(embeddings)").all() as unknown as Array<{
			name: string;
		}>;
		if (!cols.some((c) => c.name === "seg_index")) {
			this.db.exec("DROP TABLE embeddings");
			this.db.exec(`
				CREATE TABLE embeddings (
					uri TEXT NOT NULL,
					seg_index INTEGER NOT NULL,
					content_hash TEXT NOT NULL,
					vector TEXT NOT NULL,
					priority INTEGER NOT NULL DEFAULT 0,
					world_timestamp TEXT,
					updated_at INTEGER NOT NULL,
					PRIMARY KEY (uri, seg_index)
				)
			`);
		}
	}

	private get _db(): DatabaseSync {
		this.open();
		return this.db!;
	}

	/** Load cached segment vectors for unchanged docs. Returns {uri -> segments[]}. */
	loadValid(namespace: string, docs: SearchDoc[]): Map<string, Float32Array[]> {
		const map = new Map<string, Float32Array[]>();
		try {
			const rows = this._db
				.prepare("SELECT uri, seg_index, content_hash, vector FROM embeddings ORDER BY uri, seg_index")
				.all() as unknown as Array<{
				uri: string;
				seg_index: number;
				content_hash: string;
				vector: string;
			}>;
			const docHashes = new Map(docs.map((d) => [d.uri, md5(`${d.content}|${d.searchTerms}`)]));
			for (const row of rows) {
				const hash = docHashes.get(row.uri);
				if (!hash || hash !== row.content_hash) continue; // stale version
				try {
					const v = Float32Array.from(JSON.parse(row.vector) as number[]);
					const list = map.get(row.uri);
					if (list) list[row.seg_index] = v;
					else map.set(row.uri, [v]);
				} catch {}
			}
			return map;
		} catch {
			return map;
		}
	}

	/**
	 * Persist segment vectors for docs. `segments` maps uri -> segment texts,
	 * `vectors` parallel to the flattened segment list.
	 */
	save(docs: SearchDoc[], segments: Map<string, string[]>, vectors: Float32Array[]): void {
		if (!this.db) return;
		const stmt = this._db.prepare(
			`INSERT INTO embeddings (uri, seg_index, content_hash, vector, priority, world_timestamp, updated_at)
			 VALUES (?, ?, ?, ?, ?, ?, ?)
			 ON CONFLICT(uri, seg_index) DO UPDATE SET content_hash=excluded.content_hash, vector=excluded.vector, priority=excluded.priority, world_timestamp=excluded.world_timestamp, updated_at=excluded.updated_at`,
		);
		const seen = new Set<string>(docs.map((d) => d.uri));
		this._db.exec("BEGIN");
		try {
			let vi = 0;
			for (const doc of docs) {
				const segs = segments.get(doc.uri) ?? [];
				for (let si = 0; si < segs.length; si++) {
					stmt.run(
						doc.uri,
						si,
						md5(`${doc.content}|${doc.searchTerms}`),
						JSON.stringify([...vectors[vi]]),
						doc.priority,
						doc.worldTimestamp,
						nowMs(),
					);
					vi++;
				}
			}
			// purge stale rows for this namespace's docs no longer present
			const all = this._db.prepare("SELECT uri FROM embeddings").all() as unknown as Array<{
				uri: string;
			}>;
			const del = this._db.prepare("DELETE FROM embeddings WHERE uri = ?");
			for (const { uri } of all) {
				if (!seen.has(uri)) del.run(uri);
			}
			this._db.exec("COMMIT");
		} catch {
			this._db.exec("ROLLBACK");
		}
	}

	close(): void {
		this.db?.close();
		this.db = null;
	}
}

// ── Recall engine ───────────────────────────────────────────────────────────

interface RecalledItem {
	uri: string;
	disclosure: string;
	summary: string;
	score: number;
	kw: number;
}

interface WorldClock {
	current_time: string | null;
}

function loadWorldClock(): WorldClock {
	// Per-process env overrides (multi-role: each role process injects its
	// own clock via the project-level .pi/mcp.json env block).
	const envEnabled = process.env.WORLD_CLOCK_ENABLED?.trim();
	if (envEnabled && ["false", "0", "no"].includes(envEnabled.toLowerCase())) {
		// Real-clock mode: recency is measured against today.
		return { current_time: new Date().toISOString().slice(0, 10) };
	}
	const envTime = process.env.WORLD_CLOCK_CURRENT_TIME?.trim();
	if (envTime) return { current_time: envTime };
	// Fall back to the file-level clock (shared config.json).
	try {
		const cfg = JSON.parse(readFileSync(CONFIG_PATH, "utf-8"));
		const clock = cfg?.world_clock ?? {};
		if (clock.enabled === false) {
			return { current_time: new Date().toISOString().slice(0, 10) };
		}
		return { current_time: clock.current_time ?? null };
	} catch {
		return { current_time: null };
	}
}

/** Parse "YYYY-MM-DD" to epoch days; null if absent/unparseable. */
function toEpochDays(ts: string | null): number | null {
	if (!ts) return null;
	const m = ts.match(/^(\d{4})-(\d{2})-(\d{2})/);
	if (!m) return null;
	const ms = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
	return Math.floor(ms / 86400000);
}

function recencyBoost(docTs: string | null, nowDays: number): number {
	const days = toEpochDays(docTs);
	if (days == null) return 0;
	const delta = nowDays - days;
	if (delta < 0) return 0.05; // future-dated entries get a tiny boost
	if (delta <= 7) return 0.05; // very recent world events
	if (delta <= 30) return 0.02;
	return 0;
}

function cosine(a: Float32Array, b: Float32Array): number {
	let dot = 0;
	const len = Math.min(a.length, b.length);
	for (let i = 0; i < len; i++) dot += a[i] * b[i];
	return dot; // both normalized
}

async function recall(
	query: string,
	namespace: string,
): Promise<{ items: RecalledItem[]; mode: "vector" | "keyword" }> {
	// Open nocturne DB read-only
	if (!existsSync(DB_PATH)) return { items: [], mode: "keyword" };
	const db = new DatabaseSync(DB_PATH, { readOnly: true });
	try {
		const docs = loadSearchDocuments(db, namespace);
		if (docs.length === 0) return { items: [], mode: "keyword" };
		const bootUris = loadBootUrisSync(db, namespace);
		const clock = loadWorldClock();
		const nowDays = toEpochDays(clock.current_time ?? new Date().toISOString().slice(0, 10)) ?? 0;

		// Deduplicate docs by uri (keep latest - order by rowid)
		const byUri = new Map<string, SearchDoc>();
		for (const d of docs) {
			if (bootUris.has(d.uri)) continue; // boot memories already in preset slot
			if (!byUri.has(d.uri)) byUri.set(d.uri, d);
		}
		const pool = [...byUri.values()];

		const queryTokens = tokenizeForMatch(query);

		// Vector scoring (with cache)
		const cache = new VectorCache();
		let mode: "vector" | "keyword" = "keyword";
		let vecScores = new Map<string, number>();
		let queryVec: Float32Array | null = null;
		try {
			const cached = cache.loadValid(namespace, pool);
			const missing = pool.filter((d) => !cached.has(d.uri));
			// Chunk each doc into overlapping segments; short docs stay single.
			const segByUri = new Map<string, string[]>();
			for (const d of missing) {
				segByUri.set(
					d.uri,
					chunkText(`${d.uri}\n${d.disclosure}\n${d.content}`, EMBED_INPUT_MAX, EMBED_CHUNK_OVERLAP),
				);
			}
			const segTexts = [...segByUri.values()].flat();
			if (segTexts.length > 0) {
				const vectors = await embed(segTexts);
				if (vectors) {
					cache.save(missing, segByUri, vectors);
					let vi = 0;
					for (const d of missing) {
						const n = (segByUri.get(d.uri) ?? []).length;
						const segs = vectors.slice(vi, vi + n);
						vi += n;
						cached.set(d.uri, segs);
					}
				}
			}
			const qv = await embed([query.slice(0, EMBED_INPUT_MAX)]);
			if (qv && qv.length === 1) queryVec = qv[0];
			if (queryVec) {
				mode = "vector";
				for (const d of pool) {
					const segs = cached.get(d.uri);
					if (!segs || segs.length === 0) continue;
					// Segment-level cosine, take the max: any strongly matching
					// segment justifies recalling the memory.
					let best = 0;
					for (const v of segs) {
						const c = cosine(queryVec, v);
						if (c > best) best = c;
					}
					vecScores.set(d.uri, best);
				}
			}
		} catch {
			// fall through to keyword mode
		} finally {
			cache.close();
		}

		// Hybrid scoring
		const maxPriority = Math.max(...pool.map((d) => d.priority), 1);
		const scored: RecalledItem[] = pool.map((doc) => {
			const vec = mode === "vector" ? (vecScores.get(doc.uri) ?? 0) : 0;
			const kw = keywordScore(queryTokens, doc);
			const prio = doc.priority / maxPriority;
			let score = W_VECTOR * vec + W_KEYWORD * kw + W_PRIORITY * prio;
			score += recencyBoost(doc.worldTimestamp, nowDays);
			return {
				uri: doc.uri,
				disclosure: doc.disclosure,
				summary: firstLineSummary(doc.content),
				score,
				kw,
			};
		});

		scored.sort((a, b) => b.score - a.score);
		const items = scored
			.filter((s) => s.score >= MIN_SCORE)
			// Keyword mode: require at least one query-term hit, otherwise the
			// score can be gamed by priority alone (no semantic signal).
			.filter((s) => mode === "vector" || s.kw > 0)
			.slice(0, TOP_K);
		return { items, mode };
	} finally {
		db.close();
	}
}

// ── Injection format ─────────────────────────¯¯¯¯¯¯¯¯───────────────────────

function buildMemoriesBlock(items: RecalledItem[]): string {
	const lines: string[] = ["<memories>"];
	items.forEach((item, i) => {
		const anchor = i === 0 ? " [高度相关，建议读取]" : "";
		lines.push(`\n${i + 1}. ${item.uri}${anchor}`);
		if (item.disclosure) lines.push(`   想起条件: ${item.disclosure}`);
		lines.push(`   摘要: ${item.summary}`);
	});
	lines.push(
		"\n当某条想起条件与当前对话明显相关时，应当主动 browse_memory 读取原文。",
		"</memories>",
	);
	return lines.join("\n");
}

// ── Extension ───────────────────────────────────────────────────────────────

export default function nocturneMemoryRecallExtension(pi: ExtensionAPI): void {
	// custom type policy: LLM-visible, TUI-hidden, excluded from compaction summary
	pi.registerCustomType("rp-memories", {
		context: "include",
		llmRole: "user",
		compaction: "exclude",
	});

	// Session-level dedup: uri -> content hash of the last injected version.
	// Survives process restarts by rebuilding from session entries (only the
	// stretch after the last compaction - earlier injections are out of
	// context and may be re-injected).
	const injected = new Map<string, string>();

	function rebuildInjectedFromSession(ctx: ExtensionContext): void {
		injected.clear();
		const entries = ctx.sessionManager.getEntries();
		// Skip everything up to and including the last compaction: those
		// injections are no longer in the LLM context.
		let start = 0;
		for (let i = entries.length - 1; i >= 0; i--) {
			if (entries[i].type === "compaction") {
				start = i + 1;
				break;
			}
		}
		for (let i = start; i < entries.length; i++) {
			const e = entries[i];
			if (e.type !== "custom_message" || e.customType !== "rp-memories") continue;
			const details = e.details as { ids?: string[]; hashes?: Record<string, string> } | undefined;
			if (details?.hashes) {
				for (const uri of Object.keys(details.hashes)) {
					injected.set(uri, details.hashes[uri]);
				}
			} else if (details?.ids) {
				// Legacy entries without per-uri hashes: mark as injected with
				// a uri-only key so they are not re-injected verbatim.
				for (const uri of details.ids) injected.set(uri, `legacy:${uri}`);
			}
		}
	}

	pi.on("session_start", (_event, ctx) => {
		rebuildInjectedFromSession(ctx);
	});

	pi.on("session_compact", () => {
		injected.clear();
	});

	pi.on("before_agent_start", async (event: BeforeAgentStartEvent, ctx: ExtensionContext) => {
		// Rebuild once per session on first prompt (covers --resume/--fork
		// paths that may not fire session_start before agent starts).
		if (!injectedReady) {
			rebuildInjectedFromSession(ctx);
			injectedReady = true;
		}

		// Skip slash-command expansions and empty prompts
		const prompt = event.prompt?.trim();
		if (!prompt || prompt.startsWith("/") || prompt.startsWith("\\")) return;

		const namespace = detectNamespace();
		if (!namespace) return;

		const { items, mode } = await recall(prompt, namespace);
		if (items.length === 0) return;

		// Dedup against previously injected (same content version)
		const fresh: RecalledItem[] = [];
		const hashes: Record<string, string> = {};
		for (const it of items) {
			const hash = md5(`${it.uri}|${it.summary}`);
			if (injected.get(it.uri) === hash) continue;
			fresh.push(it);
			hashes[it.uri] = hash;
		}
		if (fresh.length === 0) return;

		for (const it of fresh) injected.set(it.uri, md5(`${it.uri}|${it.summary}`));

		const content = buildMemoriesBlock(fresh);

		return {
			message: {
				customType: "rp-memories",
				content,
				display: false,
				details: { ids: fresh.map((i) => i.uri), hashes, mode },
			},
		};
	});

	// Optional manual command: /memories stat
	pi.registerCommand("memories", {
		description: "Nocturne memory recall control (stat)",
		handler: async (args: string) => {
			const sub = args.trim().split(/\s+/)[0] ?? "";
			if (sub === "stat") {
				const ns = detectNamespace();
				pi.appendEntry("nocturne_recall_stat", { namespace: ns, injected: [...injected.keys()] });
			}
		},
	});

	let injectedReady = false;
}
