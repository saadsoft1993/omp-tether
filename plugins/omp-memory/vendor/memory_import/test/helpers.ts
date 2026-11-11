/**
 * Scratch environments for the importer tests: a fake home, banks built with the real library schema, a fake omp.
 *
 * The library is the PUBLISHED package `@oh-my-pi/pi-mnemopi`, installed by the importer's own `setup` code into a
 * scratch install root (`$OMP_MEMORY_TEST_LIB_DIR`, else `<tmp>/omp-memory-test-lib/<version>`), once: that first run
 * needs the network. The version is `$OMP_MEMORY_TEST_VERSION` (default below). No omp source tree is involved.
 */
import { Database } from "bun:sqlite";
import { chmodSync, mkdirSync, mkdtempSync, realpathSync, rmSync, truncateSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import type { Embedder } from "../embedder";
import type { Options } from "../engine";
import { encodeProjectName } from "../folders";
import { DEFAULT_EMBEDDING_MODEL, libraryOptions, PARITY_REQUIREMENTS, requireLibrary, type Library, resolveLibrary } from "../lib";
import { runSetup } from "../setup";

/** The omp release the tests run the importer against: the library of exactly this version is installed once. */
export const TEST_OMP_VERSION = process.env.OMP_MEMORY_TEST_VERSION ?? "18.18.3";

export const testLibraryRoot = (): string =>
	process.env.OMP_MEMORY_TEST_LIB_DIR ?? path.join(tmpdir(), "omp-memory-test-lib", TEST_OMP_VERSION);

/** The scratch install root holding `@oh-my-pi/pi-mnemopi@TEST_OMP_VERSION`; installs it with `setup` when it is missing. */
export async function ensureTestLibrary(): Promise<string> {
	const root = testLibraryRoot();
	if (resolveLibrary(TEST_OMP_VERSION, { libDir: root }).location === null) {
		const report = await runSetup({
			ompBin: "omp",
			ompVersion: TEST_OMP_VERSION,
			libDir: root,
			dryRun: false,
			embeddings: false,
			say: () => {},
		});
		if (!report.ok) throw new Error(`test library setup failed: ${report.problems.join("; ")}`);
	}
	return root;
}

/** Deterministic 768-dimension vectors (the size of bge-base) from the text's hash: no model, no onnxruntime. */
export const fakeEmbedder: Embedder = {
	async embed(texts) {
		return texts.map(text => {
			const digest = new Bun.CryptoHasher("sha512").update(text).digest();
			return Array.from({ length: 768 }, (_, index) => ((digest[index % digest.length] ?? 0) - 128) / 128);
		});
	},
	close() {},
};

export interface Env {
	root: string;
	/** The install root of the library the tests use. */
	libDir: string;
	home: string;
	banksDir: string;
	ledger: string;
	lib: Library;
	fakeOmp: string;
	cleanup(): void;
	options(over?: Partial<Options>): Options;
	/** A folder that exists under the fake home; returns its realpath. */
	folder(name: string): string;
	/** `home/.claude/projects/<encoded cwd>/` with a session file carrying that cwd. */
	project(cwd: string, root?: string): string;
	memory(projectDir: string, file: string, front: Record<string, string>, body: string): string;
	bankOf(cwd: string): string;
	emptyBank(bank: string): string;
	db(bank: string): Database;
}

/**
 * A stand-in `omp`: prints its version and carries the SQL text the importer looks for. Without the SQL it is padded to
 * the size of a compiled binary, so that it is scanned (a small file without the SQL is taken for a launcher script).
 */
export function fakeOmpScript(file: string, version: string, withNeedles = true): void {
	// the SQL sits after `exit`, as plain text: the parity scan reads the file's bytes, the shell never reaches it
	const needles = withNeedles ? PARITY_REQUIREMENTS.map(requirement => requirement.sample).join("\n") : "";
	writeFileSync(file, `#!/bin/sh\necho "omp/${version}"\nexit 0\n${needles}\n`);
	if (!withNeedles) truncateSync(file, 6 * 1024 * 1024);
	chmodSync(file, 0o755);
}

export async function makeEnv(): Promise<Env> {
	const root = realpathSync(mkdtempSync(path.join(tmpdir(), "UnifyImporter-test-")));
	const home = path.join(root, "home");
	const banksDir = path.join(root, "banks");
	mkdirSync(home, { recursive: true });
	mkdirSync(banksDir, { recursive: true });
	const libDir = await ensureTestLibrary();
	const fakeOmp = path.join(root, "omp");
	fakeOmpScript(fakeOmp, TEST_OMP_VERSION);
	const { lib } = await requireLibrary({ libDir, ompBin: fakeOmp });
	const env: Env = {
		root,
		libDir,
		home,
		banksDir,
		ledger: path.join(root, "ledger.json"),
		lib,
		fakeOmp,
		cleanup: () => rmSync(root, { recursive: true, force: true }),
		options: (over = {}) => ({
			command: "dry-run",
			mode: "claude-memory",
			banksDir,
			ledgerFile: path.join(root, "ledger.json"),
			apply: false,
			runId: `imp-test-${Bun.randomUUIDv7().slice(-6)}`,
			forceClosed: true,
			// the apply contract needs one of each pair; tests of the plan and backup checks pass their own
			noPlanCheck: true,
			noBackupCheck: true,
			libDir,
			ompBin: fakeOmp,
			embedderFactory: () => fakeEmbedder,
			skipUnassigned: false,
			claudeHome: home,
			claudeMemDb: path.join(home, ".claude-mem", "claude-mem.db"),
			hindsightLedger: path.join(root, "hs-ledger.json"),
			...over,
		}),
		folder: name => {
			const dir = path.join(home, "work", name);
			mkdirSync(dir, { recursive: true });
			return realpathSync(dir);
		},
		project: (cwd, rootName = ".claude") => {
			const dir = path.join(home, rootName, "projects", encodeProjectName(cwd));
			mkdirSync(dir, { recursive: true });
			writeFileSync(path.join(dir, "11111111-1111-4111-8111-111111111111.jsonl"), `${JSON.stringify({ type: "user", cwd })}\n`);
			return dir;
		},
		memory: (projectDir, file, front, body) => {
			const memoryDir = path.join(projectDir, "memory");
			mkdirSync(memoryDir, { recursive: true });
			const head = Object.entries(front).map(([key, value]) => `${key}: ${value}`).join("\n");
			const target = path.join(memoryDir, file);
			writeFileSync(target, `---\n${head}\n---\n\n${body}\n`);
			return target;
		},
		bankOf: cwd => lib.projectBankSegment(cwd),
		emptyBank: bank => {
			const file = path.join(banksDir, bank, "mnemopi.db");
			const memory = new lib.Mnemopi(libraryOptions(file, bank, DEFAULT_EMBEDDING_MODEL));
			memory.close();
			return file;
		},
		db: bank => new Database(path.join(banksDir, bank, "mnemopi.db")),
	};
	return env;
}

/** A native-looking row (what omp's retain leaves before consolidation): old timestamp, consolidated_at NULL. */
export function insertNativeRow(db: Database, bank: string, id: string, content: string): void {
	db.run(
		`INSERT INTO working_memory (id, content, source, timestamp, session_id, importance, metadata_json, scope, veracity, memory_type, trust_tier, consolidated_at)
		 VALUES (?, ?, 'coding-agent-retain', '2020-01-01T00:00:00.000Z', ?, 0.75, '{"tool":"retain"}', 'bank', 'tool', 'fact', 'STATED', NULL)`,
		[id, content, bank],
	);
}

/**
 * A real second process that keeps `file` open until `close()`. It says "ready" once the database is open; the handle
 * is kept on globalThis because Bun closes a SQLite handle when it is garbage-collected.
 */
export async function holdOpen(file: string): Promise<{ pid: number; close(): void }> {
	const child = Bun.spawn(
		[
			process.execPath,
			"-e",
			`import {Database} from "bun:sqlite"; globalThis.held = new Database(${JSON.stringify(file)}); globalThis.held.query("SELECT 1").get(); console.log("ready"); setInterval(() => {}, 1000);`,
		],
		{ stdin: "ignore", stdout: "pipe", stderr: "ignore" },
	);
	const text = new TextDecoder().decode((await child.stdout.getReader().read()).value);
	if (!text.includes("ready")) throw new Error("holder did not start");
	return { pid: child.pid, close: () => child.kill() };
}
