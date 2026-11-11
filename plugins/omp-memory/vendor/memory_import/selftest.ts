/**
 * The behavioural self-test: before anything is written to a real bank, the importer performs its exact write sequence
 * through the LOADED library, in a scratch bank under the system temp directory, and checks what each step left behind:
 *
 *   restore insert -> annotation -> embedding (`applyBank`, the importer's own write path), then the row, its full-text
 *   entry, its annotations, its embedding row and its `consolidated_at`; then the trim exemption (a native row with an old
 *   timestamp is trimmed by the library's own `remember`, the imported rows survive); then `forgetRows`, after which no
 *   trace of the rows may remain.
 *
 * Any failed step refuses the write and names the step. The result is cached per library (version and a fingerprint of the
 * package files the importer depends on) and Bun version, in `<cache>/selftest/`: `setup` and `doctor --deep` run it,
 * `import --apply` and `undo --apply` run it when no passing result is cached, plain `doctor` only reads the cache.
 * A stand-in embedder (deterministic vectors, no model) runs it by default; `setup` runs it once with the real embedding
 * model, which proves the model path as well.
 */
import { createHash } from "node:crypto";
import { existsSync, lstatSync, mkdtempSync, readdirSync, readFileSync, readlinkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { applyBank, forgetRows, openReadonly, type RowToWrite } from "./banks";
import { createWorkerEmbedder, type Embedder } from "./embedder";
import { bankDbPath, DEFAULT_EMBEDDING_MODEL, type Library, libraryOptions, memoryCacheDir } from "./lib";
import { Refusal, writeJsonAtomic } from "./model";
import { scrubText, scrubValue } from "./sink";

/** Bump when the sequence below changes, so that cached results of an older sequence are not trusted. */
export const SELFTEST_VERSION = 2;

/** The omp releases the importer has been run against, version by version (see compat.json): a message, not a gate. */
export const VERIFIED_OMP_RANGE = { first: "18.18.0", last: "18.18.8" } as const;

export type EmbedderKind = "stub" | "real";

export interface SelfTestStep {
	step: string;
	ok: boolean;
	detail: string;
}

export interface SelfTestResult {
	ok: boolean;
	/** The first step that failed; null when everything passed. */
	failedStep: string | null;
	embedder: EmbedderKind;
	model: string;
	steps: SelfTestStep[];
	libraryVersion: string;
	fingerprint: string;
	bun: string;
	selfTestVersion: number;
	at: string;
	ms: number;
}

interface SelfTestRecord {
	schema: 1;
	runs: Partial<Record<EmbedderKind, SelfTestResult>>;
}

export interface SelfTestStatus {
	status: "passed" | "failed" | "not-run";
	/** The valid runs that are cached (a run made for another library, fingerprint, Bun or sequence is not valid). */
	runs: SelfTestResult[];
	failedStep: string | null;
	detail: string;
}

/**
 * A short hash of the whole installed package (every file below its directory except nested dependencies, which `setup`
 * pins by version), so that an edited package is never taken for the one that passed the self-test.
 */
export function libraryFingerprint(pkgDir: string): string {
	const hash = createHash("sha256");
	const files = (existsSync(pkgDir) ? readdirSync(pkgDir, { recursive: true, withFileTypes: true }) : [])
		.filter(entry => !entry.isDirectory())
		.map(entry => path.relative(pkgDir, path.join(entry.parentPath, entry.name)))
		.filter(file => !file.split(path.sep).includes("node_modules"))
		.sort();
	for (const file of files) {
		const full = path.join(pkgDir, file);
		const kind = lstatSync(full);
		hash.update(file).update("\0").update(kind.isSymbolicLink() ? `-> ${readlinkSync(full)}` : kind.isFile() ? readFileSync(full) : "other").update("\0");
	}
	return hash.digest("hex").slice(0, 16);
}

function recordFile(version: string, fingerprint: string, env: NodeJS.ProcessEnv): string {
	return path.join(memoryCacheDir(env), "selftest", `${version}-${fingerprint}.json`);
}

function readRecord(file: string): SelfTestRecord {
	try {
		const parsed = JSON.parse(readFileSync(file, "utf8")) as Partial<SelfTestRecord> | null; // our own cache file; checked below
		if (parsed?.schema === 1 && typeof parsed.runs === "object" && parsed.runs !== null) return { schema: 1, runs: parsed.runs };
	} catch {
		// no record, or a damaged one: treated as never run
	}
	return { schema: 1, runs: {} };
}

/** What is cached for a library, without running anything and without writing. */
export function readSelfTest(pkgDir: string, version: string, env: NodeJS.ProcessEnv = process.env): SelfTestStatus {
	const fingerprint = libraryFingerprint(pkgDir);
	const record = readRecord(recordFile(version, fingerprint, env));
	const runs = Object.values(record.runs).filter(
		(run): run is SelfTestResult =>
			run !== undefined &&
			run.libraryVersion === version &&
			run.fingerprint === fingerprint &&
			run.bun === Bun.version &&
			run.selfTestVersion === SELFTEST_VERSION,
	);
	const failed = runs.find(run => !run.ok);
	if (failed) {
		const step = failed.steps.find(entry => !entry.ok);
		return { status: "failed", runs, failedStep: failed.failedStep, detail: `step "${failed.failedStep}": ${step?.detail ?? "failed"}` };
	}
	if (runs.length === 0) return { status: "not-run", runs, failedStep: null, detail: "not yet verified" };
	return { status: "passed", runs, failedStep: null, detail: `passed (${runs.map(run => run.embedder).join(" and ")} embedder)` };
}

class StepFailed extends Error {}

const BANK = "selftest";
const TOKEN = "selftestuniquetoken";
const ROWS: RowToWrite[] = [
	{
		id: "5e1f7e57aaaaaaa1",
		content: `omp-memory self-test row one ${TOKEN}: a claude-code style memory file`,
		source: "claude-code",
		sourceKind: "memory",
		timestamp: "2020-01-02T03:04:05.000Z",
		importance: 0.6,
		memoryType: "fact",
		metadata: { source: "claude-code", originKey: "selftest/one", originHash: "h1", importRunId: "selftest", tool: "import" },
	},
	{
		id: "5e1f7e57aaaaaaa2",
		content: "omp-memory self-test row two: a claude-mem style session summary",
		source: "claude-mem",
		sourceKind: "summary",
		timestamp: "2021-06-07T08:09:10.000Z",
		importance: 0.5,
		memoryType: "episode",
		metadata: { source: "claude-mem", originKey: "selftest/two", originHash: "h2", importRunId: "selftest", tool: "import" },
	},
];

/** Deterministic 768-dimension vectors from the text's hash (the size of the default model): no model, no onnxruntime. */
export function stubEmbedder(): Embedder {
	return {
		async embed(texts) {
			return texts.map(text => {
				const digest = new Bun.CryptoHasher("sha512").update(text).digest();
				return Array.from({ length: 768 }, (_, index) => ((digest[index % digest.length] ?? 0) - 128) / 128);
			});
		},
		close() {},
	};
}

/** Runs the sequence. Never throws for a library problem: the failure is in the result. Writes nothing outside a temp directory. */
export async function runSelfTest(
	lib: Library,
	options: { embedder: Embedder; kind: EmbedderKind; model?: string },
): Promise<SelfTestResult> {
	const started = Date.now();
	const model = options.model ?? DEFAULT_EMBEDDING_MODEL;
	const steps: SelfTestStep[] = [];
	const dir = mkdtempSync(path.join(tmpdir(), "memory-import-selftest-"));
	const file = bankDbPath(dir, BANK);
	const ids = ROWS.map(row => row.id);
	const ok = (step: string, detail: string): void => {
		steps.push({ step, ok: true, detail });
	};
	const fail = (step: string, detail: string): never => {
		steps.push({ step, ok: false, detail });
		throw new StepFailed(detail);
	};
	let current = "open a scratch bank";
	try {
		// restore insert, annotation, embedding: the importer's own write path
		const applied = await applyBank(lib, dir, BANK, ROWS, { model, importedAt: new Date().toISOString(), embedder: options.embedder });
		if (applied.error !== null) fail(applied.stage ?? "restore insert", applied.error);

		current = "read back";
		let db = openReadonly(file);
		try {
			const rows = db
				.query("SELECT id, content, session_id, scope, trust_tier, consolidated_at, metadata_json FROM working_memory ORDER BY id")
				.all() as { id: string; content: string; session_id: string; scope: string; trust_tier: string; consolidated_at: string | null; metadata_json: string }[];
			const wrong =
				rows.length !== ROWS.length ||
				ROWS.some((row, index) => {
					const found = rows[index];
					return !found || found.id !== row.id || found.content !== row.content || found.session_id !== BANK || found.scope !== "bank" || found.trust_tier !== "IMPORTED" || !found.metadata_json.includes(row.metadata.originKey as string);
				});
			if (wrong) fail("restore insert", `the bank holds ${rows.length} row(s), or rows that differ from what was restored (content, session, scope, trust tier or metadata)`);
			ok("restore insert", `${rows.length} rows restored through the library's import path`);

			const noFts = ids.filter(id => db.query("SELECT 1 FROM fts_working WHERE id = ? LIMIT 1").get(id) === null);
			if (noFts.length > 0) fail("full-text entry", `${noFts.length} of ${ids.length} rows have no fts_working entry`);
			ok("full-text entry", "every row is in fts_working");

			for (const row of ROWS) {
				const found = (kind: string): string | undefined => {
					const annotation = db.query("SELECT value FROM annotations WHERE memory_id = ? AND kind = ? LIMIT 1").get(row.id, kind) as { value: string } | null;
					return annotation?.value;
				};
				if (found("occurred_on") !== row.timestamp.slice(0, 10)) fail("annotation", `row ${row.id} has no correct occurred_on annotation`);
				if (found("has_source") !== `import-${row.source}`) fail("annotation", `row ${row.id} has no correct has_source annotation`);
			}
			ok("annotation", "occurred_on and has_source on every row");

			for (const id of ids) {
				const stored = db.query("SELECT model, embedding_json FROM memory_embeddings WHERE memory_id = ?").get(id) as { model: string; embedding_json: string } | null;
				if (stored === null) fail("embedding", `row ${id} has no memory_embeddings row`);
				if (stored?.model !== model) fail("embedding", `row ${id} is embedded with ${stored?.model}, not ${model}`);
				const vector: unknown = JSON.parse(stored?.embedding_json ?? "null");
				if (!Array.isArray(vector) || vector.length === 0 || vector.some(value => typeof value !== "number" || !Number.isFinite(value))) {
					fail("embedding", `row ${id} has a stored vector that is not a list of numbers`);
				}
			}
			ok("embedding", `a ${model} vector for every row${options.kind === "real" ? " (made by the real embedding model)" : ""}`);

			const unconsolidated = rows.filter(row => row.consolidated_at === null).length;
			if (unconsolidated > 0) fail("consolidated_at", `${unconsolidated} of ${rows.length} restored rows have no consolidated_at`);
			ok("consolidated_at", "stamped on every restored row");
		} finally {
			db.close();
		}

		// the trim exemption: the library's own `remember` trims native rows older than the working-memory TTL
		current = "trim exemption";
		const memory = new lib.Mnemopi(libraryOptions(file, BANK, model, options.embedder));
		try {
			lib.withMnemopiRuntimeOptions(memory.runtimeOptions, () => {
				memory.beam.remember("self-test control row a", { timestamp: "2019-01-01T00:00:00.000Z" });
				memory.beam.remember("self-test control row b", { timestamp: "2019-01-02T00:00:00.000Z" });
			});
			await memory.flushExtractions();
		} finally {
			memory.close();
		}
		db = openReadonly(file);
		try {
			const kept = (db.query("SELECT COUNT(*) AS n FROM working_memory WHERE id IN (?, ?)").get(...ids) as { n: number }).n;
			const others = (db.query("SELECT COUNT(*) AS n FROM working_memory WHERE id NOT IN (?, ?)").get(...ids) as { n: number }).n;
			if (others > 0) fail("trim exemption", `the library did not trim old native rows (${others} left), so the exemption of imported rows could not be shown`);
			if (kept !== ids.length) fail("trim exemption", `the library's trim removed ${ids.length - kept} imported row(s)`);
			ok("trim exemption", "old native rows were trimmed, the restored rows were not");
		} finally {
			db.close();
		}

		// forget: the importer's undo path leaves nothing behind
		current = "forget";
		const removed = forgetRows(lib, dir, BANK, ids, model);
		if (removed.length !== ids.length) fail("forget", `forgetWorking removed ${removed.length} of ${ids.length} rows`);
		db = openReadonly(file);
		try {
			const marks = ids.map(() => "?").join(", ");
			const left = {
				"working_memory": `SELECT COUNT(*) AS n FROM working_memory WHERE id IN (${marks})`,
				fts_working: `SELECT COUNT(*) AS n FROM fts_working WHERE id IN (${marks})`,
				memory_embeddings: `SELECT COUNT(*) AS n FROM memory_embeddings WHERE memory_id IN (${marks})`,
				annotations: `SELECT COUNT(*) AS n FROM annotations WHERE memory_id IN (${marks})`,
			};
			for (const [table, sql] of Object.entries(left)) {
				const n = (db.query(sql).get(...ids) as { n: number }).n;
				if (n > 0) fail("forget", `${n} row(s) are still in ${table} after the delete`);
			}
			ok("forget", "the rows, their full-text entries, embeddings and annotations are gone");
		} finally {
			db.close();
		}
	} catch (error) {
		if (!(error instanceof StepFailed)) steps.push({ step: current, ok: false, detail: scrubText(String(error).slice(0, 300)) });
	} finally {
		rmSync(dir, { recursive: true, force: true });
	}
	const failed = steps.find(step => !step.ok);
	return {
		ok: failed === undefined,
		failedStep: failed?.step ?? null,
		embedder: options.kind,
		model,
		steps,
		libraryVersion: lib.version,
		fingerprint: libraryFingerprint(lib.pkgDir),
		bun: Bun.version,
		selfTestVersion: SELFTEST_VERSION,
		at: new Date().toISOString(),
		ms: Date.now() - started,
	};
}

function recordResult(result: SelfTestResult, env: NodeJS.ProcessEnv): void {
	const target = recordFile(result.libraryVersion, result.fingerprint, env);
	const record = readRecord(target);
	record.runs[result.embedder] = result;
	writeJsonAtomic(target, scrubValue(record), 0o600); // the cache holds step details (an exception's text): the label policy applies to what is persisted too
}

/**
 * Run the self-test and cache the result (always, for `setup` and `doctor --deep`), or use a cached pass (`reuse`, for the
 * write commands). A run that fails is cached too, so that plain `doctor` shows it.
 */
export async function ensureSelfTest(
	lib: Library,
	options: { reuse: boolean; kind?: EmbedderKind; embedder?: Embedder; model?: string; env?: NodeJS.ProcessEnv },
): Promise<{ result: SelfTestResult; cached: boolean }> {
	const env = options.env ?? process.env;
	const kind = options.kind ?? "stub";
	if (options.reuse) {
		const cached = readSelfTest(lib.pkgDir, lib.version, env);
		const run = cached.runs.find(entry => entry.ok);
		if (cached.status === "passed" && run) return { result: run, cached: true };
	}
	const embedder = options.embedder ?? (kind === "real" ? createWorkerEmbedder(lib.pkgDir, options.model ?? DEFAULT_EMBEDDING_MODEL) : stubEmbedder());
	let result: SelfTestResult;
	try {
		result = await runSelfTest(lib, { embedder, kind, model: options.model });
	} finally {
		embedder.close();
	}
	try {
		recordResult(result, env);
	} catch {
		// an unwritable cache directory only means the test runs again next time; the result still counts for this run
	}
	return { result, cached: false };
}

/** The refusal for a failed self-test: names the failing step, the versions, and what the importer was verified against. */
export function selfTestRefusal(result: SelfTestResult, ompVersion: string): Refusal {
	const step = result.steps.find(entry => !entry.ok);
	return new Refusal(
		`the library self-test failed at step "${result.failedStep}": ${step?.detail ?? "failed"} (omp ${ompVersion}, @oh-my-pi/pi-mnemopi ${result.libraryVersion}; this importer is verified against omp ${VERIFIED_OMP_RANGE.first} to ${VERIFIED_OMP_RANGE.last}). Nothing was written; \`omp-memory doctor --deep\` repeats the test`,
	);
}

/** Refuses (with the failing step) unless the library passes the self-test: a cached pass is enough, else it is run now. */
export async function requireSelfTest(lib: Library, ompVersion: string, env: NodeJS.ProcessEnv = process.env): Promise<SelfTestResult> {
	const { result } = await ensureSelfTest(lib, { reuse: true, env });
	if (!result.ok) throw selfTestRefusal(result, ompVersion);
	return result;
}
