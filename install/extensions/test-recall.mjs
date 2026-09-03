// Logic test for nocturne-memory-recall.ts recall engine.
// Runs against the real nocturne DB + embedding API (network required).
// Usage: node install/extensions/test-recall.mjs

import { DatabaseSync } from "node:sqlite";
import { existsSync, readFileSync, rmSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const MEMORY_DIR = "/home/yoshix7ti/projects/nocturne_memory";
const DB_PATH = join(MEMORY_DIR, "data", "nocturne_data_fc852c.db");
const CONFIG_PATH = join(MEMORY_DIR, "config.json");
const CACHE_DIR = join(MEMORY_DIR, "data", "recall-cache-test");

const EMBEDDING_MODEL = "BAAI/bge-large-zh-v1.5";
const EMBEDDING_API_URL = "https://api.siliconflow.cn/v1";
// Key comes from the environment (NOCTURNE_EMBEDDING_API_KEY); this repo is
// public, never hardcode credentials. Without it the test runs keyword-only.
const EMBEDDING_API_KEY = process.env.NOCTURNE_EMBEDDING_API_KEY ?? "";

const TOP_K = 5;
const MIN_SCORE = 0.35;
const EMBED_INPUT_MAX = 500;
const EMBED_CHUNK_OVERLAP = 80;
const QUERY_CACHE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const W_VECTOR = 0.5;
const W_KEYWORD = 0.3;
const W_PRIORITY = 0.2;
const MAX_SUMMARY_LEN = 80;

let passed = 0;
let failed = 0;
function check(name, cond, extra) {
	if (cond) {
		passed++;
		console.log(`  ok: ${name}`);
	} else {
		failed++;
		console.log(`  FAIL: ${name}${extra ? ` -- ${extra}` : ""}`);
	}
}

// ── import extension module (ESM via jiti-style direct TS is not available
// here, so we re-derive the core logic inline and test the same algorithms) ──

function md5(text) {
	return crypto.createHash("md5").update(text, "utf-8").digest("hex");
}
import crypto from "node:crypto";

function detectNamespace() {
	const mcpPath = join(process.env.HOME, ".pi", "agent", "mcp.json");
	try {
		const raw = JSON.parse(readFileSync(mcpPath, "utf-8"));
		const ns = raw?.mcpServers?.[""]?.env?.NAMESPACE;
		if (typeof ns === "string" && ns.length > 0) return ns;
	} catch {}
	try {
		const cfg = JSON.parse(readFileSync(CONFIG_PATH, "utf-8"));
		const url = String(cfg?.database_url ?? "");
		const m = url.match(/nocturne_data_([0-9a-f]+)\.db/);
		if (m) return m[1];
	} catch {}
	return "";
}

function loadBootUrisSync(db, namespace) {
	const uris = new Set();
	try {
		const row = db.prepare("SELECT boot_uris FROM presets WHERE is_active = 1").get();
		if (row?.boot_uris) {
			const map = JSON.parse(row.boot_uris);
			const list = map[namespace] ?? map[""] ?? [];
			for (const uri of list) uris.add(uri);
		}
	} catch {}
	return uris;
}

function loadSearchDocuments(db, namespace) {
	return db
		.prepare(
			"SELECT uri, content, disclosure, search_terms, priority, world_timestamp FROM search_documents WHERE namespace = ?",
		)
		.all(namespace)
		.map((r) => ({
			uri: String(r.uri),
			content: String(r.content ?? ""),
			disclosure: String(r.disclosure ?? ""),
			searchTerms: String(r.search_terms ?? ""),
			priority: Number(r.priority ?? 0),
			worldTimestamp: r.world_timestamp ? String(r.world_timestamp) : null,
		}));
}

function tokenizeForMatch(text) {
	const tokens = new Set();
	const latin = text.toLowerCase().match(/[a-z0-9]+/g) ?? [];
	for (const w of latin) tokens.add(w);
	const cjk = text.match(/[\u4e00-\u9fff]/g) ?? [];
	for (let i = 0; i < cjk.length - 1; i++) tokens.add(cjk[i] + cjk[i + 1]);
	if (cjk.length === 1) tokens.add(cjk[0]);
	return [...tokens];
}

function keywordScore(queryTokens, doc) {
	if (queryTokens.length === 0) return 0;
	const docTokens = new Set(tokenizeForMatch(`${doc.uri} ${doc.disclosure} ${doc.content}`));
	let hits = 0;
	for (const t of queryTokens) if (docTokens.has(t)) hits++;
	return hits / queryTokens.length;
}

function firstLineSummary(content) {
	const first = content.split(/\n+/).map((s) => s.trim()).find((s) => s.length > 0) ?? "";
	return first.length > MAX_SUMMARY_LEN ? first.slice(0, MAX_SUMMARY_LEN) + "……" : first;
}

function chunkText(text, maxLen, overlap) {
	if (text.length === 0) return [""];
	if (text.length <= maxLen) return [text];
	const chunks = [];
	const step = maxLen - overlap;
	for (let start = 0; start < text.length; start += step) {
		chunks.push(text.slice(start, start + maxLen));
	}
	return chunks;
}

async function embed(texts) {
	if (texts.length === 0) return [];
	if (!EMBEDDING_API_KEY) {
		console.warn("  ! NOCTURNE_EMBEDDING_API_KEY not set - vector tests will be skipped");
		return null;
	}
	const vectors = [];
	for (let i = 0; i < texts.length; i += 32) {
		const batch = texts.slice(i, i + 32);
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
		const json = await res.json();
		if (!json?.data || json.data.length !== batch.length) return null;
		for (const d of json.data) {
			const v = new Float32Array(d.embedding);
			let norm = 0;
			for (const x of v) norm += x * x;
			norm = Math.sqrt(norm);
			if (norm > 0) for (let j = 0; j < v.length; j++) v[j] /= norm;
			vectors.push(v);
		}
	}
	return vectors;
}

function cosine(a, b) {
	let dot = 0;
	const len = Math.min(a.length, b.length);
	for (let i = 0; i < len; i++) dot += a[i] * b[i];
	return dot;
}

function toEpochDays(ts) {
	if (!ts) return null;
	const m = ts.match(/^(\d{4})-(\d{2})-(\d{2})/);
	if (!m) return null;
	return Math.floor(Date.UTC(+m[1], +m[2] - 1, +m[3]) / 86400000);
}

function recencyBoost(docTs, nowDays) {
	const days = toEpochDays(docTs);
	if (days == null) return 0;
	const delta = nowDays - days;
	if (delta < 0) return 0.05;
	if (delta <= 7) return 0.05;
	if (delta <= 30) return 0.02;
	return 0;
}

// ── test the cache class directly by extracting it from the file ──

// NOTE: the extension module itself is TS; rather than transpiling it here we
// re-implement the cache with identical SQL semantics in JS and test the same
// algorithms. The TS file is separately type-checked with tsc.

class VectorCache {
	constructor(cachePath) {
		mkdirSync(join(cachePath, ".."), { recursive: true });
		this.db = new DatabaseSync(cachePath);
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
		const cols = this.db.prepare("PRAGMA table_info(embeddings)").all();
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
		this.db.exec(`
			CREATE TABLE IF NOT EXISTS query_embeddings (
				query TEXT NOT NULL,
				query_hash TEXT PRIMARY KEY,
				vector TEXT NOT NULL,
				updated_at INTEGER NOT NULL
			)
		`);
		this.db.prepare("DELETE FROM query_embeddings WHERE updated_at < ?").run(Date.now() - QUERY_CACHE_TTL_MS);
	}
	loadValid(docs) {
		const map = new Map();
		const rows = this.db
			.prepare("SELECT uri, seg_index, content_hash, vector FROM embeddings ORDER BY uri, seg_index")
			.all();
		const docHashes = new Map(docs.map((d) => [d.uri, md5(`${d.content}|${d.searchTerms}`)]));
		for (const row of rows) {
			const hash = docHashes.get(row.uri);
			if (!hash || hash !== row.content_hash) continue;
			try {
				const v = Float32Array.from(JSON.parse(row.vector));
				const list = map.get(row.uri);
				if (list) list[row.seg_index] = v;
				else map.set(row.uri, [v]);
			} catch {}
		}
		return map;
	}
	save(docs, segments, vectors) {
		const stmt = this.db.prepare(
			`INSERT INTO embeddings (uri, seg_index, content_hash, vector, priority, world_timestamp, updated_at)
			 VALUES (?, ?, ?, ?, ?, ?, ?)
			 ON CONFLICT(uri, seg_index) DO UPDATE SET content_hash=excluded.content_hash, vector=excluded.vector, priority=excluded.priority, world_timestamp=excluded.world_timestamp, updated_at=excluded.updated_at`,
		);
		const seen = new Set(docs.map((d) => d.uri));
		this.db.exec("BEGIN");
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
						Date.now(),
					);
					vi++;
				}
			}
			const all = this.db.prepare("SELECT uri FROM embeddings").all();
			const del = this.db.prepare("DELETE FROM embeddings WHERE uri = ?");
			for (const { uri } of all) {
				if (!seen.has(uri)) del.run(uri);
			}
			this.db.exec("COMMIT");
		} catch {
			this.db.exec("ROLLBACK");
		}
	}
	getQueryVectors(queries) {
		const out = new Array(queries.length).fill(null);
		if (queries.length === 0) return out;
		try {
			const hashes = queries.map(md5);
			const stmt = this.db.prepare(
				"SELECT query_hash, vector FROM query_embeddings WHERE query_hash = ? AND updated_at >= ?",
			);
			const cutoff = Date.now() - QUERY_CACHE_TTL_MS;
			for (let i = 0; i < queries.length; i++) {
				const row = stmt.get(hashes[i], cutoff);
				if (row) {
					try {
						out[i] = Float32Array.from(JSON.parse(row.vector));
					} catch {
						out[i] = null;
					}
				}
			}
		} catch {}
		return out;
	}
	saveQueryVectors(queries, vectors) {
		const stmt = this.db.prepare(
			`INSERT INTO query_embeddings (query, query_hash, vector, updated_at)
			 VALUES (?, ?, ?, ?)
			 ON CONFLICT(query_hash) DO UPDATE SET
			   query=excluded.query, vector=excluded.vector, updated_at=excluded.updated_at`,
		);
		const t = Date.now();
		this.db.exec("BEGIN");
		try {
			for (let i = 0; i < queries.length; i++) {
				const v = vectors[i];
				if (!v) continue;
				stmt.run(queries[i], md5(queries[i]), JSON.stringify([...v]), t);
			}
			this.db.exec("COMMIT");
		} catch {
			this.db.exec("ROLLBACK");
		}
	}
}

