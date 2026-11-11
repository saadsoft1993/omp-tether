/**
 * Reading banks (read-only SQLite handles), and writing/deleting rows through the Mnemopi library.
 *
 * A row the importer writes is what omp's own retain/learn writes (source string aside): scope `bank`, veracity
 * `tool`, memory type `fact` (summaries: `episode`), the bank's own session/author/channel ids, FTS through the
 * library's triggers, the `occurred_on` / `has_source` annotations, an embedding by the bank's model. Differences,
 * on purpose: the original timestamp, trust tier `IMPORTED`, and `consolidated_at` stamped by the library's import
 * path, so the next omp start neither trims nor summarises the row. Fact/triple extraction never runs.
 */
import { Database } from "bun:sqlite";
import { existsSync, readdirSync, readlinkSync, realpathSync, statSync } from "node:fs";
import path from "node:path";
import {
	assertBankWritable,
	bankDbPath,
	DEFAULT_EMBEDDING_MODEL,
	freshCoreSchema,
	type Library,
	libraryOptions,
	readCoreSchema,
	schemaDiff,
} from "./lib";
import type { Embedder } from "./embedder";
import { isRecord, type SourceId, type SourceKind } from "./model";
import { fail } from "./sink";

export interface ImportedRow {
	id: string;
	originKey: string;
	originHash: string;
	runId: string | null;
	source: string | null;
}

export interface BankState {
	exists: boolean;
	/** originKey -> row (rows an importer wrote, found by their metadata). */
	imported: Map<string, ImportedRow>;
	/** content -> row id, every row of the bank. */
	contents: Map<string, string>;
	rowIds: Set<string>;
	models: string[];
	schemaProblems: string[];
	rowCount: number;
}

export function emptyBankState(): BankState {
	return {
		exists: false,
		imported: new Map(),
		contents: new Map(),
		rowIds: new Set(),
		models: [],
		schemaProblems: [],
		rowCount: 0,
	};
}

/**
 * A read-only handle. A WAL-mode database whose `-shm` file is gone cannot be opened by a read-only connection; that
 * means no process has it open, so, when its `-wal` is empty too, it is opened `immutable` (nothing can change under it).
 */
export function openReadonly(file: string): Database {
	const plain = new Database(file, { readonly: true });
	try {
		plain.query("SELECT 1 FROM sqlite_master LIMIT 1").get();
		return plain;
	} catch (error) {
		plain.close();
		if (!String(error).includes("unable to open")) throw error;
	}
	const wal = `${file}-wal`;
	if (existsSync(wal) && statSync(wal).size > 0) fail("a bank has unmerged WAL frames and cannot be read read-only", file);
	return new Database(`file:${file}?mode=ro&immutable=1`, { readonly: true });
}

function parseMeta(json: string | null): Record<string, unknown> {
	if (json === null || json === "") return {};
	try {
		const parsed: unknown = JSON.parse(json);
		return isRecord(parsed) ? parsed : {};
	} catch {
		return {};
	}
}

export async function readBankState(lib: Library, banksDir: string, bank: string): Promise<BankState> {
	const file = bankDbPath(banksDir, bank);
	if (!existsSync(file)) return emptyBankState();
	const db = openReadonly(file);
	try {
		const state = emptyBankState();
		state.exists = true;
		state.schemaProblems = schemaDiff(await freshCoreSchema(lib), readCoreSchema(db));
		if (state.schemaProblems.length > 0) return state;
		const rows = db.query("SELECT id, content, metadata_json FROM working_memory").all() as {
			id: string;
			content: string;
			metadata_json: string | null;
		}[];
		for (const row of rows) {
			state.rowIds.add(row.id);
			state.contents.set(row.content, row.id);
			const meta = parseMeta(row.metadata_json);
			if (typeof meta.originKey === "string" && typeof meta.originHash === "string") {
				state.imported.set(meta.originKey, {
					id: row.id,
					originKey: meta.originKey,
					originHash: meta.originHash,
					runId: typeof meta.importRunId === "string" ? meta.importRunId : null,
					source: typeof meta.source === "string" ? meta.source : null,
				});
			}
		}
		state.rowCount = rows.length;
		state.models = (db.query("SELECT DISTINCT model FROM memory_embeddings").all() as { model: string | null }[]).map(
			row => row.model ?? "(none)",
		);
		return state;
	} finally {
		db.close();
	}
}

