/**
 * Write-path integrity: what happens to a bank around an import or an undo. Banks, ledgers and homes are scratch ones built
 * by helpers.ts; the only process started besides this one is a child that stands in for an omp session holding a bank open
 * or for an importer that is killed half way.
 */
import { Database } from "bun:sqlite";
import { afterAll, describe, expect, test } from "bun:test";
import { appendFileSync, chmodSync, cpSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { applyBank, bankOpenBy, readBankState, type RowToWrite } from "../banks";
import { buildOptions, parseArgs } from "../main";
import { requireBackupCoverage, runImport, runUndo, runVerify } from "../engine";
import { emptyLedger, loadLedger, saveLedger } from "../ledger";
import { assertBankWritable, DEFAULT_EMBEDDING_MODEL, inspectLibrary } from "../lib";
import { Refusal, UsageError } from "../model";
import { libraryFingerprint } from "../selftest";
import { type Env, fakeEmbedder, fakeOmpScript, holdOpen, insertNativeRow, makeEnv, TEST_OMP_VERSION } from "./helpers";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

const envs: Env[] = [];
afterAll(() => {
	for (const env of envs) env.cleanup();
});

interface Scenario {
	env: Env;
	cwd: string;
	bank: string;
	dir: string;
}

/** A project folder with `notes` memory notes. The bank exists (empty) unless `existing` is false. */
async function scenario(notes: number, existing = true): Promise<Scenario> {
	const env = await makeEnv();
	envs.push(env);
	const cwd = env.folder("work");
	const bank = env.bankOf(cwd);
	const dir = env.project(cwd);
	for (let index = 1; index <= notes; index += 1) {
		env.memory(dir, `n${index}.md`, { name: `Note ${index}`, type: "project" }, `Distinct body number ${index} for the write integrity tests.`);
	}
	if (existing) env.emptyBank(bank);
	return { env, cwd, bank, dir };
}

const apply = (env: Env, over: Partial<Parameters<Env["options"]>[0]> = {}) => env.options({ command: "import", apply: true, ...over });

const count = (env: Env, bank: string, sql = "SELECT COUNT(*) AS n FROM working_memory"): number => {
	const db = env.db(bank);
	try {
		return (db.query(sql).get() as { n: number }).n;
	} finally {
		db.close();
	}
};

const rowToWrite = (id: string, content: string): RowToWrite => ({
	id,
	content,
	source: "claude-code",
	sourceKind: "memory",
	timestamp: "2024-01-02T03:04:05.000Z",
	importance: 0.5,
	memoryType: "fact",
	metadata: { source: "claude-code", originKey: `direct/${id}`, originHash: "h", importRunId: "direct", tool: "import" },
});

const EVIL_TRIGGER = "CREATE TRIGGER evil AFTER INSERT ON working_memory BEGIN UPDATE working_memory SET content = content || ' (edited)' WHERE id <> new.id; END;";

describe("rows, annotations and embeddings are written in one transaction", () => {
	test("an importer killed while it embeds leaves the bank with no rows, or with rows that all have their embedding", async () => {
		const { env } = await scenario(0, false);
		const script = path.join(env.root, "crash.ts");
		writeFileSync(
			script,
			`import { applyBank } from ${JSON.stringify(path.join(import.meta.dir, "..", "banks"))};
import { loadLibrary, resolveLibrary } from ${JSON.stringify(path.join(import.meta.dir, "..", "lib"))};
const [libDir, banksDir, version] = process.argv.slice(2);
const location = resolveLibrary(version, { libDir }).location;
if (location === null) throw new Error("no library");
const lib = await loadLibrary(location);
const rows = [1, 2].map(n => ({ id: "c0ffee000000000" + n, content: "crash row " + n, source: "claude-code", sourceKind: "memory", timestamp: "2024-01-02T03:04:05.000Z", importance: 0.5, memoryType: "fact", metadata: { source: "claude-code", originKey: "k" + n, originHash: "h", importRunId: "crash", tool: "import" } }));
await applyBank(lib, banksDir, "crashbank", rows, { model: "BAAI/bge-base-en-v1.5", importedAt: new Date().toISOString(), embedder: { async embed() { process.exit(9); }, close() {} } });
`,
		);
		const ran = Bun.spawnSync([process.execPath, script, env.libDir, env.banksDir, TEST_OMP_VERSION], { stdout: "pipe", stderr: "pipe", env: process.env });
		expect(ran.exitCode).toBe(9); // the embedder was reached and the process ended there
		if (existsSync(path.join(env.banksDir, "crashbank", "mnemopi.db"))) {
			const rows = count(env, "crashbank");
			const embedded = count(env, "crashbank", "SELECT COUNT(*) AS n FROM memory_embeddings");
			expect(embedded).toBe(rows);
		}
	});

	test("an embedder that returns nothing writes nothing, and does not even create the bank", async () => {
		const { env } = await scenario(0, false);
		const result = await applyBank(env.lib, env.banksDir, "freshbank", [rowToWrite("c0ffee0000000011", "first")], {
			model: DEFAULT_EMBEDDING_MODEL,
			importedAt: new Date().toISOString(),
			embedder: { embed: async () => [], close() {} },
		});
		expect(result.error).toMatch(/no embedding/);
		expect(result.inserted).toEqual([]);
		expect(existsSync(path.join(env.banksDir, "freshbank", "mnemopi.db"))).toBe(false);
	});

	test("a vector that is not a list of numbers is refused before anything is written", async () => {
		const { env, bank } = await scenario(0);
		const result = await applyBank(env.lib, env.banksDir, bank, [rowToWrite("c0ffee0000000012", "second")], {
			model: DEFAULT_EMBEDDING_MODEL,
			importedAt: new Date().toISOString(),
			embedder: { embed: async () => [[Number.NaN, 1]], close() {} },
		});
		expect(result.error).toMatch(/no embedding/);
		expect(count(env, bank)).toBe(0);
		expect(count(env, bank, "SELECT COUNT(*) AS n FROM annotations")).toBe(0);
	});

	test("a good write leaves the rows, their annotations and their embeddings together", async () => {
		const { env, bank } = await scenario(2);
		const report = await runImport(apply(env));
		expect(report.counts.imported).toBe(2);
		expect(count(env, bank)).toBe(2);
		expect(count(env, bank, "SELECT COUNT(*) AS n FROM memory_embeddings")).toBe(2);
		expect(count(env, bank, "SELECT COUNT(*) AS n FROM annotations WHERE kind = 'occurred_on'")).toBe(2);
	});
});

describe("the bank's triggers and its native rows are checked", () => {
	test("an extra trigger that rewrites rows is a schema difference", async () => {
		const { env, bank } = await scenario(1);
		const db = env.db(bank);
		insertNativeRow(db, bank, "native-a", "A native note.");
		db.exec(EVIL_TRIGGER);
		db.close();
		const state = await readBankState(env.lib, env.banksDir, bank);
		expect(state.schemaProblems.join("; ")).toContain("unexpected trigger evil");
		await expect(runImport(apply(env))).rejects.toThrow(/schema differs/);
		expect(count(env, bank)).toBe(1);
	});

	test("a library trigger whose body was changed is a schema difference", async () => {
		const { env, bank } = await scenario(1);
		const db = env.db(bank);
		const original = (db.query("SELECT sql FROM sqlite_master WHERE name = 'wm_ai'").get() as { sql: string }).sql;
		const changed = original.replace(/\bEND\s*;?\s*$/i, "UPDATE working_memory SET importance = 0 WHERE id <> new.id; END");
		expect(changed).not.toBe(original);
		db.exec("DROP TRIGGER wm_ai");
		db.exec(changed);
		db.close();
		const state = await readBankState(env.lib, env.banksDir, bank);
		expect(state.schemaProblems.join("; ")).toContain("trigger wm_ai differs");
	});

	test("a view in the bank is a schema difference", async () => {
		const { env, bank } = await scenario(1);
		const db = env.db(bank);
		db.exec("CREATE VIEW sneaky AS SELECT id FROM working_memory");
		db.close();
		const state = await readBankState(env.lib, env.banksDir, bank);
		expect(state.schemaProblems.join("; ")).toContain("unexpected view sneaky");
	});

	test("a write that changes a native row is rolled back, even when the row count is right", async () => {
		const { env, bank } = await scenario(0);
		const db = env.db(bank);
		insertNativeRow(db, bank, "native-b", "A native note that must stay as it is.");
		db.exec(EVIL_TRIGGER);
		db.close();
		const result = await applyBank(env.lib, env.banksDir, bank, [rowToWrite("c0ffee0000000021", "an imported row")], {
			model: DEFAULT_EMBEDDING_MODEL,
			importedAt: new Date().toISOString(),
			embedder: fakeEmbedder,
		});
		expect(result.error).toMatch(/other rows changed/);
		expect(result.rolledBack).toBe(true);
		expect(count(env, bank)).toBe(1);
		const check = env.db(bank);
		try {
			expect(check.query("SELECT content FROM working_memory WHERE id = 'native-b'").get()).toEqual({ content: "A native note that must stay as it is." });
		} finally {
			check.close();
		}
	});

	test("undo gets the same schema check as an import: a bank an import would refuse is not touched", async () => {
		const { env, bank } = await scenario(1);
		const runId = "imp-test-undo-schema";
		await runImport(apply(env, { runId }));
		expect(count(env, bank)).toBe(1);
		const db = env.db(bank);
		db.exec(EVIL_TRIGGER);
		db.close();
		await expect(runUndo(env.options({ command: "undo", targetRun: runId, apply: true }))).rejects.toThrow(/schema differs/);
		expect(count(env, bank)).toBe(1);
	});
});

describe("bank directories and databases that are symbolic links are refused", () => {
	test("a bank directory that is a link to another bank is not written through", async () => {
		const { env, bank } = await scenario(1, false);
		env.emptyBank("victim");
		symlinkSync(path.join(env.banksDir, "victim"), path.join(env.banksDir, bank));
		await expect(runImport(apply(env))).rejects.toThrow(/symbolic link/);
		expect(count(env, "victim")).toBe(0);
	});

	test("a database file that is a link to another bank's database is not written through", async () => {
		const { env, bank } = await scenario(1, false);
		env.emptyBank("victim");
		mkdirSync(path.join(env.banksDir, bank));
		symlinkSync(path.join(env.banksDir, "victim", "mnemopi.db"), path.join(env.banksDir, bank, "mnemopi.db"));
		await expect(runImport(apply(env))).rejects.toThrow(/symbolic link/);
		expect(count(env, "victim")).toBe(0);
	});

	test("a dangling link where a new bank would be created is refused", async () => {
		const { env, bank } = await scenario(1, false);
		symlinkSync(path.join(env.root, "elsewhere"), path.join(env.banksDir, bank));
		await expect(runImport(apply(env))).rejects.toThrow(/symbolic link/);
		expect(existsSync(path.join(env.root, "elsewhere"))).toBe(false);
	});

	test("undo does not delete through a bank directory that became a link", async () => {
		const { env, bank } = await scenario(1);
		const runId = "imp-test-undo-link";
		await runImport(apply(env, { runId }));
		renameSync(path.join(env.banksDir, bank), path.join(env.banksDir, "moved"));
		symlinkSync(path.join(env.banksDir, "moved"), path.join(env.banksDir, bank));
		await expect(runUndo(env.options({ command: "undo", targetRun: runId, apply: true }))).rejects.toThrow(/symbolic link/);
		expect(count(env, "moved")).toBe(1);
	});

	test("a banks directory that is itself a link, as the user gave it, still works", async () => {
		const { env, bank } = await scenario(1);
		const alias = path.join(env.root, "banks-alias");
		symlinkSync(env.banksDir, alias);
		const report = await runImport(apply(env, { banksDir: alias }));
		expect(report.counts.imported).toBe(1);
		expect(count(env, bank)).toBe(1);
	});
});

describe("a flag given twice is a usage error", () => {
	test("value flags, inline or not, and boolean flags", () => {
		expect(() => parseArgs(["dry-run", "--out", "a.json", "--out", "b.json"])).toThrow(UsageError);
		expect(() => parseArgs(["dry-run", "--banks-dir=/x", "--banks-dir", "/y"])).toThrow(/more than once/);
		expect(() => parseArgs(["import", "--apply", "--apply"])).toThrow(UsageError);
		expect(parseArgs(["import", "--apply", "--out", "a.json"]).flags.get("out")).toBe("a.json");
	});

	test("a boolean flag takes no value: --apply=false is not a way to turn it off", () => {
		expect(() => parseArgs(["undo", "--run", "x", "--apply=false"])).toThrow(UsageError);
		expect(() => parseArgs(["import", "--force-closed=0"])).toThrow(/takes no value/);
		expect(parseArgs(["import", "--apply", "--force-closed"]).flags.get("force-closed")).toBe(true);
	});
});

describe("importer operations are serialised and the open-bank check is repeated before each write", () => {
	const lockOf = (env: Env): string => `${env.ledger}.lock`;

	test("a lock held by a live process refuses the import; nothing is written", async () => {
		const { env, bank } = await scenario(1);
		writeFileSync(lockOf(env), `${JSON.stringify({ pid: process.pid, token: "x", startedAt: new Date().toISOString() })}\n`);
		await expect(runImport(apply(env))).rejects.toThrow(/lock/);
		expect(count(env, bank)).toBe(0);
		expect(existsSync(lockOf(env))).toBe(true); // someone else's lock is left alone
	});

	test("a lock whose process is gone is stale: it is replaced, and removed again when the run ends", async () => {
		const { env, bank } = await scenario(1);
		const child = Bun.spawn([process.execPath, "-e", "0"], { stdout: "ignore", stderr: "ignore" });
		await child.exited;
		writeFileSync(lockOf(env), `${JSON.stringify({ pid: child.pid, token: "old", startedAt: "2020-01-01T00:00:00.000Z" })}\n`);
		const report = await runImport(apply(env));
		expect(report.counts.imported).toBe(1);
		expect(count(env, bank)).toBe(1);
		expect(existsSync(lockOf(env))).toBe(false);
	});

	test("two operations at once: one runs, the other is refused, and the ledger holds each item once", async () => {
		const { env, bank } = await scenario(2);
		const results = await Promise.allSettled([runImport(apply(env, { runId: "imp-test-race-a" })), runImport(apply(env, { runId: "imp-test-race-b" }))]);
		expect(results.filter(result => result.status === "fulfilled")).toHaveLength(1);
		const refused = results.find(result => result.status === "rejected");
		expect(refused?.status === "rejected" ? String(refused.reason) : "").toMatch(/lock/);
		expect(count(env, bank)).toBe(2);
		expect(Object.keys(loadLedger(env.ledger).entries)).toHaveLength(2);
		expect(existsSync(lockOf(env))).toBe(false);
	});

	test("the lock is released when the run is refused", async () => {
		const { env, bank } = await scenario(1);
		const db = env.db(bank);
		db.exec(EVIL_TRIGGER);
		db.close();
		await expect(runImport(apply(env))).rejects.toThrow(/schema differs/);
		expect(existsSync(lockOf(env))).toBe(false);
	});

	test("a bank that is opened after the first bank was written is not written", async () => {
		const env = await makeEnv();
		envs.push(env);
		const folders = [env.folder("one"), env.folder("two")] as [string, string];
		const banks = folders.map(folder => env.bankOf(folder)) as [string, string];
		for (const [index, folder] of folders.entries()) env.memory(env.project(folder), "n.md", { name: `Note ${index}` }, `Body of the note in folder ${index}.`);
		for (const bank of banks) env.emptyBank(bank);
		const order = [...banks].sort();
		const second = order[1] as string;
		const holders: { pid: number; close(): void }[] = [];
		let made = 0;
		try {
			const report = await runImport(
				apply(env, {
					forceClosed: false,
					embedderFactory: () => {
						made += 1;
						const mine = made;
						return {
							async embed(texts) {
								// an omp session opens the second bank while the importer is busy with it
								if (mine === 2) holders.push(await holdOpen(path.join(env.banksDir, second, "mnemopi.db")));
								return fakeEmbedder.embed(texts);
							},
							close() {},
						};
					},
				}),
			);
			expect(report.counts.imported).toBe(1);
			expect(report.errors.some(item => item.where === `bank ${second}` && /open in process/.test(item.error))).toBe(true);
		} finally {
			for (const held of holders) held.close();
		}
		expect(count(env, order[0] as string)).toBe(1);
		expect(count(env, second)).toBe(0);
	});
});

describe("run ids are not reused and undo only removes what it can prove is its own", () => {
	test("a run id that is in the ledger cannot be used for another import", async () => {
		const { env, dir } = await scenario(1);
		await runImport(apply(env, { runId: "imp-test-reuse" }));
		env.memory(dir, "later.md", { name: "Later note" }, "A note that appears after the first run.");
		await expect(runImport(apply(env, { runId: "imp-test-reuse" }))).rejects.toThrow(UsageError);
		expect(Object.keys(loadLedger(env.ledger).entries)).toHaveLength(1);
	});

	test("a run id that is on rows in a bank cannot be used, even when the ledger is gone", async () => {
		const { env, bank, dir } = await scenario(1);
		await runImport(apply(env, { runId: "imp-test-reuse2" }));
		writeFileSync(env.ledger, JSON.stringify({ version: 1, entries: {}, runs: {}, banksCreated: {} }));
		env.memory(dir, "later.md", { name: "Later note" }, "Another note that appears after the first run.");
		await expect(runImport(apply(env, { runId: "imp-test-reuse2" }))).rejects.toThrow(/run id/);
		expect(count(env, bank)).toBe(1);
	});

	test("a row that carries the run id but is not an importer row is left in place and reported", async () => {
		const { env, bank } = await scenario(1);
		const runId = "imp-test-foreign";
		await runImport(apply(env, { runId }));
		const db = env.db(bank);
		insertNativeRow(db, bank, "native-c", "A native row that someone tagged with the run id.");
		db.run("UPDATE working_memory SET metadata_json = json_object('importRunId', ?, 'originKey', 'x', 'originHash', 'y', 'source', 'claude-code', 'tool', 'import') WHERE id = 'native-c'", [runId]);
		db.close();
		const report = await runUndo(env.options({ command: "undo", targetRun: runId, apply: true }));
		expect(report.removed).toEqual([{ bank: expect.any(String), rows: 1 }]);
		expect(report.problems.join("; ")).toMatch(/not written by the importer/);
		expect(count(env, bank, "SELECT COUNT(*) AS n FROM working_memory WHERE id = 'native-c'")).toBe(1);
	});

	test("a row the library does not delete stays in the ledger and is reported", async () => {
		const { env, bank } = await scenario(2);
		const runId = "imp-test-survivor";
		await runImport(apply(env, { runId }));
		const db = env.db(bank);
		// the library's delete only removes a row of the bank's own session
		db.run("UPDATE working_memory SET session_id = 'another-session' WHERE id = (SELECT id FROM working_memory ORDER BY id LIMIT 1)");
		db.close();
		const report = await runUndo(env.options({ command: "undo", targetRun: runId, apply: true }));
		expect(report.removed).toEqual([{ bank: expect.any(String), rows: 1 }]);
		expect(report.problems.join("; ")).toMatch(/still present/);
		const ledger = loadLedger(env.ledger);
		expect(Object.values(ledger.entries).filter(entry => entry.state === "imported")).toHaveLength(1);
		expect(ledger.runs[runId]?.undoneAt).toBeUndefined();
		expect(count(env, bank)).toBe(1);
	});
});

describe("verify compares the rows it expects with the rows it finds, and a source it cannot read is a failure", () => {
	test("a source file that cannot be used makes verify fail", async () => {
		const { env, bank, dir } = await scenario(1);
		const runId = "imp-test-verify-source";
		await runImport(apply(env, { runId }));
		expect((await runVerify(env.options({ command: "verify", targetRun: runId }))).ok).toBe(true);
		writeFileSync(path.join(dir, "memory", "empty.md"), "");
		const report = await runVerify(env.options({ command: "verify", targetRun: runId }));
		expect(report.parseErrors).toHaveLength(1);
		expect(report.ok).toBe(false);
		expect(report.problems.join("; ")).toMatch(/source/);
		expect(count(env, bank)).toBe(1);
	});

	test("a row of the run that is not the row the ledger names fails verify, although the counts agree", async () => {
		const { env } = await scenario(1);
		const runId = "imp-test-verify-identity";
		await runImport(apply(env, { runId }));
		const ledger = loadLedger(env.ledger);
		for (const entry of Object.values(ledger.entries)) entry.rowId = "0123456789abcdef";
		saveLedger(env.ledger, ledger);
		const report = await runVerify(env.options({ command: "verify", targetRun: runId }));
		expect(report.ok).toBe(false);
		expect(report.banks[0]).toMatchObject({ expected: 1, actual: 1, missingRows: 1, unexpectedRows: 1 });
	});

	test("a row whose provenance differs from the ledger's entry fails verify", async () => {
		const { env, bank } = await scenario(1);
		const runId = "imp-test-verify-provenance";
		await runImport(apply(env, { runId }));
		const db = env.db(bank);
		db.run("UPDATE working_memory SET metadata_json = json_set(metadata_json, '$.originHash', 'changed') WHERE json_extract(metadata_json, '$.importRunId') = ?", [runId]);
		db.close();
		const report = await runVerify(env.options({ command: "verify", targetRun: runId }));
		expect(report.ok).toBe(false);
		expect(report.banks[0]?.provenanceMismatch).toBe(1);
	});

	test("an untouched run still verifies", async () => {
		const { env } = await scenario(2);
		const runId = "imp-test-verify-ok";
		await runImport(apply(env, { runId }));
		const report = await runVerify(env.options({ command: "verify", targetRun: runId }));
		expect(report.ok).toBe(true);
		expect(readFileSync(env.ledger, "utf8")).toContain(runId);
	});
});

describe("an lsof that fails does not read as a bank nobody has open", () => {
	const withFakeLsof = (env: Env, body: string, run: () => void): void => {
		const bin = path.join(env.root, `bin-${Bun.randomUUIDv7().slice(-6)}`);
		mkdirSync(bin);
		const fake = path.join(bin, "lsof");
		writeFileSync(fake, `#!/bin/sh\n${body}\n`);
		chmodSync(fake, 0o755);
		const saved = process.env.PATH;
		process.env.PATH = `${bin}:${saved ?? ""}`;
		try {
			run();
		} finally {
			process.env.PATH = saved;
		}
	};

	test("the stand-in lsof is the one that is used (a pid on its output is a holder)", async () => {
		const { env, bank } = await scenario(0);
		withFakeLsof(env, "echo 4242", () => expect(bankOpenBy(env.banksDir, bank)).toEqual(["4242"]));
	});

	test("exit status 1 with a message on stderr is unknown, not free; exit status 1 with nothing to say is free", async () => {
		const { env, bank } = await scenario(0);
		withFakeLsof(env, "echo 'lsof: cannot read the process table' >&2\nexit 1", () => expect(bankOpenBy(env.banksDir, bank)).toBeNull());
		withFakeLsof(env, "exit 1", () => expect(bankOpenBy(env.banksDir, bank)).toEqual([]));
	});
});

describe("the self-test cache covers the whole package, and an unscanned omp is not claimed to match", () => {
	test("a change anywhere in the package changes the fingerprint", async () => {
		const { env } = await scenario(0);
		const copy = path.join(env.root, "package-copy");
		cpSync(env.lib.pkgDir, copy, { recursive: true });
		const before = libraryFingerprint(copy);
		expect(libraryFingerprint(copy)).toBe(before);
		appendFileSync(path.join(copy, "src", "core", "memory.ts"), "\n// edited\n");
		expect(libraryFingerprint(copy)).not.toBe(before);
	});

	test("an omp whose SQL was not scanned gives a warning and no parity claim; a scanned one gives neither", async () => {
		const { env } = await scenario(0);
		const launcher = path.join(env.root, "omp-launcher");
		writeFileSync(launcher, `#!/bin/sh\necho "omp/${TEST_OMP_VERSION}"\n`);
		chmodSync(launcher, 0o755);
		const unscanned = await inspectLibrary({ libDir: env.libDir, ompBin: launcher });
		expect(unscanned.check.ok).toBe(true);
		expect(unscanned.check.parity?.ompBinary).toContain("not scanned");
		expect(unscanned.check.parity?.ompParityVerified).toBe(false);
		expect(unscanned.check.warnings.join("; ")).toContain("not verified");
		const scanned = await inspectLibrary({ libDir: env.libDir, ompBin: env.fakeOmp });
		expect(scanned.check.parity?.ompParityVerified).toBe(true);
		expect(scanned.check.warnings).toEqual([]);
	});
});

describe("the run id is announced before the first write", () => {
	test("the command line prints the run id and the undo command on stderr before it does anything", async () => {
		const { env, bank } = await scenario(1);
		const db = env.db(bank);
		db.exec(EVIL_TRIGGER); // the run stops at the schema check, after the announcement
		db.close();
		const cli = path.join(import.meta.dir, "..", "cli.ts");
		const ran = Bun.spawnSync(
			[process.execPath, cli, "import", "--apply", "--no-plan-check", "--no-backup-check", "--run-id", "imp-test-announce", "--banks-dir", env.banksDir, "--ledger", env.ledger, "--lib-dir", env.libDir, "--omp-bin", env.fakeOmp, "--claude-home", env.home, "--force-closed"],
			{ stdout: "pipe", stderr: "pipe", env: process.env },
		);
		expect(ran.exitCode).toBe(1);
		const err = ran.stderr.toString();
		expect(err).toContain("imp-test-announce");
		expect(err).toContain("undo --run imp-test-announce --apply");
	});
});

describe("the dry-run report carries a digest of the plan", () => {
	const digestOf = async (env: Env): Promise<string | undefined> => (await runImport(env.options({ command: "dry-run" }))).planDigest;

	test("it is the same for two dry runs of the same tree, holds no text, and differs when the plan differs", async () => {
		const { env, dir } = await scenario(2);
		const first = await digestOf(env);
		expect(first).toMatch(/^[0-9a-f]{64}$/);
		expect(await digestOf(env)).toBe(first);

		const added = env.memory(dir, "added.md", { name: "Added" }, "A third note that was added after the first dry run.");
		const withAdded = await digestOf(env);
		expect(withAdded).not.toBe(first);

		writeFileSync(added, "---\nname: Added\n---\n\nThe third note, now edited.\n");
		const edited = await digestOf(env);
		expect(edited).not.toBe(withAdded);

		rmSync(added);
		expect(await digestOf(env)).toBe(first);

		env.memory(dir, "pw.md", { name: "Held" }, `Set ${"pass" + "word"}: Ab1! in the deploy script.`);
		const held = await runImport(env.options({ command: "dry-run" }));
		expect(held.counts.skippedHeld).toBeGreaterThan(0); // the item is held, not imported
		expect(held.planDigest).not.toBe(first);
	});

	test("a run id, an output file or the time do not change it; an import report has none", async () => {
		const { env } = await scenario(1);
		const a = await runImport(env.options({ command: "dry-run", runId: "imp-test-digest-a" }));
		const b = await runImport(env.options({ command: "dry-run", runId: "imp-test-digest-b", out: path.join(env.root, "plan.json") }));
		expect(a.planDigest).toBe(b.planDigest);
		const applied = await runImport(apply(env));
		expect(applied.planDigest).toBeUndefined();
	});
});

const bytesOf = (env: Env, bank: string): Buffer => readFileSync(path.join(env.banksDir, bank, "mnemopi.db"));

/** Two folders, each with one note, each with a bank that exists already (empty). */
async function twoExisting(): Promise<{ env: Env; banks: [string, string] }> {
	const env = await makeEnv();
	envs.push(env);
	const folders = [env.folder("ra"), env.folder("rb")] as [string, string];
	const banks = folders.map(folder => env.bankOf(folder)) as [string, string];
	for (const [index, folder] of folders.entries()) env.memory(env.project(folder), "n.md", { name: `Note ${index}` }, `Body of the note ${index} in the plan and backup tests.`);
	for (const bank of banks) env.emptyBank(bank);
	return { env, banks };
}

describe("the plan that runs is the plan that was approved, and existing banks need a backup that covers them", () => {
	const lockOf = (env: Env): string => `${env.ledger}.lock`;

	test("an apply without one of each pair of flags is a usage error, and so is giving both", async () => {
		const { env, bank } = await scenario(1);
		const bare = { noPlanCheck: false, noBackupCheck: false };
		const digest = "a".repeat(64);
		for (const over of [
			bare,
			{ ...bare, expectPlanDigest: digest }, // no backup flag
			{ ...bare, backedUpBanks: [bank] }, // no plan flag
			{ noPlanCheck: true, expectPlanDigest: digest, noBackupCheck: true },
			{ noPlanCheck: true, noBackupCheck: true, backedUpBanks: [] },
		]) {
			await expect(runImport(apply(env, over))).rejects.toThrow(UsageError);
		}
		await expect(runImport(apply(env, { noPlanCheck: false, expectPlanDigest: "XYZ", noBackupCheck: true }))).rejects.toThrow(/64 hex/);
		expect(count(env, bank)).toBe(0);
		expect(existsSync(lockOf(env))).toBe(false);
	});

	test("the command line gives exit status 2 without them, and reads an empty --backed-up-banks as no banks", async () => {
		const { env } = await scenario(1);
		const cli = path.join(import.meta.dir, "..", "cli.ts");
		const common = ["--banks-dir", env.banksDir, "--ledger", env.ledger, "--lib-dir", env.libDir, "--omp-bin", env.fakeOmp, "--claude-home", env.home, "--force-closed"];
		const ran = Bun.spawnSync([process.execPath, cli, "import", "--apply", ...common], { stdout: "pipe", stderr: "pipe", env: process.env });
		expect(ran.exitCode).toBe(2);
		expect(ran.stderr.toString()).toContain("exactly one of");
		const flags = parseArgs(["import", "--apply", "--backed-up-banks=", "--expect-plan-digest", "b".repeat(64), "--banks-dir", env.banksDir, "--ledger", env.ledger]).flags;
		const options = buildOptions("import", flags);
		expect(options.backedUpBanks).toEqual([]);
		expect(options.expectPlanDigest).toBe("b".repeat(64));
		expect(options.noPlanCheck).toBe(false);
		expect(buildOptions("import", parseArgs(["import", "--apply", "--backed-up-banks", "a, b", "--banks-dir", env.banksDir, "--ledger", env.ledger]).flags).backedUpBanks).toEqual(["a", "b"]);
		expect(() => parseArgs(["import", "--no-plan-check=1"])).toThrow(UsageError);
	});

	test("the escapes are said on stderr before anything runs", async () => {
		const { env, bank } = await scenario(1);
		const db = env.db(bank);
		db.exec(EVIL_TRIGGER); // the run stops at the schema check, after the notices
		db.close();
		const cli = path.join(import.meta.dir, "..", "cli.ts");
		const ran = Bun.spawnSync(
			[process.execPath, cli, "import", "--apply", "--no-plan-check", "--no-backup-check", "--banks-dir", env.banksDir, "--ledger", env.ledger, "--lib-dir", env.libDir, "--omp-bin", env.fakeOmp, "--claude-home", env.home, "--force-closed"],
			{ stdout: "pipe", stderr: "pipe", env: process.env },
		);
		expect(ran.exitCode).toBe(1);
		expect(ran.stderr.toString()).toContain("--no-plan-check");
		expect(ran.stderr.toString()).toContain("--no-backup-check");
	});

	test("the dry run's digest lets the same tree through, with the backup list naming the bank", async () => {
		const { env, bank } = await scenario(2);
		const plan = await runImport(env.options({ command: "dry-run" }));
		const report = await runImport(apply(env, { noPlanCheck: false, expectPlanDigest: plan.planDigest, noBackupCheck: false, backedUpBanks: [bank] }));
		expect(report.counts.imported).toBe(2);
		expect(report.counts.errors).toBe(0);
	});

	test("a source changed between the dry run and the apply is refused before any write", async () => {
		const { env, bank, dir } = await scenario(1);
		const plan = await runImport(env.options({ command: "dry-run" }));
		const before = bytesOf(env, bank);
		writeFileSync(path.join(dir, "memory", "n1.md"), "---\nname: Note 1\ntype: project\n---\n\nThe note was edited after the dry run.\n");
		const attempt = runImport(apply(env, { noPlanCheck: false, expectPlanDigest: plan.planDigest, noBackupCheck: true }));
		await expect(attempt).rejects.toThrow(Refusal);
		await expect(attempt).rejects.toThrow(/plan changed since the dry run/);
		expect(bytesOf(env, bank).equals(before)).toBe(true);
		expect(count(env, bank)).toBe(0);
		expect(existsSync(env.ledger)).toBe(false);
		expect(existsSync(lockOf(env))).toBe(false);
	});

	test("a source added after the dry run is refused too", async () => {
		const { env, bank, dir } = await scenario(1);
		const plan = await runImport(env.options({ command: "dry-run" }));
		env.memory(dir, "late.md", { name: "Late" }, "A note that appeared after the dry run.");
		await expect(runImport(apply(env, { noPlanCheck: false, expectPlanDigest: plan.planDigest, noBackupCheck: true }))).rejects.toThrow(/plan changed/);
		expect(count(env, bank)).toBe(0);
	});

	test("an existing bank that the backup list does not name is refused on its own; the others are written", async () => {
		const { env, banks } = await twoExisting();
		const [covered, uncovered] = banks;
		const beforeUncovered = bytesOf(env, uncovered);
		const report = await runImport(apply(env, { noBackupCheck: false, backedUpBanks: [covered] }));
		expect(report.counts.imported).toBe(1);
		expect(report.counts.errors).toBe(1);
		expect(report.errors.find(item => item.where === `bank ${uncovered}`)?.error).toContain("is not covered by a verified backup");
		expect(count(env, covered)).toBe(1);
		expect(count(env, uncovered)).toBe(0);
		expect(bytesOf(env, uncovered).equals(beforeUncovered)).toBe(true);
		expect(Object.values(loadLedger(env.ledger).entries).every(entry => entry.bank === covered)).toBe(true);
	});

	test("a bank that did not exist when the run started but exists when its turn comes is refused", async () => {
		const env = await makeEnv();
		envs.push(env);
		const folders = [env.folder("na"), env.folder("nb")] as [string, string];
		const banks = folders.map(folder => env.bankOf(folder)) as [string, string];
		for (const [index, folder] of folders.entries()) env.memory(env.project(folder), "n.md", { name: `N${index}` }, `Body of the new-bank note ${index}.`);
		const [first, second] = [...banks].sort() as [string, string];
		let made = 0;
		const report = await runImport(
			apply(env, {
				noBackupCheck: false,
				backedUpBanks: [],
				embedderFactory: () => {
					made += 1;
					const mine = made;
					return {
						async embed(texts) {
							if (mine === 1) env.emptyBank(second); // omp makes the second bank while the first is being written
							return fakeEmbedder.embed(texts);
						},
						close() {},
					};
				},
			}),
		);
		expect(count(env, first)).toBe(1); // new when the run started and still new when it was written
		expect(report.errors.find(item => item.where === `bank ${second}`)?.error).toContain("is not covered by a verified backup");
		expect(count(env, second)).toBe(0);
	});

	test("--no-backup-check writes an existing bank that no list names", async () => {
		const { env, banks } = await twoExisting();
		const report = await runImport(apply(env, { noBackupCheck: true, backedUpBanks: undefined }));
		expect(report.counts.imported).toBe(2);
		for (const bank of banks) expect(count(env, bank)).toBe(1);
	});
});

describe("the repeated checks run before the library opens the bank for writing", () => {
	test("a refusal from the pre-write check leaves the file as it was and the library is never constructed", async () => {
		const { env, bank } = await scenario(0);
		const file = path.join(env.banksDir, bank, "mnemopi.db");
		const before = { bytes: readFileSync(file), mtime: statSync(file).mtimeMs, size: statSync(file).size };
		let constructed = 0;
		class Counted extends env.lib.Mnemopi {
			constructor(options: Record<string, unknown>) {
				constructed += 1;
				super(options);
			}
		}
		const result = await applyBank({ ...env.lib, Mnemopi: Counted }, env.banksDir, bank, [rowToWrite("c0ffee0000000031", "never written")], {
			model: DEFAULT_EMBEDDING_MODEL,
			importedAt: new Date().toISOString(),
			embedder: fakeEmbedder,
			beforeWrite: () => {
				throw new Refusal("the bank became unusable while the embeddings were made");
			},
		});
		expect(result.error).toContain("became unusable");
		expect(constructed).toBe(0);
		const after = statSync(file);
		expect(after.mtimeMs).toBe(before.mtime);
		expect(after.size).toBe(before.size);
		expect(readFileSync(file).equals(before.bytes)).toBe(true);
	});

	test("a bank that another process opens while the embeddings are made is refused, untouched", async () => {
		const { env, banks } = await twoExisting();
		const target = [...banks].sort()[0] as string;
		const file = path.join(env.banksDir, target, "mnemopi.db");
		const holders: { close(): void }[] = [];
		let seen: { bytes: Buffer; mtime: number } | null = null;
		try {
			const report = await runImport(
				apply(env, {
					forceClosed: false,
					banks: [target],
					embedderFactory: () => ({
						async embed(texts) {
							holders.push(await holdOpen(file));
							seen = { bytes: readFileSync(file), mtime: statSync(file).mtimeMs };
							return fakeEmbedder.embed(texts);
						},
						close() {},
					}),
				}),
			);
			expect(report.errors.some(item => item.where === `bank ${target}` && /open in process/.test(item.error))).toBe(true);
		} finally {
			for (const held of holders) held.close();
		}
		expect(seen).not.toBeNull();
		expect(readFileSync(file).equals((seen as unknown as { bytes: Buffer }).bytes)).toBe(true);
		expect(statSync(file).mtimeMs).toBe((seen as unknown as { mtime: number }).mtime);
	});
});

describe("the importer lock cannot be taken by two processes, also when it is stale", () => {
	const hashOf = (raw: string): string => new Bun.CryptoHasher("sha256").update(raw).digest("hex").slice(0, 32);

	const deadPid = async (): Promise<number> => {
		const child = Bun.spawn([process.execPath, "-e", "0"], { stdout: "ignore", stderr: "ignore" });
		await child.exited;
		return child.pid;
	};

	test("a stale lock that another process is reclaiming (its marker exists) is not taken over", async () => {
		const { env, bank } = await scenario(1);
		const lock = `${env.ledger}.lock`;
		const raw = `${JSON.stringify({ pid: await deadPid(), token: "gone", startedAt: "2020-01-01T00:00:00.000Z" })}\n`;
		writeFileSync(lock, raw);
		writeFileSync(`${lock}.reclaimed-${hashOf(raw)}`, "");
		await expect(runImport(apply(env))).rejects.toThrow(/being reclaimed/);
		expect(readFileSync(lock, "utf8")).toBe(raw);
		expect(count(env, bank)).toBe(0);
	});

	test("a reclaim leaves a marker for the record it replaced, and the lock is gone afterwards", async () => {
		const { env } = await scenario(1);
		const lock = `${env.ledger}.lock`;
		const raw = `${JSON.stringify({ pid: await deadPid(), token: "gone2", startedAt: "2020-01-01T00:00:00.000Z" })}\n`;
		writeFileSync(lock, raw);
		await runImport(apply(env));
		expect(existsSync(`${lock}.reclaimed-${hashOf(raw)}`)).toBe(true);
		expect(existsSync(lock)).toBe(false);
	});

	test("six processes racing for a stale lock: one at a time is inside, the others are refused, round after round", async () => {
		const env = await makeEnv();
		envs.push(env);
		const script = path.join(env.root, "race.ts");
		writeFileSync(
			script,
			`import { closeSync, openSync, unlinkSync, appendFileSync } from "node:fs";
import { withImporterLock } from ${JSON.stringify(path.join(import.meta.dir, "..", "ledger"))};
const [ledger, startAt, dir] = process.argv.slice(2);
while (Date.now() < Number(startAt)) {}
try {
	await withImporterLock(ledger, async () => {
		try {
			closeSync(openSync(dir + "/inside", "wx"));
		} catch {
			console.log("violation");
			return;
		}
		console.log("ran");
		await Bun.sleep(900);
		unlinkSync(dir + "/inside");
	});
} catch (error) {
	console.log(String(error).includes("lock") ? "refused" : "failed: " + String(error));
}
`,
		);
		const pid = await deadPid();
		for (let round = 0; round < 4; round += 1) {
			const dir = path.join(env.root, `race-${round}`);
			mkdirSync(dir);
			const ledger = path.join(dir, "ledger.json");
			writeFileSync(`${ledger}.lock`, `${JSON.stringify({ pid, token: `old-${round}`, startedAt: "2020-01-01T00:00:00.000Z" })}\n`);
			const startAt = String(Date.now() + 2500);
			const children = Array.from({ length: 6 }, () => Bun.spawn([process.execPath, script, ledger, startAt, dir], { stdout: "pipe", stderr: "pipe" }));
			const outputs = await Promise.all(children.map(async child => (await new Response(child.stdout).text()).trim()));
			await Promise.all(children.map(child => child.exited));
			expect(outputs.filter(line => line === "violation")).toEqual([]);
			expect(outputs.filter(line => line.startsWith("failed"))).toEqual([]);
			expect(outputs.filter(line => line === "ran")).toHaveLength(1);
			expect(outputs.filter(line => line === "refused")).toHaveLength(5);
			expect(existsSync(`${ledger}.lock`)).toBe(false);
		}
	});
});

describe("a run that never reached the ledger is rebuilt from its rows, and undo is only done when nothing of the run is left", () => {
	test("rows adopted after a stop between the commit and the ledger save are covered by verify", async () => {
		const { env, bank } = await scenario(2);
		const runId = "imp-test-lost-ledger";
		await runImport(apply(env, { runId }));
		rmSync(env.ledger); // the commit happened, the ledger save did not
		const again = await runImport(apply(env));
		expect(again.counts.imported).toBe(0);
		const ledger = loadLedger(env.ledger);
		expect(ledger.runs[runId]).toMatchObject({ runId, mode: "claude-memory", imported: 2, banks: [bank] });
		const report = await runVerify(env.options({ command: "verify" }));
		expect(report.problems).toEqual([]);
		expect(report.ok).toBe(true);
		expect(report.banks).toEqual([expect.objectContaining({ expected: 2, actual: 2 })]);
	});

	test("an undo that leaves a ledger-backed row whose provenance changed is not marked done, and verify still reports it", async () => {
		const { env, bank } = await scenario(2);
		const runId = "imp-test-survivor-undo";
		await runImport(apply(env, { runId }));
		const db = env.db(bank);
		db.run("UPDATE working_memory SET source = 'someone-else' WHERE id = (SELECT id FROM working_memory ORDER BY id LIMIT 1)");
		db.close();
		const report = await runUndo(env.options({ command: "undo", targetRun: runId, apply: true }));
		expect(report.removed).toEqual([{ bank: expect.any(String), rows: 1 }]);
		expect(report.problems.join("; ")).toMatch(/not written by the importer/);
		const ledger = loadLedger(env.ledger);
		expect(ledger.runs[runId]?.undoneAt).toBeUndefined();
		expect(Object.values(ledger.entries).filter(entry => entry.state === "imported")).toHaveLength(1);
		const verified = await runVerify(env.options({ command: "verify" }));
		expect(verified.ok).toBe(false);
		expect(verified.banks[0]).toMatchObject({ expected: 1, actual: 1, provenanceMismatch: 1 });
	});
});

describe("bank ids that carry a secret are never printed", () => {
	// a recognisable token built at run time: a bank directory named after it, made before the run
	const token = `${"sk"}-${"ant"}-api03-${"abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"}`;
	const secretBank = `${token}-x1y2z3`;
	const runId = "imp-test-secretbank";

	async function secretScenario(): Promise<Env> {
		const env = await makeEnv();
		envs.push(env);
		env.emptyBank(secretBank);
		const rows = ["one", "two"].map((name, index) => ({
			...rowToWrite(`c0ffee00000000b${index}`, `a row in the secret-named bank, ${name}`),
			metadata: { source: "claude-code", originKey: `direct/${name}`, originHash: `h${index}`, importRunId: runId, tool: "import" },
		}));
		const written = await applyBank(env.lib, env.banksDir, secretBank, rows, { model: DEFAULT_EMBEDDING_MODEL, importedAt: new Date().toISOString(), embedder: fakeEmbedder });
		expect(written.error).toBeNull();
		return env;
	}

	test("verify names the bank by its opaque label, internally it still finds it", async () => {
		const env = await secretScenario();
		const report = await runVerify(env.options({ command: "verify", targetRun: runId }));
		expect(report.banks).toHaveLength(1);
		expect(report.banks[0]?.bank).toMatch(/^bank#\d+$/);
		expect(report.banks[0]?.actual).toBe(2);
		expect(JSON.stringify(report)).not.toContain(token);
		expect(report.problems.join("\n")).not.toContain(token);
	});

	test("undo reports, problems and the command line's output carry no token; the rows it can remove are removed", async () => {
		const env = await secretScenario();
		const db = env.db(secretBank);
		db.run("UPDATE working_memory SET source = 'someone-else' WHERE id = 'c0ffee00000000b0'");
		db.close();
		const report = await runUndo(env.options({ command: "undo", targetRun: runId, apply: true }));
		expect(report.removed).toEqual([{ bank: expect.stringMatching(/^bank#\d+$/), rows: 1 }]);
		expect(report.problems.length).toBeGreaterThan(0);
		expect(JSON.stringify(report)).not.toContain(token);
		expect(count(env, secretBank)).toBe(1);

		const cli = path.join(import.meta.dir, "..", "cli.ts");
		const ran = Bun.spawnSync([process.execPath, cli, "undo", "--run", runId, "--apply", "--banks-dir", env.banksDir, "--ledger", env.ledger, "--lib-dir", env.libDir, "--omp-bin", env.fakeOmp, "--force-closed"], { stdout: "pipe", stderr: "pipe", env: process.env });
		expect(ran.exitCode).toBe(1); // the row that is not the importer's is still reported
		expect(ran.stdout.toString()).not.toContain(token);
		expect(ran.stderr.toString()).not.toContain(token);
		expect(ran.stdout.toString()).toContain("bank#");
	});

	test("a refusal for the bank, an import report and a run-id clash do not name it", async () => {
		const env = await secretScenario();
		symlinkSync(path.join(env.banksDir, secretBank), path.join(env.banksDir, `${token}-link00`));
		expect(() => assertBankWritable(env.banksDir, `${token}-link00`)).toThrow(/bank bank#\d+/);
		try {
			assertBankWritable(env.banksDir, `${token}-link00`);
		} catch (error) {
			expect(String(error)).not.toContain(token);
		}
		await expect(runImport(apply(env, { runId }))).rejects.toThrow(UsageError);
		await runImport(apply(env, { runId })).catch((error: unknown) => expect(String(error)).not.toContain(token));
		const db = env.db(secretBank);
		db.exec(EVIL_TRIGGER);
		db.close();
		const state = await readBankState(env.lib, env.banksDir, secretBank);
		expect(state.schemaProblems.length).toBeGreaterThan(0);
	});
});

describe("exempt banks are judged again right before their write, and banksCreated is only written for what this run made", () => {
	/** A bank this tool made (one import), with one more note waiting to be imported into it. */
	async function madeByUs(): Promise<Scenario> {
		const s = await scenario(1, false);
		await runImport(apply(s.env));
		expect(loadLedger(s.env.ledger).banksCreated[s.bank]).toBeDefined();
		s.env.memory(s.dir, "second.md", { name: "Second" }, "A second note for the bank this tool made.");
		return s;
	}
	const exempt = (bank: string) => ({ noBackupCheck: false, backedUpBanks: [] as string[], exemptBanks: [bank] });

	test("the flag reads like --backed-up-banks, empty value included", async () => {
		const { env } = await scenario(0);
		const base = ["import", "--apply", "--banks-dir", env.banksDir, "--ledger", env.ledger];
		expect(buildOptions("import", parseArgs([...base, "--exempt-banks="]).flags).exemptBanks).toEqual([]);
		expect(buildOptions("import", parseArgs([...base, "--exempt-banks", "a, b"]).flags).exemptBanks).toEqual(["a", "b"]);
		expect(buildOptions("import", parseArgs(base).flags).exemptBanks).toBeUndefined();
	});

	test("a bank this tool made and that still holds only its rows is exempt", async () => {
		const { env, bank } = await madeByUs();
		const report = await runImport(apply(env, exempt(bank)));
		expect(report.counts.imported).toBe(1);
		expect(report.counts.errors).toBe(0);
		expect(count(env, bank)).toBe(2);
	});

	test("a native row added after the plan was made ends the exemption: the bank is refused and untouched", async () => {
		const { env, bank } = await madeByUs();
		let snapshot: Buffer | null = null;
		const report = await runImport(
			apply(env, {
				...exempt(bank),
				embedderFactory: () => ({
					async embed(texts) {
						const db = env.db(bank);
						insertNativeRow(db, bank, "native-late", "written by omp while the embeddings were made");
						db.close();
						snapshot = bytesOf(env, bank);
						return fakeEmbedder.embed(texts);
					},
					close() {},
				}),
			}),
		);
		expect(report.counts.imported).toBe(0);
		expect(report.errors.find(item => item.where.startsWith("bank "))?.error).toContain("is not covered by a verified backup");
		expect(count(env, bank)).toBe(2); // the imported row and the native one, nothing more
		expect(snapshot).not.toBeNull();
		expect(bytesOf(env, bank).equals(snapshot as unknown as Buffer)).toBe(true);
	});

	test("an exemption names its bank only: another existing bank under the same invocation is refused", async () => {
		const { env, bank, cwd } = await madeByUs();
		const other = env.folder("other");
		const otherBank = env.bankOf(other);
		env.memory(env.project(other), "o.md", { name: "Other" }, "A note for a bank that is not exempt.");
		env.emptyBank(otherBank);
		expect(otherBank).not.toBe(bank);
		expect(cwd).not.toBe(other);
		const report = await runImport(apply(env, exempt(bank)));
		expect(count(env, bank)).toBe(2);
		expect(count(env, otherBank)).toBe(0);
		expect(report.errors.find(item => item.where.startsWith("bank "))?.error).toContain("is not covered by a verified backup");
	});

	test("a bank that is exempt but was not made by this tool is refused, and one that is backed up needs no exemption", async () => {
		const { env, bank } = await scenario(1); // exists from before, not in banksCreated
		const refused = await runImport(apply(env, exempt(bank)));
		expect(refused.counts.errors).toBe(1);
		expect(count(env, bank)).toBe(0);
		const covered = await runImport(apply(env, { noBackupCheck: false, backedUpBanks: [bank], exemptBanks: [] }));
		expect(covered.counts.imported).toBe(1);
	});

	test("an exemption is used up by its write: the same bank cannot be written twice in one run", async () => {
		const { env, bank } = await scenario(0);
		const options = env.options({ ...exempt(bank), backedUpBanks: [bank] });
		const written = new Set<string>();
		requireBackupCoverage(options, emptyLedger(), bank, written);
		expect(() => requireBackupCoverage(options, emptyLedger(), bank, written)).toThrow(/already written in this run/);
	});

	test("banksCreated records a bank this run made", async () => {
		const { env, bank } = await scenario(1, false);
		await runImport(apply(env));
		expect(loadLedger(env.ledger).banksCreated[bank]).toMatchObject({ runId: expect.any(String) });
	});

	test("a bank another process made after the plan is never recorded as made by this tool, refused or written", async () => {
		for (const written of [false, true]) {
			const { env, bank } = await scenario(1, false);
			const report = await runImport(
				apply(env, {
					noBackupCheck: written,
					backedUpBanks: written ? undefined : [],
					embedderFactory: () => ({
						async embed(texts) {
							env.emptyBank(bank); // made by someone else while the embeddings are made
							return fakeEmbedder.embed(texts);
						},
						close() {},
					}),
				}),
			);
			expect(report.counts.imported).toBe(written ? 1 : 0);
			expect(report.counts.errors).toBe(written ? 0 : 1);
			expect(loadLedger(env.ledger).banksCreated).toEqual({});
		}
	});
});

describe("undo judges what is left by row id, and runs are recovered from the banks", () => {
	test("a ledger-backed row whose run id was removed is a survivor: reported, kept, the run stays open, verify reports it", async () => {
		const { env, bank } = await scenario(2);
		const runId = "imp-test-stripped";
		await runImport(apply(env, { runId }));
		const db = env.db(bank);
		db.run("UPDATE working_memory SET metadata_json = json_remove(metadata_json, '$.importRunId') WHERE id = (SELECT id FROM working_memory ORDER BY id LIMIT 1)");
		const strayId = (db.query("SELECT id FROM working_memory WHERE json_extract(metadata_json, '$.importRunId') IS NULL").get() as { id: string }).id;
		db.close();
		const report = await runUndo(env.options({ command: "undo", targetRun: runId, apply: true }));
		expect(report.removed).toEqual([{ bank: expect.any(String), rows: 1 }]);
		expect(report.problems.join("; ")).toContain("no longer carry the run's metadata");
		expect(report.problems.join("; ")).toContain(strayId);
		const ledger = loadLedger(env.ledger);
		expect(ledger.runs[runId]?.undoneAt).toBeUndefined();
		expect(Object.values(ledger.entries).filter(entry => entry.state === "imported").map(entry => entry.rowId)).toEqual([strayId]);
		expect(count(env, bank)).toBe(1);
		const verified = await runVerify(env.options({ command: "verify" }));
		expect(verified.ok).toBe(false);
		expect(verified.banks[0]).toMatchObject({ expected: 1, missingRows: 1 });
	});

	test("a row whose run id was changed is a survivor too", async () => {
		const { env, bank } = await scenario(1);
		const runId = "imp-test-changed-id";
		await runImport(apply(env, { runId }));
		const db = env.db(bank);
		db.run("UPDATE working_memory SET metadata_json = json_set(metadata_json, '$.importRunId', 'imp-some-other-run')");
		db.close();
		const report = await runUndo(env.options({ command: "undo", targetRun: runId, apply: true }));
		expect(report.removed).toEqual([]);
		expect(report.problems.join("; ")).toContain("no longer carry the run's metadata");
		expect(loadLedger(env.ledger).runs[runId]?.undoneAt).toBeUndefined();
		expect(count(env, bank)).toBe(1);
		expect((await runVerify(env.options({ command: "verify" }))).ok).toBe(false);
	});

	test("a run whose ledger record was lost and whose source files are gone is still known to verify and undo", async () => {
		const { env, bank, dir } = await scenario(2);
		const runId = "imp-test-orphaned";
		await runImport(apply(env, { runId }));
		rmSync(env.ledger);
		rmSync(path.join(dir, "memory"), { recursive: true });
		const verified = await runVerify(env.options({ command: "verify" }));
		expect(verified.recoveredRuns).toEqual([runId]);
		expect(verified.problems).toEqual([]);
		expect(verified.ok).toBe(true);
		expect(verified.banks).toEqual([expect.objectContaining({ expected: 2, actual: 2 })]);
		expect((await runVerify(env.options({ command: "verify", targetRun: runId }))).ok).toBe(true);
		const undone = await runUndo(env.options({ command: "undo", targetRun: runId, apply: true }));
		expect(undone.problems).toEqual([]);
		expect(undone.removed).toEqual([{ bank: expect.any(String), rows: 2 }]);
		expect(count(env, bank)).toBe(0);
		expect(loadLedger(env.ledger).runs[runId]?.undoneAt).toBeDefined();
	});
});

describe("an exemption holds only for a bank that provably holds nothing of the user's", () => {
	const exempt = (bank: string) => ({ noBackupCheck: false, backedUpBanks: [] as string[], exemptBanks: [bank] });
	const notCovered = (report: { errors: { where: string; error: string }[] }): void => {
		expect(report.errors.find(item => item.where.startsWith("bank "))?.error).toContain("is not covered by a verified backup");
	};
	const NATIVE: Record<string, string> = {
		episodic_memory: "INSERT INTO episodic_memory (id, content) VALUES ('native-ep', 'an episode omp wrote')",
		scratchpad: "INSERT INTO scratchpad (id, content) VALUES ('native-sp', 'a scratch note')",
		gists: "INSERT INTO gists (text) VALUES ('a gist')",
		facts: "INSERT INTO facts (session_id, subject, predicate, object) VALUES ('s', 'a', 'b', 'c')",
		triples: "INSERT INTO triples (subject, predicate, object) VALUES ('a', 'b', 'c')",
		graph_edges: "INSERT INTO graph_edges (source, target, edge_type) VALUES ('a', 'b', 'c')",
		consolidation_log: "INSERT INTO consolidation_log (session_id, items_consolidated) VALUES ('s', 1)",
	};
	const addNative = (env: Env, bank: string, table: string): void => {
		const db = env.db(bank);
		try {
			db.run(NATIVE[table] as string);
		} finally {
			db.close();
		}
	};

	/** A bank this tool made with one note, and a second note waiting. */
	async function madeByUs(): Promise<Scenario> {
		const s = await scenario(1, false);
		await runImport(apply(s.env));
		expect(loadLedger(s.env.ledger).banksCreated[s.bank]).toBeDefined();
		s.env.memory(s.dir, "second.md", { name: "Second" }, "A second note for the bank this tool made.");
		return s;
	}

	/** A bank this tool made, whose rows were undone: empty, still listed in banksCreated, with a note waiting. */
	async function emptiedByUs(): Promise<Scenario> {
		const s = await scenario(1, false);
		const runId = `imp-test-emptied-${Bun.randomUUIDv7().slice(-6)}`;
		await runImport(apply(s.env, { runId }));
		await runUndo(s.env.options({ command: "undo", targetRun: runId, apply: true }));
		expect(count(s.env, s.bank)).toBe(0);
		rmSync(path.join(s.dir, "memory", "n1.md")); // the undone note is gone; only the new one is left to import
		s.env.memory(s.dir, "again.md", { name: "Again" }, "A note for the emptied bank.");
		return s;
	}

	test("native episodic memory added between the plan and the write ends the exemption; the bank is untouched", async () => {
		const { env, bank } = await madeByUs();
		let snapshot: Buffer | null = null;
		const report = await runImport(
			apply(env, {
				...exempt(bank),
				embedderFactory: () => ({
					async embed(texts) {
						addNative(env, bank, "episodic_memory");
						snapshot = bytesOf(env, bank);
						return fakeEmbedder.embed(texts);
					},
					close() {},
				}),
			}),
		);
		expect(report.counts.imported).toBe(0);
		notCovered(report);
		expect(snapshot).not.toBeNull();
		expect(bytesOf(env, bank).equals(snapshot as unknown as Buffer)).toBe(true);
	});

	test("a bank that holds only native episodic memory, no working rows at all, is refused although it is listed", async () => {
		const { env, bank } = await emptiedByUs();
		addNative(env, bank, "episodic_memory");
		const report = await runImport(apply(env, exempt(bank)));
		expect(report.counts.imported).toBe(0);
		notCovered(report);
		expect(count(env, bank)).toBe(0);
	});

	for (const table of Object.keys(NATIVE).filter(name => name !== "episodic_memory")) {
		test(`a row in ${table} ends the exemption`, async () => {
			const { env, bank } = await emptiedByUs();
			addNative(env, bank, table);
			const report = await runImport(apply(env, exempt(bank)));
			expect(report.counts.imported).toBe(0);
			notCovered(report);
			expect(count(env, bank)).toBe(0); // nothing was written
			expect(count(env, bank, `SELECT COUNT(*) AS n FROM ${table}`)).toBe(1); // and the user's row is still there
		});
	}

	test("a database with an unmerged write-ahead log is not exempt", async () => {
		const { env, bank } = await madeByUs();
		const dir = path.join(env.banksDir, bank);
		const file = path.join(dir, "mnemopi.db");
		const report = await runImport(
			apply(env, {
				...exempt(bank),
				embedderFactory: () => ({
					async embed(texts) {
						// a live writer with checkpoints off: copy the database with its log while it is open, then let it go
						const writer = new Database(file);
						writer.exec("PRAGMA journal_mode = WAL");
						writer.exec("PRAGMA wal_autocheckpoint = 0");
						writer.run("UPDATE working_memory SET importance = importance + 0.0001");
						const copies = ["", "-wal", "-shm"].map(suffix => ({ suffix, bytes: existsSync(file + suffix) ? readFileSync(file + suffix) : null }));
						writer.close();
						for (const { suffix, bytes } of copies) {
							if (bytes === null) rmSync(file + suffix, { force: true });
							else writeFileSync(file + suffix, bytes);
						}
						return fakeEmbedder.embed(texts);
					},
					close() {},
				}),
			}),
		);
		expect(report.counts.imported).toBe(0);
		expect(report.counts.errors).toBe(1);
	});

	test("a bank that cannot be read is not exempt", async () => {
		const { env, bank } = await madeByUs();
		const file = path.join(env.banksDir, bank, "mnemopi.db");
		const report = await runImport(
			apply(env, {
				...exempt(bank),
				embedderFactory: () => ({
					async embed(texts) {
						writeFileSync(file, "this is no longer a database\n");
						return fakeEmbedder.embed(texts);
					},
					close() {},
				}),
			}),
		);
		expect(report.counts.imported).toBe(0);
		expect(report.counts.errors).toBe(1);
		expect(readFileSync(file, "utf8")).toBe("this is no longer a database\n");
	});

	test("an empty bank this tool made, and a bank holding only this tool's rows, are still exempt", async () => {
		const empty = await emptiedByUs();
		const first = await runImport(apply(empty.env, exempt(empty.bank)));
		expect(first.counts.imported).toBe(1);
		expect(first.counts.errors).toBe(0);
		const rows = await madeByUs();
		const second = await runImport(apply(rows.env, exempt(rows.bank)));
		expect(second.counts.imported).toBe(1);
		expect(second.counts.errors).toBe(0);
		expect(count(rows.env, rows.bank)).toBe(2);
	});

	test("the predicate itself: everything of the user's makes it false, and any error does too", async () => {
		const { holdsOnlyImporterContent } = (await import("../banks")) as unknown as { holdsOnlyImporterContent: (banksDir: string, bank: string) => boolean };
		const { env, bank } = await madeByUs();
		expect(holdsOnlyImporterContent(env.banksDir, bank)).toBe(true);
		const native = env.db(bank);
		insertNativeRow(native, bank, "native-w", "a native working row");
		native.close();
		expect(holdsOnlyImporterContent(env.banksDir, bank)).toBe(false);
		const orphan = await emptiedByUs();
		expect(holdsOnlyImporterContent(orphan.env.banksDir, orphan.bank)).toBe(true);
		const db = orphan.env.db(orphan.bank);
		db.run("INSERT INTO annotations (memory_id, kind, value, source, confidence) VALUES ('some-native-episode', 'k', 'v', 's', 1)");
		db.close();
		expect(holdsOnlyImporterContent(orphan.env.banksDir, orphan.bank)).toBe(false); // derived rows may only refer to importer working rows
		const broken = await emptiedByUs();
		writeFileSync(path.join(broken.env.banksDir, broken.bank, "mnemopi.db"), "garbage");
		expect(holdsOnlyImporterContent(broken.env.banksDir, broken.bank)).toBe(false);
	});

	test("an importer row needs every field: a non-empty text source in its metadata, tool, origin key and hash, trust tier and a matching source column", async () => {
		const { isImporterRow } = await import("../banks");
		const row = { source: "import-claude-code", trust_tier: "IMPORTED" };
		const meta: Record<string, unknown> = { source: "claude-code", tool: "import", originKey: "folder#note", originHash: "abc" };
		expect(isImporterRow(row, meta)).toBe(true);
		const without = (key: string): Record<string, unknown> => Object.fromEntries(Object.entries(meta).filter(([name]) => name !== key));
		const bad: [string, { source: string | null; trust_tier: string | null }, Record<string, unknown>][] = [
			["no source in the metadata", row, without("source")],
			["a null source in the metadata", row, { ...meta, source: null }],
			["an empty source in the metadata, with the matching `import-` column", { ...row, source: "import-" }, { ...meta, source: "" }],
			["a number as the source", { ...row, source: "import-5" }, { ...meta, source: 5 }],
			["no tool", row, without("tool")],
			["another tool", row, { ...meta, tool: "retain" }],
			["no origin key", row, without("originKey")],
			["a null origin key", row, { ...meta, originKey: null }],
			["no origin hash", row, without("originHash")],
			["a null origin hash", row, { ...meta, originHash: null }],
			["no trust tier", { ...row, trust_tier: null }, meta],
			["another trust tier", { ...row, trust_tier: "TOOL" }, meta],
			["no source column", { ...row, source: null }, meta],
			["a source column that disagrees", { ...row, source: "import-hindsight" }, meta],
		];
		for (const [name, rowCase, metaCase] of bad) expect({ name, importer: isImporterRow(rowCase, metaCase) }).toEqual({ name, importer: false });
	});

	test("a bank whose rows lose any of those fields is no longer exempt", async () => {
		const { holdsOnlyImporterContent } = await import("../banks");
		const edits: [string, string][] = [
			["a missing source", "UPDATE working_memory SET metadata_json = json_remove(metadata_json, '$.source')"],
			["a null source", "UPDATE working_memory SET metadata_json = json_set(metadata_json, '$.source', json('null'))"],
			["an empty source", "UPDATE working_memory SET metadata_json = json_set(metadata_json, '$.source', ''), source = 'import-'"],
			["a missing tool", "UPDATE working_memory SET metadata_json = json_remove(metadata_json, '$.tool')"],
			["a null origin key", "UPDATE working_memory SET metadata_json = json_set(metadata_json, '$.originKey', json('null'))"],
			["a missing origin hash", "UPDATE working_memory SET metadata_json = json_remove(metadata_json, '$.originHash')"],
			["another trust tier", "UPDATE working_memory SET trust_tier = 'TOOL'"],
		];
		for (const [name, sql] of edits) {
			const { env, bank } = await madeByUs();
			expect({ name, exempt: holdsOnlyImporterContent(env.banksDir, bank) }).toEqual({ name, exempt: true });
			const db = env.db(bank);
			db.run(sql);
			db.close();
			expect({ name, exempt: holdsOnlyImporterContent(env.banksDir, bank) }).toEqual({ name, exempt: false });
		}
	});
});