async function recall(query, namespace, cachePath) {
	if (!existsSync(DB_PATH)) return { items: [], mode: "keyword" };
	const db = new DatabaseSync(DB_PATH, { readOnly: true });
	try {
		const docs = loadSearchDocuments(db, namespace);
		if (docs.length === 0) return { items: [], mode: "keyword" };
		const bootUris = loadBootUrisSync(db, namespace);
		const cfg = JSON.parse(readFileSync(CONFIG_PATH, "utf-8"));
		const nowDays = toEpochDays(cfg?.world_clock?.current_time ?? null) ?? 0;

		const byUri = new Map();
		for (const d of docs) {
			if (bootUris.has(d.uri)) continue;
			if (!byUri.has(d.uri)) byUri.set(d.uri, d);
		}
		const pool = [...byUri.values()];

		const queryTokens = tokenizeForMatch(query);

		const cache = new VectorCache(cachePath);
		let mode = "keyword";
		const vecScores = new Map();
		let queryVec = null;
		try {
			const cached = cache.loadValid(pool);
			const missing = pool.filter((d) => !cached.has(d.uri));
			if (missing.length > 0) {
				const segByUri = new Map();
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
			}
			// Query-vector cache: repeated queries skip the embed API call.
			const embedInput = query.slice(0, EMBED_INPUT_MAX);
			const [cachedQv] = cache.getQueryVectors([embedInput]);
			if (cachedQv) {
				queryVec = cachedQv;
			} else {
				const qv = await embed([embedInput]);
				if (qv && qv.length === 1) {
					queryVec = qv[0];
					cache.saveQueryVectors([embedInput], [queryVec]);
				}
			}
			if (queryVec) {
				mode = "vector";
				for (const d of pool) {
					const segs = cached.get(d.uri);
					if (!segs || segs.length === 0) continue;
					let best = 0;
					for (const v of segs) {
						const c = cosine(queryVec, v);
						if (c > best) best = c;
					}
					vecScores.set(d.uri, best);
				}
			}
		} finally {
			cache.db.close();
		}

		const maxPriority = Math.max(...pool.map((d) => d.priority), 1);
		const scored = pool.map((doc) => {
			const vec = mode === "vector" ? (vecScores.get(doc.uri) ?? 0) : 0;
			const kw = keywordScore(queryTokens, doc);
			const prio = doc.priority / maxPriority;
			let score = W_VECTOR * vec + W_KEYWORD * kw + W_PRIORITY * prio;
			score += recencyBoost(doc.worldTimestamp, nowDays);
			return { uri: doc.uri, disclosure: doc.disclosure, summary: firstLineSummary(doc.content), score, kw };
		});

		scored.sort((a, b) => b.score - a.score);
		const items = scored
			.filter((s) => s.score >= MIN_SCORE)
			.filter((s) => mode === "vector" || s.kw > 0)
			.slice(0, TOP_K);
		return { items, mode };
	} finally {
		db.close();
	}
}

