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

// Query-vector cache TTL. Re-used prompts (/pi, /reroll, /tree + resend) hit
// this cache and skip the embed API call entirely. A query's embedding is a
// pure function of its text for a fixed model, so the only reason to expire
// is to bound cache growth and flush vectors from a retired model. 7 days
// covers same-day rerolls and any model swap within a week.
const QUERY_CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000;

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
		// Query-vector cache: one row per exact query string (md5 key). This is
		// the re-embed fast path for repeated prompts; unlike doc vectors it
		// needs no content_hash (the query text IS the key) — TTL handles aging.
		this.db.exec(`
			CREATE TABLE IF NOT EXISTS query_embeddings (
				query TEXT NOT NULL,
				query_hash TEXT PRIMARY KEY,
				vector TEXT NOT NULL,
				updated_at INTEGER NOT NULL
			)
		`);
		this.db
			.prepare("DELETE FROM query_embeddings WHERE updated_at < ?")
			.run(nowMs() - QUERY_CACHE_TTL_MS);
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

	/** Join cached query vectors for the given query strings, in order.
	 *  Missing/expired entries are null so callers only embed the gaps. */
	getQueryVectors(queries: string[]): (Float32Array | null)[] {
		const out: (Float32Array | null)[] = new Array(queries.length).fill(null);
		if (queries.length === 0) return out;
		try {
			const hashes = queries.map(md5);
			const stmt = this._db.prepare(
				"SELECT query_hash, vector FROM query_embeddings WHERE query_hash = ? AND updated_at >= ?",
			);
			// Pruned once per open() (see TTL cleanup there); belt-and-suspenders
			// freshness check keeps an aged row (opened pre-cleanup) out of use.
			const cutoff = nowMs() - QUERY_CACHE_TTL_MS;
			for (let i = 0; i < queries.length; i++) {
				const row = stmt.get(hashes[i], cutoff) as
					| { vector: string }
					| undefined;
				if (row) {
					try {
						out[i] = Float32Array.from(JSON.parse(row.vector) as number[]);
					} catch {
						out[i] = null;
					}
				}
			}
		} catch {
			// cache read failure => treat as all-miss
		}
		return out;
	}

	/** Upsert query vectors; `vectors` parallel to `queries` (may be sparse). */
	saveQueryVectors(queries: string[], vectors: (Float32Array | null)[]): void {
		if (!this.db) return;
		const stmt = this._db.prepare(
			`INSERT INTO query_embeddings (query, query_hash, vector, updated_at)
			 VALUES (?, ?, ?, ?)
			 ON CONFLICT(query_hash) DO UPDATE SET
			   query=excluded.query, vector=excluded.vector, updated_at=excluded.updated_at`,
		);
		const t = nowMs();
		this._db.exec("BEGIN");
		try {
			for (let i = 0; i < queries.length; i++) {
				const v = vectors[i];
				if (!v) continue;
				stmt.run(queries[i], md5(queries[i]), JSON.stringify([...v]), t);
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
			// Query vectors: cache by the EXACT embed input (instruction-prefixed
			// for the intent query, truncated for all). Repeated prompts (/pi,
			// /reroll, /tree + resend) hit the cache and skip the embed API call.
			// The doc scoring below still runs live, so semantic freshness is
			// never traded away — only the network round-trip is saved.
			const embedInputs = queries.map((q, i) =>
				i === 0 ? `${QUERY_INSTRUCTION}${q.slice(0, EMBED_INPUT_MAX - QUERY_INSTRUCTION.length)}` : q.slice(0, EMBED_INPUT_MAX),
			);
			const cachedQ = cache.getQueryVectors(embedInputs);
			const missIdx: number[] = [];
			for (let i = 0; i < cachedQ.length; i++) if (!cachedQ[i]) missIdx.push(i);
			if (missIdx.length > 0) {
				const missedInputs = missIdx.map((i) => embedInputs[i]);
				const qvs = await embed(missedInputs);
				if (qvs) {
					cache.saveQueryVectors(missedInputs, qvs);
					for (let j = 0; j < missIdx.length; j++) cachedQ[missIdx[j]] = qvs[j];
				}
			}
			queryVecs = cachedQ.filter((v): v is Float32Array => v !== null);
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

		const { items, mode } = await recall(queries, namespace);
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
}
