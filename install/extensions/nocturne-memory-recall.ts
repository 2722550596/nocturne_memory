import type { BeforeAgentStartEvent, ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { DatabaseSync } from "node:sqlite";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";

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

// Paths come from nocturne-memory.config.json (NOCTURNE_MEMORY_DIR /
// NOCTURNE_PI_AGENT_DIR env overrides win). Empty => recall no-ops.
const MEMORY_DIR = process.env.NOCTURNE_MEMORY_DIR?.trim() || EXT_CFG.memoryDir || "";
const PI_AGENT_DIR = process.env.NOCTURNE_PI_AGENT_DIR?.trim() || EXT_CFG.piAgentDir || "";

if (!MEMORY_DIR) {
	console.error("[nocturne-recall] nocturne-memory.config.json missing or no memoryDir — recall disabled.");
}

// Extension id for persisted configuration (Settings.extensionSettings).
// Auto-recall defaults to ON; set "autoRecall": false via /recall off to disable.
const EXT_ID = "nocturne-recall";
const AUTO_RECALL_KEY = "autoRecall";

// Domain (uri scheme) blocklist for recall. These domains are operational
// noise (maintenance logs, raw session transcripts) that would pollute
// retrieval results, so they are excluded by default. The list is persisted
// under DOMAIN_BLOCKLIST_KEY and adjustable via `/recall domain add|remove`.
const DEFAULT_DOMAIN_BLOCKLIST = ["maintenance", "history_raw"];
const DOMAIN_BLOCKLIST_KEY = "domainBlocklist";

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
const EMBEDDING_API_KEY = process.env.NOCTURNE_EMBEDDING_API_KEY || EXT_CFG.embeddingApiKey || "";
// Official query instruction for bge-*-zh-v1.5 retrieval (BAAI README):
// prepend to SHORT QUERIES only, never to passages/documents.
const QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章：";

// Vector cache location (next to source DB, not in pi agent dir)
const CACHE_DIR = join(dirname(DB_PATH), "recall-cache");
const CACHE_PATH = join(CACHE_DIR, "embeddings.sqlite");

// Recall tuning
const TOP_K = 3;
const MIN_SCORE = 0.35;
// Anchor threshold: the top-ranked item only earns "高度相关，建议读取"
// when its absolute score clears this bar. Priority (max 0.15) plus recency
// (max 0.08) cannot reach it alone — semantic relevance must contribute.
const HIGH_CONFIDENCE = 0.55;
// doc-coverage keyword normalization alignment gain (see keywordScore).
const DOC_COVERAGE_GAIN = 1.4;
const MAX_SUMMARY_LEN = 80;

// BAAI/bge-large-zh-v1.5 max sequence is 512 tokens; ~1 zh char per token.
// Truncate embedding inputs so long memories don't 400 the whole batch.
// Long memories are chunked (see chunkText) with this overlap to keep
// sentence-level semantics intact across segment boundaries.
const EMBED_INPUT_MAX = 500;
const EMBED_CHUNK_OVERLAP = 80;

// Scoring weights (hybrid). Priority follows the graph's semantic:
// importance 0 = most important -> prio score 1.0; 10 = trivia -> 0.0.
const W_VECTOR = 0.55;
const W_KEYWORD = 0.3;
const W_PRIORITY = 0.15;

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

/** Extract the domain (uri scheme) from a memory uri: "core://a/b" -> "core".
 *  Uris without a scheme ("plain/path") return "" and never match a domain. */
function uriDomain(uri: string): string {
	const i = uri.indexOf("://");
	return i > 0 ? uri.slice(0, i) : "";
}

/** Read the recall domain blocklist. Falls back to the defaults when the
 *  setting is absent or malformed, so the maintenance/history_raw exclusion
 *  holds even on a fresh install. An explicitly persisted empty list means
 *  "no blocklist" (user removed every domain). */
function getDomainBlocklist(ctx: ExtensionContext): string[] {
	const cfg = ctx.getExtensionSetting<unknown>(EXT_ID, DOMAIN_BLOCKLIST_KEY);
	if (Array.isArray(cfg)) {
		return cfg.filter((d): d is string => typeof d === "string");
	}
	return [...DEFAULT_DOMAIN_BLOCKLIST];
}

/** Distinct domains actually present in the source DB (best-effort, for
 *  autocomplete candidates). Returns [] when the DB is unavailable. */
function listDomainsFromDb(): string[] {
	if (!existsSync(DB_PATH)) return [];
	try {
		const db = new DatabaseSync(DB_PATH, { readOnly: true });
		try {
			const rows = db
				.prepare(
					"SELECT DISTINCT substr(uri, 1, instr(uri, '://') - 1) AS domain FROM search_documents WHERE instr(uri, '://') > 1",
				)
				.all() as Array<{ domain: string }>;
			return rows.map((r) => r.domain).filter(Boolean);
		} finally {
			db.close();
		}
	} catch {
		return [];
	}
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

/** Flatten all line breaks and take the first MAX_SUMMARY_LEN chars, so an
 *  early line break (e.g. a short markdown title) cannot truncate the
 *  excerpt to just the heading. */
function summarize(content: string): string {
	const flat = content.replace(/\r?\n+/g, " ").trim();
	return flat.length > MAX_SUMMARY_LEN ? `${flat.slice(0, MAX_SUMMARY_LEN)}……` : flat;
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
	const docTokens = tokenizeForMatch(`${doc.uri} ${doc.disclosure} ${doc.searchTerms} ${doc.content}`);
	const docTokenSet = new Set(docTokens);
	let hits = 0;
	for (const t of queryTokens) if (docTokenSet.has(t)) hits++;
	// Two complementary normalizations, max wins:
	// - query-precision: share of query tokens the doc matches. Right metric
	//   for short prompts, but a long Prior-context query dilutes it.
	// - doc-coverage: share of doc tokens the query mentions. Catches "the
	//   context talks about this doc" regardless of context length. Saturates
	//   lower than query-precision (a doc is rarely >80% covered), so align
	//   scales with DOC_COVERAGE_GAIN before comparing.
	let covered = 0;
	for (const t of docTokenSet) if (queryTokens.includes(t)) covered++;
	const byQuery = hits / queryTokens.length;
	const byDoc = docTokens.length > 0 ? Math.min(1, (covered / docTokens.length) * DOC_COVERAGE_GAIN) : 0;
	return Math.max(byQuery, byDoc);
}

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
	/** Full content: the dedup hash is version-sensitive, so it must cover
	 *  the whole body, not just the first-line summary. */
	content: string;
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

function priorityScore(priority: number): number {
	// Graph semantic: 0 = most important, 10 = trivia (see remember_child_memory).
	// Map to 1.0..0.0 with clamping; out-of-range values never get a boost.
	const p = Math.min(Math.max(priority, 0), 10);
	return 1 - p / 10;
}

function recencyBoost(docTs: string | null, nowDays: number): number {
	const days = toEpochDays(docTs);
	if (days == null) return 0;
	const delta = nowDays - days;
	if (delta < 0) return 0.08; // future-dated entries get the recent-tier boost
	if (delta <= 7) return 0.08; // very recent world events
	if (delta <= 30) return 0.04;
	if (delta <= 90) return 0.02;
	return 0;
}

function cosine(a: Float32Array, b: Float32Array): number {
	let dot = 0;
	const len = Math.min(a.length, b.length);
	for (let i = 0; i < len; i++) dot += a[i] * b[i];
	return dot; // both normalized
}

async function recall(
	queries: string[],
	namespace: string,
	blockedDomains: string[],
): Promise<{ items: RecalledItem[]; mode: "vector" | "keyword" }> {
	// Multi-query recall: each query is scored independently and the per-doc
	// best wins. Query[0] is the current prompt (retrieval intent); any
	// following queries are conversation context (declarative text) — only
	// the intent query carries the BGE instruction.
	if (queries.length === 0) return { items: [], mode: "keyword" };
	// Open nocturne DB read-only
	if (!existsSync(DB_PATH)) return { items: [], mode: "keyword" };
	const db = new DatabaseSync(DB_PATH, { readOnly: true });
	try {
		// Drop blocked domains (maintenance logs, raw transcripts) before
		// anything else: they must not pollute scores, cache or TOP_K slots.
		const blockSet = new Set(blockedDomains);
		const docs = loadSearchDocuments(db, namespace).filter((d) => !blockSet.has(uriDomain(d.uri)));
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

		const queryTokensList = queries.map(tokenizeForMatch);

		// Vector scoring (with cache)
		const cache = new VectorCache();
		let mode: "vector" | "keyword" = "keyword";
		let vecScores = new Map<string, number>();
		let queryVecs: Float32Array[] = [];
		try {
			const cached = cache.loadValid(namespace, pool);
			const missing = pool.filter((d) => !cached.has(d.uri));
			// Chunk each doc into overlapping segments; short docs stay single.
			const segByUri = new Map<string, string[]>();
			for (const d of missing) {
				segByUri.set(
					d.uri,
					chunkText(`${d.uri}\n${d.disclosure}\n${d.searchTerms}\n${d.content}`, EMBED_INPUT_MAX, EMBED_CHUNK_OVERLAP),
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
			// One embed call per query; only the intent query gets the BGE
			// instruction. Context queries stay instruction-free (they are
			// declarative text, closer to the passage side).
			const embedInputs = queries.map((q, i) =>
				i === 0 ? `${QUERY_INSTRUCTION}${q.slice(0, EMBED_INPUT_MAX - QUERY_INSTRUCTION.length)}` : q.slice(0, EMBED_INPUT_MAX),
			);
			const qvs = await embed(embedInputs);
			queryVecs = qvs ?? [];
			if (queryVecs.length === queries.length) {
				mode = "vector";
				for (const d of pool) {
					const segs = cached.get(d.uri);
					if (!segs || segs.length === 0) continue;
					// Per query: segment-level cosine, take the max segment.
					// Across queries: take the best query — any query view
					// (intent or context) justifies recalling the memory.
					let best = 0;
					for (const queryVec of queryVecs) {
						for (const v of segs) {
							const c = cosine(queryVec, v);
							if (c > best) best = c;
						}
					}
					vecScores.set(d.uri, best);
				}
			}
		} catch {
			// fall through to keyword mode
		} finally {
			cache.close();
		}

		// Hybrid scoring — per-query keyword score, best query wins.
		const scored: RecalledItem[] = pool.map((doc) => {
			const vec = mode === "vector" ? (vecScores.get(doc.uri) ?? 0) : 0;
			let kw = 0;
			for (const qTokens of queryTokensList) {
				const k = keywordScore(qTokens, doc);
				if (k > kw) kw = k;
			}
			const prio = priorityScore(doc.priority);
			let score = W_VECTOR * vec + W_KEYWORD * kw + W_PRIORITY * prio;
			score += recencyBoost(doc.worldTimestamp, nowDays);
			return {
				uri: doc.uri,
				disclosure: doc.disclosure,
				summary: summarize(doc.content),
				content: doc.content,
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
		const anchor = i === 0 && item.score >= HIGH_CONFIDENCE ? " [高度相关，建议读取]" : "";
		lines.push(`\n${i + 1}. ${item.uri}${anchor}`);
		if (item.disclosure) lines.push(`   想起条件: ${item.disclosure}`);
		lines.push(`   摘要: ${item.summary}`);
	});
	lines.push(
		"\n如果你想起了什么，主动用 browse_memory 读取原文试试吧。",
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
	// Rebuilt from the session's ACTIVE tree path (buildContextEntries walks
	// from the current leaf, so entries abandoned by /tree rollback do not
	// count as injected), restricted to what is actually in the LLM context:
	// the compaction-aware entry list. Rebuilt on every prompt so /tree,
	// /resume, /fork and compaction all stay consistent; the walk is cheap
	// (O(entries on path)) and recall() already dwarfs it. The framework
	// persists each returned message to the session before the next prompt,
	// so the session is the single source of truth for dedup state.
	const injected = new Map<string, string>();

	function rebuildInjectedFromSession(ctx: ExtensionContext): void {
		injected.clear();
		const entries = ctx.sessionManager.buildContextEntries();
		for (const e of entries) {
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

	pi.on("session_tree", (_event, ctx) => {
		// Leaf moved (/tree navigation, rollback): the active path changed,
		// so the injected set must be rebuilt from the new path.
		rebuildInjectedFromSession(ctx);
	});

	pi.on("session_compact", (_event, ctx) => {
		// Compaction may keep recent injections in context (firstKeptEntryId);
		// rebuild instead of clearing so kept entries stay deduped.
		rebuildInjectedFromSession(ctx);
	});

	pi.on("before_agent_start", async (event: BeforeAgentStartEvent, ctx: ExtensionContext) => {
		// Rebuild on every prompt: the active path may have changed via
		// /tree, /resume, /fork or compaction between prompts. This also
		// covers --resume paths that never fire session_start.
		rebuildInjectedFromSession(ctx);

		// Skip slash-command expansions and empty prompts
		const prompt = event.prompt?.trim();
		if (!prompt || prompt.startsWith("/") || prompt.startsWith("\\")) return;

		// Auto-recall switch: read persisted extension setting (default ON).
		// Off -> skip retrieval entirely; the /recall command toggles it.
		const autoRecall = ctx.getExtensionSetting<boolean>(EXT_ID, AUTO_RECALL_KEY);
		if (autoRecall === false) return;

		const namespace = detectNamespace();
		if (!namespace) return;

		// Dual-query recall: [current prompt, conversation context]. The
		// context view uses recent message turns so referential/elliptical
		// prompts ("然后呢？", "里面有什么？") can still retrieve. Message
		// entries carry our own rp-memories injections as plain text — those
		// must not retrieve memories (no self-excitation), so they are
		// identified by the <memories> block and skipped.
		const queries: string[] = [prompt];
		try {
			const entries = ctx.sessionManager.buildContextEntries();
			const msgs: string[] = [];
			for (let i = entries.length - 1; i >= 0 && msgs.length < 6; i--) {
				const e = entries[i] as { type?: string; message?: { role?: string; content?: unknown } };
				if (e.type !== "message" || !e.message || typeof e.message.role !== "string") continue;
				const content = e.message.content;
				const text =
					typeof content === "string"
						? content
						: Array.isArray(content)
							? content
									.map((b: { text?: string }) => (typeof b?.text === "string" ? b.text : ""))
									.join(" ")
									.trim()
							: "";
				if (!text || text.includes("<memories>")) continue;
				msgs.unshift(`${e.message.role}: ${text}`);
			}
			if (msgs.length > 0) queries.push(`Prior context:\n${msgs.join("\n")}`);
		} catch {
			// No usable history (fresh session / harness): prompt-only recall.
		}

		const { items, mode } = await recall(queries, namespace, getDomainBlocklist(ctx));
		if (items.length === 0) return;

		// Dedup against previously injected (same content version). The hash
		// covers the FULL body: a content edit must invalidate the marker so
		// the updated memory gets re-injected.
		const fresh: RecalledItem[] = [];
		const hashes: Record<string, string> = {};
		for (const it of items) {
			const hash = md5(`${it.uri}|${it.content}`);
			if (injected.get(it.uri) === hash) continue;
			fresh.push(it);
			hashes[it.uri] = hash;
		}
		if (fresh.length === 0) return;

		for (const it of fresh) injected.set(it.uri, hashes[it.uri]);

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
	// Slash command to control auto-recall: /recall on | off | stat
	// Toggles persist to Settings.extensionSettings["nocturne-recall"]["autoRecall"].
	pi.registerCommand("recall", {
		description:
			"Nocturne memory auto-recall control: /recall on | off | stat | domain list|add|remove <域名> (默认 on)",
		getArgumentCompletions: (prefix, ctx) => {
			// Split the RAW prefix: a trailing space yields a trailing empty
			// token, which marks "the next argument slot is empty" vs. a typed
			// partial (e.g. "domain add " vs "domain add h"). trim() would
			// erase that distinction.
			const raw = prefix;
			const parts = raw.split(/\s+/);
			const last = parts[parts.length - 1] ?? "";
			const cur = raw.endsWith(" ") ? "" : last;
			const filter = (items: string[]) =>
				items.filter((i) => i.startsWith(cur)).map((i) => ({ value: i, label: i }));
			// Argument 1 (no subcommand typed yet): exactly one token so far.
			if (parts.length === 1) {
				return filter(["on", "off", "stat", "domain"]);
			}
			if (parts[0] === "domain") {
				const action = parts[1] ?? "";
				// Argument 2: domain action. Complete actions whenever the
				// second token is NOT already a full action — whether it is
				// empty ("domain "), a bare prefix ("domain a") or a typo
				// ("domain addx") all complete to list/add/remove.
				if (action !== "add" && action !== "remove" && action !== "list") {
					return filter(["list", "add", "remove"]);
				}
				// Argument 3: domain name for add/remove.
				if (action === "add") {
					const blocked = new Set(getDomainBlocklist(ctx));
					return filter(listDomainsFromDb().filter((d) => !blocked.has(d)));
				}
				if (action === "remove") {
					return filter(getDomainBlocklist(ctx));
				}
				return null; // "domain list" takes no third argument
			}
			return null;
		},
		handler: async (args: string, ctx) => {
			const parts = args.trim().split(/\s+/).filter(Boolean);
			const sub = parts[0] ?? "";
			const state = (): boolean =>
				ctx.getExtensionSetting<boolean>(EXT_ID, AUTO_RECALL_KEY) !== false;
			const blocklist = (): string[] => getDomainBlocklist(ctx);
			if (sub === "on") {
				ctx.setExtensionSetting(EXT_ID, AUTO_RECALL_KEY, true);
				ctx.ui.notify("Nocturne auto-recall 已开启", "info");
			} else if (sub === "off") {
				ctx.setExtensionSetting(EXT_ID, AUTO_RECALL_KEY, false);
				ctx.ui.notify("Nocturne auto-recall 已关闭", "info");
			} else if (sub === "domain") {
				const action = parts[1] ?? "list";
				const domain = parts[2];
				if (action === "add" && domain) {
					const next = [...new Set([...blocklist(), domain])];
					ctx.setExtensionSetting(EXT_ID, DOMAIN_BLOCKLIST_KEY, next);
					ctx.ui.notify(`已加入域名黑名单: ${domain}（当前 ${next.join(", ") || "无"}）`, "info");
				} else if (action === "remove" && domain) {
					const next = blocklist().filter((d) => d !== domain);
					ctx.setExtensionSetting(EXT_ID, DOMAIN_BLOCKLIST_KEY, next);
					ctx.ui.notify(`已移出域名黑名单: ${domain}（当前 ${next.join(", ") || "无"}）`, "info");
				} else if (action === "list" || !domain) {
					ctx.ui.notify(`域名黑名单: ${blocklist().join(", ") || "无"}`, "info");
				} else {
					ctx.ui.notify("用法: /recall domain list | add <域名> | remove <域名>", "error");
				}
			} else if (sub === "stat") {
				const ns = detectNamespace();
				pi.appendEntry("nocturne_recall_stat", { namespace: ns, injected: [...injected.keys()] });
				ctx.ui.notify(
					`Nocturne auto-recall: ${state() ? "开启" : "关闭"} (namespace=${ns || "无"}, 已注入 ${injected.size} 条, 域名黑名单: ${blocklist().join(", ") || "无"})`,
					"info",
				);
			} else {
				ctx.ui.notify(
					`用法: /recall on | off | stat | domain list|add|remove <域名> （当前 ${state() ? "开启" : "关闭"}）`,
					"error",
				);
			}
		},
	});
}