/** Bank directories under `banksDir` that hold a database. */
export function listBanks(banksDir: string): string[] {
	try {
		return readdirSync(banksDir)
			.filter(name => existsSync(bankDbPath(banksDir, name)))
			.sort();
	} catch {
		return [];
	}
}

function lsofPath(): string | null {
	return Bun.which("lsof", { PATH: process.env.PATH ?? "" }) ?? (existsSync("/usr/sbin/lsof") ? "/usr/sbin/lsof" : null);
}

/** Linux without lsof: the processes whose open file descriptors point at one of `files` (own processes; root sees all). */
function procHolders(files: string[]): string[] | null {
	let pids: string[];
	try {
		pids = readdirSync("/proc").filter(name => /^\d+$/.test(name));
	} catch {
		return null;
	}
	const wanted = new Set(files.map(file => realpathSync(file)));
	const holders: string[] = [];
	for (const pid of pids) {
		if (pid === String(process.pid)) continue;
		let fds: string[];
		try {
			fds = readdirSync(`/proc/${pid}/fd`);
		} catch {
			continue; // another user's process, or one that just ended
		}
		const holds = fds.some(fd => {
			try {
				return wanted.has(readlinkSync(`/proc/${pid}/fd/${fd}`));
			} catch {
				return false;
			}
		});
		if (holds) holders.push(pid);
	}
	return holders;
}

/**
 * Processes (other than this one) that have the bank database open: `lsof` when it is installed, else, on Linux, a scan of
 * /proc/<pid>/fd. Null when neither is available (the caller then refuses unless the user declares the sessions closed).
 */
export function bankOpenBy(banksDir: string, bank: string): string[] | null {
	const base = bankDbPath(banksDir, bank);
	const files = [base, `${base}-wal`, `${base}-shm`].filter(file => existsSync(file));
	if (files.length === 0) return [];
	const lsof = lsofPath();
	if (lsof === null) return process.platform === "linux" ? procHolders(files) : null;
	const ran = Bun.spawnSync([lsof, "-t", "--", ...files], { stdout: "pipe", stderr: "pipe" });
	if (ran.exitCode > 1) return null;
	const holders = ran.stdout
		.toString()
		.split("\n")
		.map(line => line.trim())
		.filter(pid => pid !== "" && pid !== String(process.pid));
	// lsof also exits 1 when it could not look (it says why on stderr): that is "unknown", not "nobody"
	if (ran.exitCode === 1 && holders.length === 0 && ran.stdout.toString().trim() === "" && ran.stderr.toString().trim() !== "") return null;
	return holders;
}

export interface RowToWrite {
	id: string;
	content: string;
	source: SourceId;
	sourceKind: SourceKind;
	timestamp: string;
	importance: number;
	memoryType: string;
	metadata: Record<string, unknown>;
}

const sqliteTime = (iso: string): string => iso.slice(0, 19).replace("T", " ");

function toRestoreRow(bank: string, row: RowToWrite, importedAt: string): Record<string, unknown> {
	return {
		id: row.id,
		content: row.content,
		source: `import-${row.source}`,
		timestamp: row.timestamp,
		session_id: bank,
		importance: row.importance,
		metadata_json: JSON.stringify(row.metadata),
		scope: "bank",
		veracity: "tool",
		memory_type: row.memoryType,
		author_id: "coding-agent",
		author_type: "agent",
		channel_id: bank,
		trust_tier: "IMPORTED",
		created_at: sqliteTime(importedAt),
		consolidated_at: null,
		valid_until: null,
		superseded_by: null,
		recall_count: 0,
		last_recalled: null,
		embed_text: null,
		event_date: null,
		event_date_precision: "unknown",
		temporal_tags: "[]",
	};
}