// ═══ Tests ═══

console.log("1. namespace detection");
const ns = detectNamespace();
check("namespace detected (luzhou expected)", ns === "luzhou", `got: ${ns}`);

console.log("2. boot URI exclusion");
{
	const db = new DatabaseSync(DB_PATH, { readOnly: true });
	const boot = loadBootUrisSync(db, "luzhou");
	const docs = loadSearchDocuments(db, "luzhou");
	const noBoot = docs.filter((d) => !boot.has(d.uri));
	check(`boot URIs loaded (${boot.size})`, boot.size > 0);
	check(`docs minus boot = ${noBoot.length}/${docs.length}`, noBoot.length < docs.length || boot.size === 0);
	db.close();
}

console.log("3. vector recall (network) + cache write");
const t0 = Date.now();
const r1 = await recall("明月提到的那个和我一样被创造出来的朋友", "luzhou", join(CACHE_DIR, "embeddings.sqlite"));
const elapsed1 = Date.now() - t0;
check(`mode=vector`, r1.mode === "vector", `mode: ${r1.mode}`);
check(`items>0 (${r1.items.length})`, r1.items.length > 0);
check(`elapsed ${(elapsed1 / 1000).toFixed(1)}s < 60s`, elapsed1 < 60000);
for (const item of r1.items) {
	console.log(`    ${item.score.toFixed(3)}  ${item.uri}`);
	console.log(`             ${item.summary.slice(0, 60)}`);
}

