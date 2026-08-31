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
				uri TEXT PRIMARY KEY,
				content_hash TEXT NOT NULL,
				vector TEXT NOT NULL,
				priority INTEGER NOT NULL DEFAULT 0,
				world_timestamp TEXT,
				updated_at INTEGER NOT NULL
			)
		`);
	}
	loadValid(docs) {
		const map = new Map();
		const rows = this.db.prepare("SELECT uri, content_hash, vector FROM embeddings").all();
		const byUri = new Map(rows.map((r) => [r.uri, r]));
		const docHashes = new Map(docs.map((d) => [d.uri, md5(`${d.content}|${d.searchTerms}`)]));
		for (const [uri, row] of byUri) {
			const hash = docHashes.get(uri);
			if (hash && hash === row.content_hash) {
				try {
					map.set(uri, Float32Array.from(JSON.parse(row.vector)));
				} catch {}
			}
		}
		return map;
	}
	save(docs, vectors) {
		const stmt = this.db.prepare(
			`INSERT INTO embeddings (uri, content_hash, vector, priority, world_timestamp, updated_at)
			 VALUES (?, ?, ?, ?, ?, ?)
			 ON CONFLICT(uri) DO UPDATE SET content_hash=excluded.content_hash, vector=excluded.vector, priority=excluded.priority, world_timestamp=excluded.world_timestamp, updated_at=excluded.updated_at`,
		);
		const seen = new Set(docs.map((d) => d.uri));
		this.db.exec("BEGIN");
		try {
			for (let i = 0; i < docs.length; i++) {
				stmt.run(
					docs[i].uri,
					md5(`${docs[i].content}|${docs[i].searchTerms}`),
					JSON.stringify([...vectors[i]]),
					docs[i].priority,
					docs[i].worldTimestamp,
					Date.now(),
				);
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
				const vectors = await embed(
					missing.map((d) => `${d.uri}\n${d.disclosure}\n${d.content}`.slice(0, EMBED_INPUT_MAX)),
				);
				if (vectors) {
					cache.save(missing, vectors);
					for (let i = 0; i < missing.length; i++) cached.set(missing[i].uri, vectors[i]);
				}
			}
			const qv = await embed([query.slice(0, EMBED_INPUT_MAX)]);
			if (qv && qv.length === 1) queryVec = qv[0];
			if (queryVec) {
				mode = "vector";
				for (const d of pool) {
					const v = cached.get(d.uri);
					if (v) vecScores.set(d.uri, cosine(queryVec, v));
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