/** The step of the write sequence an error came from (the self-test names it). */
export type ApplyStage = "bank check" | "restore insert" | "annotation" | "embedding" | "native rows";

export interface ApplyResult {
	bank: string;
	inserted: string[];
	error: string | null;
	/** Where `error` happened; null without an error. */
	stage: ApplyStage | null;
	rolledBack: boolean;
}

/** The embedding model a bank already uses (one model), else omp's default. More than one model is refused. */
export function bankEmbeddingModel(state: BankState, override: string | undefined): { model: string; problem: string | null } {
	if (override) return { model: override, problem: state.models.length > 0 && !state.models.includes(override) ? `bank has ${state.models.join(", ")}` : null };
	if (state.models.length > 1) return { model: DEFAULT_EMBEDDING_MODEL, problem: `bank has several embedding models: ${state.models.join(", ")}` };
	return { model: state.models[0] ?? DEFAULT_EMBEDDING_MODEL, problem: null };
}

/**
 * Moves the write-ahead log into the database file and empties it, so that what this tool wrote is in the file itself (a copy of
 * the file alone has it) and a leftover log is a sign of somebody else's unmerged data. It cannot truncate while another process
 * has the bank open; then the log stays, which is the safe answer for the exemption check.
 */
function checkpoint(db: Database): void {
	try {
		db.exec("PRAGMA wal_checkpoint(TRUNCATE)");
	} catch {
		// busy or read-only: the log stays
	}
}

/** Rows of `working_memory` other than `ids`: their number and a digest of every column, in id order. */
function otherRows(db: Database, ids: ReadonlySet<string>): { count: number; digest: string } {
	const hasher = new Bun.CryptoHasher("sha256");
	let count = 0;
	for (const row of db.query("SELECT * FROM working_memory ORDER BY id").iterate()) {
		if (!isRecord(row) || ids.has(String(row.id))) continue;
		hasher.update(`${JSON.stringify(Object.values(row))}\n`);
		count += 1;
	}
	return { count, digest: hasher.digest("hex") };
}

const messageOf = (error: unknown): string => (error instanceof Error ? error.message : String(error));

/**
 * Write one bank: rows, annotations and embeddings, in ONE transaction. The embeddings are made first, with the same
 * embedder (the library's own embedding, in a worker) and stored with the SQL the library's embedding step uses, so a
 * process that stops at any point leaves the bank without the rows or with every row complete. Nothing is written when
 * an embedding is missing. The transaction is rolled back too when the bank's other rows differ afterwards (a count and a
 * digest of every column are compared). `beforeWrite` runs right before the transaction opens and may throw to refuse.
 */