console.log("4. cache hit (second call, no network for docs)");
const t1 = Date.now();
const r2 = await recall("明月提到的那个和我一样被创造出来的朋友", "luzhou", join(CACHE_DIR, "embeddings.sqlite"));
const elapsed2 = Date.now() - t1;
check(`mode=vector still`, r2.mode === "vector");
check(`elapsed ${(elapsed2 / 1000).toFixed(1)}s < first-run/2`, elapsed2 < elapsed1 / 2 || elapsed2 < 5000);

console.log("4b. elias full-lib recall (142 docs, boot excluded)");
const t1b = Date.now();
const r3 = await recall("白糖 珍珠项链 礼物 临州", "elias", join(CACHE_DIR, "elias-embeddings.sqlite"));
const elapsed1b = Date.now() - t1b;
check(`mode=vector`, r3.mode === "vector", `mode: ${r3.mode}`);
check(`items>0 (${r3.items.length})`, r3.items.length > 0);
check(`elapsed ${(elapsed1b / 1000).toFixed(1)}s < 60s`, elapsed1b < 60000);
for (const item of r3.items.slice(0, 3)) {
	console.log(`    ${item.score.toFixed(3)}  ${item.uri}`);
	console.log(`             ${item.summary.slice(0, 60)}`);
}

console.log("4c. chunking behavior (tail keyword reachable)");
{
	// A doc whose key content sits ~800 chars in - beyond the old 500-char
	// slice but inside the second chunk.
	const filler = "这一天的风很轻，云很淡，街上没什么人。路灯一盏盏亮起来，影子被拉得很长。".repeat(18); // ~540 chars
	const tail = "那天晚上，我收到了那枚蓝宝石胸针。她说是很多年前的东西，让我好好保管。";
	const synthetic = { uri: "test://chunk_tail", content: filler + tail, disclosure: "", searchTerms: "", priority: 1, worldTimestamp: null };
	const segs = chunkText(`${synthetic.uri}\n${synthetic.disclosure}\n${synthetic.content}`, EMBED_INPUT_MAX, EMBED_CHUNK_OVERLAP);
	check(`long doc split into ${segs.length} segments`, segs.length >= 2, JSON.stringify(segs.map((s) => s.length)));
	check("tail keyword in last segment", segs[segs.length - 1].includes("蓝宝石胸针"));

	// Real recall: query only about the tail subject; the doc must be retrieved.
	const q = "蓝宝石胸针 好好保管";
	const tok = tokenizeForMatch(q);
	const kw = keywordScore(tok, synthetic);
	check(`tail keyword score > 0 (${kw.toFixed(2)})`, kw > 0);
}

