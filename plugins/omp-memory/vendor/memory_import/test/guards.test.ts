/** Refusals (version, schema, open bank), bank ids, folder decoding and front matter parsing. */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { mkdirSync, symlinkSync } from "node:fs";
import path from "node:path";
import { runDoctor } from "../doctor";
import { runImport } from "../engine";
import { decodeProjectDir, encodeProjectName } from "../folders";
import { inspectLibrary } from "../lib";
import { memoryHashInput, parseMemoryFile } from "../sources/claude_memory";
import { bankOpenBy } from "../banks";
import { type Env, fakeOmpScript, insertNativeRow, makeEnv, TEST_OMP_VERSION } from "./helpers";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

let env: Env;
let cwd: string;
let bank: string;

beforeAll(async () => {
	env = await makeEnv();
	cwd = env.folder("guard");
	bank = env.bankOf(cwd);
	env.memory(env.project(cwd), "n.md", { name: "Guard note" }, "A note that should never reach a refused bank.");
	env.emptyBank(bank);
});
afterAll(() => env.cleanup());

const rows = (): number => {
	const db = env.db(bank);
	try {
		return (db.query("SELECT COUNT(*) AS n FROM working_memory").get() as { n: number }).n;
	} finally {
		db.close();
	}
};

describe("library and omp version", () => {
	// The version cases use a fake `omp` script (see helpers.ts) that prints a version and carries the SQL text.

	test("the same release passes, with parity of the SQL the importer relies on, in the package and in omp", async () => {
		const { check, lib } = await inspectLibrary({ libDir: env.libDir, ompBin: env.fakeOmp });
		expect(lib?.version).toBe(TEST_OMP_VERSION);
		expect(check.ok).toBe(true);
		expect(check.libraryKind).toBe("explicit");
		expect(check.parity?.missingInLibrary).toEqual([]);
		expect(check.parity?.missingInOmp).toEqual([]);
		expect(check.parity?.ompBinary).toBe("scanned");
	});

	test("a different release line is refused and nothing is written", async () => {
		const bin = path.join(env.root, "omp-other");
		fakeOmpScript(bin, "99.1.0");
		await expect(runImport(env.options({ command: "import", apply: true, ompBin: bin }))).rejects.toThrow(/no @oh-my-pi\/pi-mnemopi@99\.1\.0/);
		expect(rows()).toBe(0);
	});

	test("a patch-level difference is refused too, and the refusal names the exact setup command", async () => {
		const [maj, min, pat] = TEST_OMP_VERSION.split(".").map(Number);
		const newer = path.join(env.root, "omp-newer");
		const version = `${maj}.${min}.${(pat ?? 0) + 3}`;
		fakeOmpScript(newer, version);
		const attempt = runImport(env.options({ command: "import", apply: true, ompBin: newer }));
		await expect(attempt).rejects.toThrow(new RegExp(`no @oh-my-pi/pi-mnemopi@${version.replaceAll(".", "\\.")} .*it is ${TEST_OMP_VERSION.replaceAll(".", "\\.")}, omp is ${version.replaceAll(".", "\\.")}`));
		await expect(attempt).rejects.toThrow(/Run `.*cli\.ts setup --omp-bin .*omp-newer` to install exactly that version/);
		const { check, lib } = await inspectLibrary({ libDir: env.libDir, ompBin: newer });
		expect(lib).toBeNull();
		expect(check.ok).toBe(false);
		expect(rows()).toBe(0);
	});

	test("dry-run is refused as well when no library matches omp (the library validates every bank)", async () => {
		const bin = path.join(env.root, "omp-other2");
		fakeOmpScript(bin, "99.1.0");
		await expect(runImport(env.options({ command: "dry-run", ompBin: bin }))).rejects.toThrow(/Run `.*setup/);
	});

	test("the same release whose omp lacks the SQL the importer relies on is refused at write time", async () => {
		const changed = path.join(env.root, "omp-changed");
		fakeOmpScript(changed, TEST_OMP_VERSION, false);
		const { check } = await inspectLibrary({ libDir: env.libDir, ompBin: changed });
		expect(check.ok).toBe(false);
		expect(check.parity?.missingInOmp.length).toBeGreaterThan(0);
		await expect(runImport(env.options({ command: "import", apply: true, ompBin: changed }))).rejects.toThrow(/omp binary lacks/);
		// the plan is still available (it reports the problem)
		const plan = await runImport(env.options({ command: "dry-run", ompBin: changed }));
		expect((plan.library as { ok: boolean }).ok).toBe(false);
		expect(rows()).toBe(0);
	});

	test("an omp that is a small launcher script is not scanned for SQL, and says so", async () => {
		const launcher = path.join(env.root, "omp-launcher");
		await Bun.write(launcher, `#!/bin/sh\necho "omp/${TEST_OMP_VERSION}"\n`);
		await Bun.$`chmod 755 ${launcher}`;
		const { check } = await inspectLibrary({ libDir: env.libDir, ompBin: launcher });
		expect(check.ok).toBe(true);
		expect(check.parity?.ompBinary).toContain("not scanned");
	});

	test("an omp that prints no version is refused", async () => {
		const mute = path.join(env.root, "omp-mute");
		await Bun.write(mute, "#!/bin/sh\necho hello\n");
		await Bun.$`chmod 755 ${mute}`;
		await expect(runImport(env.options({ command: "dry-run", ompBin: mute }))).rejects.toThrow(/could not read a version/);
	});
});

describe("bank guards", () => {
	test("a bank whose schema differs from the library is refused", async () => {
		const other = env.folder("oldschema");
		env.memory(env.project(other), "x.md", { name: "X" }, "Some text for the old-schema bank.");
		const otherBank = env.bankOf(other);
		mkdirSync(path.join(env.banksDir, otherBank), { recursive: true });
		const db = new Database(path.join(env.banksDir, otherBank, "mnemopi.db"));
		db.exec("CREATE TABLE working_memory (id TEXT PRIMARY KEY, content TEXT NOT NULL)");
		db.close();
		await expect(runImport(env.options({ command: "import", apply: true }))).rejects.toThrow(/schema differs from the library/);
		expect(rows()).toBe(0);
	});

	test("a bank another process has open is refused unless --force-closed", async () => {
		const file = path.join(env.banksDir, bank, "mnemopi.db");
		// Integration with the OS: a real second process holds the file; it says "ready" once it has the database open
		// and stays alive until the test kills it. The handle is kept on globalThis: Bun closes a SQLite handle that is only held in a local once it is garbage-collected.
		const holder = Bun.spawn(
			[
				process.execPath,
				"-e",
				`import {Database} from "bun:sqlite"; globalThis.held = new Database(${JSON.stringify(file)}); globalThis.held.query("SELECT 1").get(); console.log("ready"); setInterval(() => {}, 1000);`,
			],
			{ stdin: "ignore", stdout: "pipe", stderr: "ignore" },
		);
		try {
			const reader = holder.stdout.getReader();
			expect(new TextDecoder().decode((await reader.read()).value)).toContain("ready");
			const holders = bankOpenBy(env.banksDir, bank);
			expect(holders).toContain(String(holder.pid));
			// the scan never misses a live holder: many scans in a row, each a fresh look at the process table
			for (let scan = 0; scan < 25; scan++) expect(bankOpenBy(env.banksDir, bank)).toContain(String(holder.pid));
			const guarded = env.options({ command: "import", apply: true, forceClosed: false, mode: "claude-memory" });
			// the schema-mismatch bank from the previous test sits in the same banks dir; remove it from the way
			await Bun.$`rm -rf ${path.join(env.banksDir, env.bankOf(env.folder("oldschema")))}`;
			await expect(runImport(guarded)).rejects.toThrow(/open in process/);
			// and with the sessions declared closed it goes through
			expect((await runImport({ ...guarded, forceClosed: true })).counts.imported).toBe(2);
			expect(rows()).toBe(1);
		} finally {
			holder.kill();
		}
	});
});

describe("bank ids and folders", () => {
	test("doctor reproduces every bank id from the folder its rows record, by realpath or by the recorded path", async () => {
		const real = env.folder("viaReal");
		const realBank = env.bankOf(real);
		env.emptyBank(realBank);
		let db = env.db(realBank);
		db.run(`UPDATE working_memory SET id = id`);
		insertNativeRow(db, realBank, "n1", "native via real");
		db.run("UPDATE working_memory SET metadata_json = ?", [JSON.stringify({ cwd: real })]);
		db.close();
		const link = path.join(env.root, "linked");
		symlinkSync(real, link);
		const linkBank = env.bankOf(link);
		env.emptyBank(linkBank);
		db = env.db(linkBank);
		insertNativeRow(db, linkBank, "n2", "native via link");
		db.run("UPDATE working_memory SET metadata_json = ?", [JSON.stringify({ cwd: link })]);
		db.close();
		const report = await runDoctor({ libDir: env.libDir, ompBin: env.fakeOmp, banksDir: env.banksDir });
		expect(report.banks.find(item => item.bank === realBank)?.matchedBy).toBe("realpath");
		expect(report.banks.find(item => item.bank === linkBank)?.matchedBy).toBe("recorded-path");
		expect(report.bankIdMismatches).toBe(0);
		expect(report.schemaMismatches).toBe(0);
		expect(report.library.ok).toBe(true);
		expect(report.library.libraryVersion).toBe(TEST_OMP_VERSION);
		expect(report.bun.version).toBe(Bun.version);
	});

	test("doctor still checks bank ids, and says the schema was not checked, when no library matches omp", async () => {
		const bin = path.join(env.root, "omp-nolib");
		fakeOmpScript(bin, "99.1.0");
		const report = await runDoctor({ libDir: env.libDir, ompBin: bin, banksDir: env.banksDir });
		expect(report.library.ok).toBe(false);
		expect(report.library.reasons.join(" ")).toContain("setup");
		expect(report.banks.every(item => item.schema === "not checked")).toBe(true);
		expect(report.bankIdMismatches).toBe(0);
		expect(report.banks.some(item => item.matchedBy === "realpath")).toBe(true);
	});

	test("a directory name decodes to the existing folder, including dots and hyphens; ambiguity yields several", () => {
		const base = path.join(env.root, "tree");
		for (const dir of ["a-b/c", "a/b-c", ".hidden/x-y", "plain/one"]) mkdirSync(path.join(base, dir), { recursive: true });
		const encode = (folder: string) => encodeProjectName(folder);
		expect(decodeProjectDir(encode(path.join(base, ".hidden", "x-y")))).toEqual([path.join(base, ".hidden", "x-y")]);
		expect(decodeProjectDir(encode(path.join(base, "plain", "one")))).toEqual([path.join(base, "plain", "one")]);
		const both = decodeProjectDir(encode(path.join(base, "a-b", "c")));
		expect(both.sort()).toEqual([path.join(base, "a-b", "c"), path.join(base, "a", "b-c")]);
		expect(decodeProjectDir("-definitely-not-a-folder")).toEqual([]);
	});
});

describe("front matter", () => {
	test("nested metadata type is read, volatile fields do not change the hash", () => {
		const a = parseMemoryFile("---\nname: n\nmetadata:\n  type: reference\noriginSessionId: abc\nmodified: 2026-01-01\n---\n\nbody\n");
		expect(a.front["metadata.type"]).toBe("reference");
		const b = parseMemoryFile("---\nname: n\nmetadata:\n  type: reference\noriginSessionId: other\nmodified: 2027-02-02\n---\n\nbody   \n");
		expect(memoryHashInput(a)).toBe(memoryHashInput(b));
		const c = parseMemoryFile("---\nname: n\nmetadata:\n  type: reference\n---\n\nchanged\n");
		expect(memoryHashInput(a)).not.toBe(memoryHashInput(c));
	});

	test("a file without front matter or with an unclosed fence is kept as text", () => {
		expect(parseMemoryFile("just text").body).toBe("just text");
		const open = parseMemoryFile("---\nname: n\nno end");
		expect(open.problem).toContain("never closed");
		expect(open.body).toContain("no end");
	});
});