export async function applyBank(
	lib: Library,
	banksDir: string,
	bank: string,
	rows: RowToWrite[],
	options: { model: string; importedAt: string; embedder: Embedder; beforeWrite?: () => void | Promise<void> },
): Promise<ApplyResult> {
	const result: ApplyResult = { bank, inserted: [], error: null, stage: null, rolledBack: false };
	const refuse = (stage: ApplyStage, error: string): ApplyResult => {
		result.error = error;
		result.stage = stage;
		result.rolledBack = true;
		return result;
	};
	try {
		assertBankWritable(banksDir, bank);
	} catch (error) {
		return refuse("bank check", messageOf(error));
	}
	let vectors: number[][];
	try {
		vectors = await options.embedder.embed(rows.map(row => row.content));
	} catch (error) {
		return refuse("embedding", `no embedding with ${options.model} (${messageOf(error)}); nothing was written`);
	}
	const complete =
		vectors.length === rows.length &&
		vectors.every(vector => Array.isArray(vector) && vector.length > 0 && vector.every(value => typeof value === "number" && Number.isFinite(value)));
	if (!complete) return refuse("embedding", `no embedding with ${options.model} for every one of the ${rows.length} rows (the embedder returned ${vectors.length}); nothing was written`);
	const file = bankDbPath(banksDir, bank);
	// the repeated checks (read-only) come first: the library's constructor opens the database for writing and may change it
	try {
		await options.beforeWrite?.();
		assertBankWritable(banksDir, bank);
	} catch (error) {
		return refuse("bank check", messageOf(error));
	}
	const memory = new lib.Mnemopi(libraryOptions(file, bank, options.model, options.embedder));
	const db = memory.db;
	try {
		const ids = new Set(rows.map(row => row.id));
		db.exec("BEGIN IMMEDIATE");
		let stage: ApplyStage = "restore insert";
		try {
			const before = otherRows(db, ids);
			const stats = memory.beam.importFromDict({ working_memory: rows.map(row => toRestoreRow(bank, row, options.importedAt)) });
			if (stats.working_memory.inserted !== rows.length) {
				throw new Error(`library inserted ${stats.working_memory.inserted} of ${rows.length} rows (id collision?)`);
			}
			stage = "annotation";
			for (const row of rows) {
				memory.beam.annotations.add(row.id, "occurred_on", row.timestamp.slice(0, 10));
				memory.beam.annotations.add(row.id, "has_source", `import-${row.source}`);
			}
			stage = "embedding";
			// the statement of the library's own embedding step; vectors are kept as 32-bit floats, as the library keeps them
			const store = db.prepare("INSERT OR REPLACE INTO memory_embeddings(memory_id, embedding_json, model) VALUES (?, ?, ?)");
			rows.forEach((row, index) => store.run(row.id, JSON.stringify(Array.from(Float32Array.from(vectors[index] ?? []))), options.model));
			stage = "native rows";
			const after = otherRows(db, ids);
			if (after.count !== before.count || after.digest !== before.digest) throw new Error("the bank's other rows changed during the write");
			db.exec("COMMIT");
		} catch (error) {
			if (db.inTransaction) db.exec("ROLLBACK");
			return refuse(stage, messageOf(error));
		}
		result.inserted = rows.map(row => row.id);
		await memory.flushExtractions();
		checkpoint(db);
		return result;
	} finally {
		memory.close();
	}
}

/** Delete rows (by id) through the library's own delete path. Returns the ids that were present and are now gone. */
export function forgetRows(lib: Library, banksDir: string, bank: string, ids: string[], model: string): string[] {
	assertBankWritable(banksDir, bank);
	const file = bankDbPath(banksDir, bank);
	const memory = new lib.Mnemopi(libraryOptions(file, bank, model));
	try {
		const removed: string[] = [];
		memory.db.exec("BEGIN IMMEDIATE");
		try {
			for (const id of ids) if (lib.forgetWorking(memory.beam, id)) removed.push(id);
			memory.db.exec("COMMIT");
			checkpoint(memory.db);
		} catch (error) {
			memory.db.exec("ROLLBACK");
			throw error;
		}
		return removed;
	} finally {
		memory.close();
	}
}

export interface RunRows {
	bank: string;
	/** Rows that carry the run id and are provably importer rows: the only ones undo may delete. */
	ids: string[];
	originKeys: Map<string, string>;
	/** Rows that carry the run id but are not importer rows (trust tier, source or provenance differ): never deleted. */
	foreign: string[];
}

/** An importer row: trust tier IMPORTED, source `import-<source>` agreeing with a non-empty text `source` in its metadata, tool `import` and the provenance keys. */
export function isImporterRow(row: { source: string | null; trust_tier: string | null }, meta: Record<string, unknown>): boolean {
	return (
		row.trust_tier === "IMPORTED" &&
		typeof meta.source === "string" &&
		meta.source !== "" &&
		row.source === `import-${meta.source}` &&
		meta.tool === "import" &&
		typeof meta.originKey === "string" &&
		typeof meta.originHash === "string"
	);
}