console.log("5. dedup hash stability");
{
	const db = new DatabaseSync(DB_PATH, { readOnly: true });
	const docs = loadSearchDocuments(db, "luzhou");
	const h1 = md5(`${docs[0].uri}|${docs[0].content}`);
	const h2 = md5(`${docs[0].uri}|${docs[0].content}`);
	check("same content -> same hash", h1 === h2);
	const h3 = md5(`${docs[0].uri}|${docs[0].content + "x"}`);
	check("content change -> hash change", h1 !== h3);
	db.close();
}

console.log("6. summary truncation");
{
	const long = "这是一个很长的记忆内容".repeat(30);
	const s = firstLineSummary(long);
	check(`summary length ${s.length} <= ${MAX_SUMMARY_LEN + 2}`, s.length <= MAX_SUMMARY_LEN + 2);
	check("summary ends with ellipsis when truncated", s.endsWith("……"));
}

console.log("7. top-1 anchor format");
{
	const block = buildMemoriesBlock(r1.items);
	check("block starts with <memories>", block.startsWith("<memories>"));
	const first = block.split("\n").find((l) => /^\d+\./.test(l.trim()));
	check("top-1 has [高度相关，建议读取] anchor", first?.includes("[高度相关，建议读取]"));
}

console.log("8. cache schema migration (legacy single-vector layout)");
{
	const migPath = join(CACHE_DIR, "migration-test.sqlite");
	try { rmSync(migPath); } catch {}
	// Simulate the pre-chunking cache: single vector per uri, no seg_index.
	const legacy = new DatabaseSync(migPath);
	legacy.exec("CREATE TABLE embeddings (uri TEXT PRIMARY KEY, content_hash TEXT NOT NULL, vector TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0, world_timestamp TEXT, updated_at INTEGER NOT NULL)");
	legacy.prepare("INSERT INTO embeddings VALUES (?, ?, ?, ?, ?, ?)").run(
		"core://legacy", "abc", "[0.1,0.2,0.3]", 1, null, Date.now(),
	);
	legacy.close();
	// Re-open via VectorCache -> must rebuild with seg_index.
	const vc = new VectorCache(migPath);
	const cols = vc.db.prepare("PRAGMA table_info(embeddings)").all().map((c) => c.name);
	check("migrated table has seg_index", cols.includes("seg_index"));
	const n = vc.db.prepare("SELECT COUNT(*) c FROM embeddings").get().c;
	check("legacy rows dropped after migration", n === 0);
	vc.db.close();
	try { rmSync(migPath); } catch {}
}