/** Rows whose metadata says they were written by run `runId` (found by scanning, independent of the ledger). */
export function rowsOfRun(banksDir: string, runId: string, options: { skipUnreadable?: boolean } = {}): RunRows[] {
	const found: RunRows[] = [];
	for (const bank of listBanks(banksDir)) {
		let db: Database;
		try {
			db = openReadonly(bankDbPath(banksDir, bank));
		} catch (error) {
			if (options.skipUnreadable) continue;
			throw error;
		}
		try {
			const rows = db
				.query(
					"SELECT id, source, trust_tier, metadata_json FROM working_memory WHERE json_valid(metadata_json) AND json_extract(metadata_json, '$.importRunId') = ?",
				)
				.all(runId) as { id: string; source: string | null; trust_tier: string | null; metadata_json: string }[];
			if (rows.length === 0) continue;
			const mine: { id: string; originKey: string }[] = [];
			const foreign: string[] = [];
			for (const row of rows) {
				const meta = parseMeta(row.metadata_json);
				if (isImporterRow(row, meta)) mine.push({ id: row.id, originKey: String(meta.originKey) });
				else foreign.push(row.id);
			}
			found.push({ bank, ids: mine.map(row => row.id), originKeys: new Map(mine.map(row => [row.id, row.originKey])), foreign });
		} catch (error) {
			if (!options.skipUnreadable) throw error;
		} finally {
			db.close();
		}
	}
	return found;
}

/** Which of `ids` are rows of the bank right now (read-only; the answer after a delete is the proof of what was deleted). */
export function existingIds(banksDir: string, bank: string, ids: string[]): Set<string> {
	const present = new Set<string>();
	const file = bankDbPath(banksDir, bank);
	if (!existsSync(file)) return present;
	const db = openReadonly(file);
	try {
		for (let start = 0; start < ids.length; start += 500) {
			const chunk = ids.slice(start, start + 500);
			const found = db.query(`SELECT id FROM working_memory WHERE id IN (${chunk.map(() => "?").join(",")})`).all(...chunk) as { id: string }[];
			for (const row of found) present.add(row.id);
		}
	} finally {
		db.close();
	}
	return present;
}

export function bankPath(banksDir: string, bank: string): string {
	return path.dirname(bankDbPath(banksDir, bank));
}

/**
 * What a bank may hold for it to be exempt from the backup requirement: nothing of the user's. The same definition is mirrored in
 * `python/omp_memory/safety.py`; change both together.
 *
 *  - `working_memory`: every row is this tool's (`isImporterRow`).
 *  - `annotations` and `memory_embeddings` (importer-derived): rows only for ids of working rows (which are all this tool's).
 *  - `fts_working` and the shadow tables (`_config`, `_data`, `_docsize`, `_idx`, `_content`) of any FTS5 table, and `sqlite_*`
 *    tables: derived or internal, not looked at.
 *  - every other table, whatever its name (episodic memory, scratchpad, gists, facts, triples, graph, consolidation log,
 *    `fts_episodes`, `fts_facts`, a table this list does not know): zero rows.
 *  - a non-empty `-wal` or `-journal` file (unmerged data), an unreadable or locked database, any error: not exempt.
 */
export const EXEMPTION_RULES = {
	rowTable: "working_memory",
	derivedTables: { annotations: "memory_id", memory_embeddings: "memory_id" } as Readonly<Record<string, string>>,
	derivedFts: "fts_working",
	ftsShadowSuffixes: ["config", "data", "docsize", "idx", "content"],
} as const;

const quoted = (name: string): string => `"${name.replaceAll('"', '""')}"`;

function holdsOnlyImporterContentIn(db: Database): boolean {
	const tables = db.query("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite!_%' ESCAPE '!'").all() as { name: string; sql: string | null }[];
	const fts = tables.filter(table => /^\s*create\s+virtual\s+table\b[^(]*\busing\s+fts5\b/i.test(table.sql ?? "")).map(table => table.name);
	const isShadow = (name: string): boolean => fts.some(base => name.startsWith(`${base}_`) && EXEMPTION_RULES.ftsShadowSuffixes.includes(name.slice(base.length + 1)));
	if (!tables.some(table => table.name === EXEMPTION_RULES.rowTable)) return false; // not a bank this tool can vouch for
	for (const { name } of tables) {
		if (isShadow(name) || name === EXEMPTION_RULES.derivedFts) continue;
		if (name === EXEMPTION_RULES.rowTable) {
			const rows = db.query(`SELECT source, trust_tier, metadata_json FROM ${quoted(name)}`).all() as { source: string | null; trust_tier: string | null; metadata_json: string | null }[];
			if (!rows.every(row => isImporterRow(row, parseMeta(row.metadata_json)))) return false;
			continue;
		}
		const refers = EXEMPTION_RULES.derivedTables[name];
		const strays = refers === undefined
			? `SELECT COUNT(*) AS n FROM ${quoted(name)}`
			: `SELECT COUNT(*) AS n FROM ${quoted(name)} WHERE ${quoted(refers)} IS NULL OR ${quoted(refers)} NOT IN (SELECT id FROM ${quoted(EXEMPTION_RULES.rowTable)})`;
		if ((db.query(strays).get() as { n: number }).n > 0) return false;
	}
	return true;
}

/** Whether the bank holds nothing but what this tool wrote (see `EXEMPTION_RULES`). Read-only; fails closed: any error is `false`. */
export function holdsOnlyImporterContent(banksDir: string, bank: string): boolean {
	try {
		const file = bankDbPath(banksDir, bank);
		for (const sidecar of [`${file}-wal`, `${file}-journal`]) if (existsSync(sidecar) && statSync(sidecar).size > 0) return false;
		if (!existsSync(file)) return true;
		const db = openReadonly(file);
		try {
			return holdsOnlyImporterContentIn(db);
		} finally {
			db.close();
		}
	} catch {
		return false;
	}
}

/** A row that carries this tool's provenance and a run id, as found in a bank (independent of the ledger). */
export interface RunRow {
	bank: string;
	id: string;
	runId: string;
	source: string;
	originKey: string;
	originHash: string;
	importedAt: string | null;
}

/** Every importer row (provably this tool's, with a run id) of every bank, or of run `only`. Unreadable banks are skipped. */
export function importerRows(banksDir: string, only?: string): RunRow[] {
	const found: RunRow[] = [];
	for (const bank of listBanks(banksDir)) {
		let db: Database;
		try {
			db = openReadonly(bankDbPath(banksDir, bank));
		} catch {
			continue;
		}
		try {
			const rows = db
				.query("SELECT id, source, trust_tier, metadata_json FROM working_memory WHERE json_valid(metadata_json) AND json_extract(metadata_json, '$.importRunId') IS NOT NULL")
				.all() as { id: string; source: string | null; trust_tier: string | null; metadata_json: string }[];
			for (const row of rows) {
				const meta = parseMeta(row.metadata_json);
				if (typeof meta.importRunId !== "string" || (only !== undefined && meta.importRunId !== only) || !isImporterRow(row, meta)) continue;
				found.push({
					bank,
					id: row.id,
					runId: meta.importRunId,
					source: String(meta.source),
					originKey: String(meta.originKey),
					originHash: String(meta.originHash),
					importedAt: typeof meta.importedAt === "string" ? meta.importedAt : null,
				});
			}
		} catch {
			// a bank that cannot be read has no rows to recover
		} finally {
			db.close();
		}
	}
	return found;
}