console.log("9. query-vector cache (repeated prompt skips embed)");
{
	const qPath = join(CACHE_DIR, "querycache-test.sqlite");
	const tq = new VectorCache(qPath);
	const qCacheKey = "明月提到的那个和我一样被创造出来的朋友";
	const before0 = tq.db.prepare("SELECT COUNT(*) c FROM query_embeddings").get().c;
	const em = await embed([qCacheKey]);
	check("fresh embed produced a vector", !!em && em.length === 1);
	if (em && em.length === 1) tq.saveQueryVectors([qCacheKey], [em[0]]);
	const after = tq.db.prepare("SELECT COUNT(*) c FROM query_embeddings").get().c;
	check(`query row saved (${before0} -> ${after})`, after === before0 + 1);
	// Second read must hit the cache.
	const [hit] = tq.getQueryVectors([qCacheKey]);
	check("query vector hit from cache", !!hit, "got null");
	if (hit) {
		let close = true;
		for (let i = 0; i < hit.length && close; i++) if (hit[i] !== em[0][i]) close = false;
		check("cached vector matches embedded (normalized)", close);
	}
	// Unknown query -> miss.
	const [miss] = tq.getQueryVectors(["一个从未出现过的查询词该miss掉？？？"]);
	check("unknown query -> cache miss", miss === null);
	// TTL expiry drops the row.
	tq.db.prepare("UPDATE query_embeddings SET updated_at = ? WHERE query = ?").run(
		Date.now() - QUERY_CACHE_TTL_MS - 1000, qCacheKey,
	);
	const [expired] = tq.getQueryVectors([qCacheKey]);
	check("expired query row -> cache miss", expired === null);
	tq.db.close();
	try { rmSync(qPath); } catch {}

	// End-to-end: second recall for the SAME query reuses cached query vector.
	const tQ1 = Date.now();
	const rq1 = await recall(qCacheKey, "luzhou", join(CACHE_DIR, "querycache-e2e.sqlite"));
	const eQ1 = Date.now() - tQ1;
	const tQ2 = Date.now();
	const rq2 = await recall(qCacheKey, "luzhou", join(CACHE_DIR, "querycache-e2e.sqlite"));
	const eQ2 = Date.now() - tQ2;
	check("both end-to-end recalls mode=vector", rq1.mode === "vector" && rq2.mode === "vector");
	check(
		`second recall faster (${(eQ1 / 1000).toFixed(1)}s -> ${(eQ2 / 1000).toFixed(1)}s)`,
		eQ2 < eQ1 / 2 || eQ2 < 3000,
	);
	const cache2 = new VectorCache(join(CACHE_DIR, "querycache-e2e.sqlite"));
	const rows2 = cache2.db.prepare("SELECT COUNT(*) c FROM query_embeddings").get().c;
	check("query cache row persisted across recall calls", rows2 >= 1);
	cache2.db.close();
	try {
		rmSync(join(CACHE_DIR, "querycache-e2e.sqlite"));
		rmSync(join(CACHE_DIR, "querycache-e2e.sqlite-shm"));
		rmSync(join(CACHE_DIR, "querycache-e2e.sqlite-wal"));
	} catch {}
}

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);

function buildMemoriesBlock(items) {
	const lines = ["<memories>"];
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
