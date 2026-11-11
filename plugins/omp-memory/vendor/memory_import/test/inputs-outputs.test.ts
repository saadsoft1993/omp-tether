/**
 * What goes in (source files, provenance strings, setup flags) and what comes out (reports, samples, state files, temp
 * files) of the importer: output destinations, temp files, secrets in provenance, hold order, setup --dry-run, file kinds
 * and sizes in the memory directory, registry URLs.
 */
import { afterAll, afterEach, beforeAll, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import {
	existsSync,
	lstatSync,
	mkdirSync,
	mkdtempSync,
	readdirSync,
	readFileSync,
	realpathSync,
	rmdirSync,
	rmSync,
	statSync,
	symlinkSync,
	writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { buildOptions, parseArgs, releaseList } from "../main";
import { runImport, runScan, runUndo, runVerify } from "../engine";
import { SETUP_MARKER } from "../lib";
import { loadLedger } from "../ledger";
import { Refusal, UsageError, writeJsonAtomic } from "../model";
import { carriesSecret } from "../hold";
import { isInside } from "../outputs";
import { claimRuntimeDir, fastembedRuntimeDir, releaseRuntimeClaim, runSetup } from "../setup";
import { draftProjectMap } from "../sources/hindsight";
import { collectClaudeMemory } from "../sources/claude_memory";
import { removeSnapshot, snapshotDatabase, sqliteUri } from "../sources/claude_mem";
import { encodeProjectName } from "../folders";
import { type Env, ensureTestLibrary, makeEnv, TEST_OMP_VERSION } from "./helpers";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

// built at run time so that no file of this repository holds a literal token
const TOKEN = `ghp_${"Zk9Qm2Xv7Lp4Rt8NwB3cD5eF6gH1jK0aS7uY"}`;

const scratch = (): string => realpathSync(mkdtempSync(path.join(tmpdir(), "inouts-test-")));

describe("temp files of writeJsonAtomic", () => {
	let dir: string;
	beforeAll(() => {
		dir = scratch();
	});
	afterAll(() => rmSync(dir, { recursive: true, force: true }));

	test("a temp file or link put at the name `<file>.<pid>.tmp` is never written through", () => {
		const victim = path.join(dir, "victim.txt");
		const dangling = path.join(dir, "created-by-link.txt");
		writeFileSync(victim, "keep\n", { mode: 0o644 });
		const target = path.join(dir, "out.json");
		const predictable = `${target}.${process.pid}.tmp`;
		symlinkSync(victim, predictable);
		writeJsonAtomic(target, { a: 1 });
		expect(readFileSync(victim, "utf8")).toBe("keep\n");
		expect(statSync(victim).mode & 0o777).toBe(0o644);

		rmSync(predictable);
		symlinkSync(dangling, predictable);
		writeJsonAtomic(target, { a: 2 });
		expect(existsSync(dangling)).toBe(false);

		expect(JSON.parse(readFileSync(target, "utf8"))).toEqual({ a: 2 });
		expect(lstatSync(target).isFile()).toBe(true);
		expect(statSync(target).mode & 0o777).toBe(0o600);
	});

	test("no temp file is left behind", () => {
		const target = path.join(dir, "clean.json");
		writeJsonAtomic(target, { ok: true });
		const left = readdirSync(dir).filter(name => name.startsWith("clean.json") && name !== "clean.json");
		expect(left).toEqual([]);
	});

	test("the mode asked for is the mode of the result, whatever the umask", () => {
		const saved = process.umask(0o077);
		try {
			const target = path.join(dir, "wide.json");
			writeJsonAtomic(target, { ok: true }, 0o640);
			expect(statSync(target).mode & 0o777).toBe(0o640);
		} finally {
			process.umask(saved);
		}
	});
});

describe("output destinations", () => {
	let env: Env;
	let cwd: string;
	let bank: string;
	let dbFile: string;
	beforeAll(async () => {
		env = await makeEnv();
		cwd = env.folder("outs");
		bank = env.bankOf(cwd);
		env.memory(env.project(cwd), "note.md", { name: "Outs note", type: "project" }, "A plain note about the outs project.");
		dbFile = env.emptyBank(bank);
	});
	afterAll(() => env.cleanup());

	const isSqlite = (file: string): boolean => readFileSync(file).subarray(0, 15).toString("latin1") === "SQLite format 3";

	test("scan --out cannot replace a bank database", async () => {
		await expect(runScan(env.options({ command: "scan", out: dbFile }))).rejects.toThrow(Refusal);
		expect(isSqlite(dbFile)).toBe(true);
	});

	test("nothing may be written anywhere inside the banks directory, sidecars and new files included", async () => {
		for (const out of [`${dbFile}-wal`, path.join(env.banksDir, bank, "report.json"), path.join(env.banksDir, "not-a-bank-yet", "x.json")]) {
			const before = existsSync(out) ? readFileSync(out) : null;
			await expect(runScan(env.options({ command: "scan", out }))).rejects.toThrow(/banks directory/);
			expect(existsSync(out) ? readFileSync(out) : null).toEqual(before);
		}
	});

	test("a link that leads into the banks directory is refused like the directory itself", async () => {
		const alias = path.join(env.root, "alias");
		symlinkSync(env.banksDir, alias);
		await expect(runScan(env.options({ command: "scan", out: path.join(alias, bank, "mnemopi.db") }))).rejects.toThrow(Refusal);
		expect(isSqlite(dbFile)).toBe(true);
	});

	test("the dry run checks its out file and its samples file before it writes either", async () => {
		const out = path.join(env.root, "plan.json");
		await expect(runImport(env.options({ command: "dry-run", out: dbFile }))).rejects.toThrow(Refusal);
		expect(isSqlite(dbFile)).toBe(true);
		const samples = path.join(env.root, "plan.samples.json");
		writeFileSync(samples, "something that is not a samples file\n");
		await expect(runImport(env.options({ command: "dry-run", out }))).rejects.toThrow(Refusal);
		expect(readFileSync(samples, "utf8")).toBe("something that is not a samples file\n");
		expect(existsSync(out)).toBe(false);
		rmSync(samples);
	});

	test("an existing file is replaced only when it is a previous report", async () => {
		const notes = path.join(env.root, "notes.txt");
		writeFileSync(notes, "my notes\n");
		await expect(runScan(env.options({ command: "scan", out: notes }))).rejects.toThrow(/format marker/);
		expect(readFileSync(notes, "utf8")).toBe("my notes\n");

		const other = path.join(env.root, "other.json");
		writeFileSync(other, JSON.stringify({ projects: { x: "/somewhere" } }));
		await expect(runScan(env.options({ command: "scan", out: other }))).rejects.toThrow(Refusal);
		expect(JSON.parse(readFileSync(other, "utf8"))).toEqual({ projects: { x: "/somewhere" } });

		const folder = path.join(env.root, "a-folder");
		mkdirSync(folder);
		await expect(runScan(env.options({ command: "scan", out: folder }))).rejects.toThrow(Refusal);

		const report = path.join(env.root, "report.json");
		await runScan(env.options({ command: "scan", out: report }));
		await runScan(env.options({ command: "scan", out: report })); // a report of an earlier run: replaced
		expect(JSON.parse(readFileSync(report, "utf8")).command).toBe("scan");
	});

	test("the report cannot replace the ledger or another input of the run", async () => {
		const ledger = path.join(env.root, "own-ledger.json");
		writeFileSync(ledger, JSON.stringify({ version: 1, entries: {}, runs: {}, banksCreated: {} }));
		await expect(runScan(env.options({ command: "scan", out: ledger, ledgerFile: ledger }))).rejects.toThrow(Refusal);
		expect(JSON.parse(readFileSync(ledger, "utf8")).version).toBe(1);
	});

	test("verify and undo check their out file the same way", async () => {
		await expect(runVerify(env.options({ command: "verify", out: dbFile }))).rejects.toThrow(Refusal);
		await expect(runUndo(env.options({ command: "undo", targetRun: "imp-none", out: dbFile }))).rejects.toThrow(Refusal);
		expect(isSqlite(dbFile)).toBe(true);
	});

	test("the ledger cannot live inside the banks directory", async () => {
		const ledgerFile = path.join(env.banksDir, bank, "ledger.json");
		await expect(runImport(env.options({ command: "import", apply: true, ledgerFile }))).rejects.toThrow(/banks directory/);
		expect(existsSync(ledgerFile)).toBe(false);
		const db = env.db(bank);
		try {
			expect(db.query("SELECT COUNT(*) AS n FROM working_memory").get()).toEqual({ n: 0 });
		} finally {
			db.close();
		}
	});

	test("the command line refuses the same destinations before anything runs", () => {
		const flags = (extra: string[]) => parseArgs(["dry-run", "--banks-dir", env.banksDir, ...extra]).flags;
		expect(() => buildOptions("dry-run", flags(["--out", dbFile]))).toThrow(UsageError);
		expect(() => buildOptions("dry-run", flags(["--ledger", path.join(env.banksDir, bank, "ledger.json")]))).toThrow(UsageError);
		expect(buildOptions("dry-run", flags(["--out", path.join(env.root, "fine.json")])).out).toBe(path.join(env.root, "fine.json"));
	});
});

describe("hold comes before the ledger decision", () => {
	let env: Env;
	let cwd: string;
	let bank: string;
	let projectDir: string;
	beforeAll(async () => {
		env = await makeEnv();
		cwd = env.folder("holds");
		bank = env.bankOf(cwd);
		projectDir = env.project(cwd);
		env.emptyBank(bank);
	});
	afterAll(() => env.cleanup());

	const SECRET = "Ab1!";

	test("a changed note that now holds a password is held, and neither stdout nor the samples file show it", async () => {
		env.memory(projectDir, "deploy.md", { name: "Deploy notes" }, "Deploy with the blue lane.");
		await runImport(env.options({ command: "import", apply: true }));
		env.memory(projectDir, "deploy.md", { name: "Deploy notes" }, `Deploy with the blue lane, the admin panel password: ${SECRET} stays here.`);
		const out = path.join(env.root, "changed.json");
		const plan = await runImport(env.options({ command: "dry-run", out }));
		expect(plan.counts.skippedHeld).toBe(1);
		expect(plan.counts.changedSource).toBe(0);
		expect(JSON.stringify(plan)).not.toContain(SECRET);
		expect(readFileSync(plan.samplesFile ?? "", "utf8")).not.toContain(SECRET);
	});

	test("a tombstoned note that now holds a password is held and not sampled either", async () => {
		env.memory(projectDir, "gone.md", { name: "Gone notes" }, "Gone with the old lane.");
		const first = await runImport(env.options({ command: "import", apply: true }));
		const db = env.db(bank);
		try {
			const id = Object.values(loadLedger(env.ledger).entries).find(entry => entry.originKey.endsWith("#gone"))?.rowId ?? "";
			expect(id).not.toBe("");
			db.query("DELETE FROM working_memory WHERE id = ?").run(id);
		} finally {
			db.close();
		}
		expect(first.counts.errors).toBe(0);
		env.memory(projectDir, "gone.md", { name: "Gone notes" }, `Gone with the old lane, the admin panel password: ${SECRET} stays here.`);
		const plan = await runImport(env.options({ command: "dry-run", out: path.join(env.root, "tomb.json") }));
		expect(plan.held.some(item => item.originKey.endsWith("#gone"))).toBe(true);
		expect(plan.tombstoned.some(item => item.originKey.endsWith("#gone"))).toBe(false);
		expect(JSON.stringify(plan)).not.toContain(SECRET);
		expect(readFileSync(plan.samplesFile ?? "", "utf8")).not.toContain(SECRET);
	});

	test("an unchanged, imported note is still reported as unchanged", async () => {
		env.memory(projectDir, "keep.md", { name: "Keep notes" }, "Keep with the green lane.");
		await runImport(env.options({ command: "import", apply: true }));
		const plan = await runImport(env.options({ command: "dry-run" }));
		expect(plan.counts.skippedUnchanged).toBeGreaterThanOrEqual(1);
		expect(plan.held.some(item => item.originKey.endsWith("#keep"))).toBe(false);
	});
});

describe("secrets in provenance strings", () => {
	let env: Env;
	let cwd: string;
	let bank: string;
	let projectDir: string;
	beforeAll(async () => {
		env = await makeEnv();
		cwd = env.folder("prov");
		bank = env.bankOf(cwd);
		projectDir = env.project(cwd);
		env.emptyBank(bank);
	});
	afterAll(() => env.cleanup());

	const bankMetadata = (): string => {
		const db = env.db(bank);
		try {
			return (db.query("SELECT metadata_json FROM working_memory").all() as { metadata_json: string }[]).map(row => row.metadata_json).join("\n");
		} finally {
			db.close();
		}
	};

	test("a token in a memory file name holds the item under an opaque key and reaches no output, ledger or bank", async () => {
		env.memory(projectDir, `${TOKEN}.md`, { name: "Token named note" }, "A body with nothing special in it.");
		env.memory(projectDir, "plain.md", { name: "Plain note" }, "Another body with nothing special in it.");
		const out = path.join(env.root, "prov.json");
		const plan = await runImport(env.options({ command: "dry-run", out }));
		expect(plan.counts.skippedHeld).toBe(1);
		expect(plan.counts.wouldImport).toBe(1);
		expect(plan.held).toEqual([{ originKey: expect.stringMatching(/^held#\d+$/), kinds: ["hold:provenance-secret"] }]);
		expect(JSON.stringify(plan)).not.toContain(TOKEN);
		expect(readFileSync(out, "utf8")).not.toContain(TOKEN);
		expect(readFileSync(plan.samplesFile ?? "", "utf8")).not.toContain(TOKEN);

		// a reviewer cannot release it: the opaque key is not an originKey
		const key = plan.held[0]?.originKey ?? "";
		const applied = await runImport(env.options({ command: "import", apply: true, releaseHeld: [key] }));
		expect(applied.counts.imported).toBe(1);
		expect(JSON.stringify(applied)).not.toContain(TOKEN);
		expect(readFileSync(env.ledger, "utf8")).not.toContain(TOKEN);
		expect(bankMetadata()).not.toContain(TOKEN);
	});

	test("a token in the front matter type is not printed back in a warning", async () => {
		env.memory(projectDir, "typed.md", { name: "Typed note", type: TOKEN }, "A body about types in the prov project.");
		const plan = await runImport(env.options({ command: "dry-run" }));
		expect(JSON.stringify(plan)).not.toContain(TOKEN);
		expect(plan.warnings.some(item => item.error.includes("unknown type"))).toBe(true);
	});

	test("scan withholds a token from every key it lists", async () => {
		writeFileSync(path.join(projectDir, "memory", `${TOKEN}-bin.md`), "binary\0content\0here\n");
		const scan = await runScan(env.options({ command: "scan" }));
		expect(scan.counts.skippedBinary).toBe(1);
		expect(scan.binary).toEqual([{ originKey: expect.stringMatching(/^held#\d+$/), reason: expect.any(String) }]);
		expect(JSON.stringify(scan)).not.toContain(TOKEN);
	});

	test("ordinary provenance (paths, uuids, project tags) is not held", async () => {
		const plan = await runImport(env.options({ command: "dry-run" }));
		expect(plan.held.map(item => item.originKey).filter(key => !key.startsWith("held#"))).toEqual([]);
		expect(plan.perBank.find(item => item.bank === bank)?.wouldImport).toBeGreaterThanOrEqual(1);
	});

	test("a token in a Hindsight tag or document id holds the fact; nothing of it reaches the bank, the ledger or the report", async () => {
		const sms = env.folder("hs");
		env.emptyBank(env.bankOf(sms));
		const exportFile = path.join(env.root, "hs-export.json");
		const map = path.join(env.root, "hs-map.json");
		const fact = (id: string, text: string, extra: Record<string, unknown>) => ({
			id,
			text,
			fact_type: "world",
			state: "valid",
			tags: ["project:hs"],
			context: "coding-agent-retain",
			document_id: `session-${id}`,
			date: "2026-10-01T10:00:00Z",
			...extra,
		});
		writeFileSync(
			exportFile,
			JSON.stringify({
				banks: {
					one: [
						fact("a1", "A fact about the hs project.", {}),
						fact("a2", "A second fact about the hs project.", { tags: ["project:hs", TOKEN] }),
						fact("a3", "A third fact about the hs project.", { document_id: TOKEN }),
					],
				},
			}),
		);
		writeFileSync(map, JSON.stringify({ projects: { hs: sms } }));
		const hs = (over: object) => env.options({ mode: "hindsight", hindsightExport: exportFile, projectMap: map, ...over });
		const plan = await runImport(hs({ command: "dry-run", out: path.join(env.root, "hs-plan.json") }));
		expect(plan.counts.skippedHeld).toBe(2);
		expect(plan.counts.wouldImport).toBe(1);
		expect(JSON.stringify(plan)).not.toContain(TOKEN);
		const applied = await runImport(hs({ command: "import", apply: true }));
		expect(applied.counts.imported).toBe(1);
		expect(readFileSync(env.ledger, "utf8")).not.toContain(TOKEN);
		const db = env.db(env.bankOf(sms));
		try {
			const rows = (db.query("SELECT metadata_json FROM working_memory").all() as { metadata_json: string }[]).map(row => row.metadata_json).join("\n");
			expect(rows).not.toContain(TOKEN);
		} finally {
			db.close();
		}
	});
});

describe("setup --dry-run and registry URLs", () => {
	let root: string;
	beforeAll(() => {
		root = scratch();
	});
	afterAll(() => rmSync(root, { recursive: true, force: true }));

	const base = (over: Partial<Parameters<typeof runSetup>[0]> = {}): Parameters<typeof runSetup>[0] => ({
		ompBin: "omp",
		ompVersion: "9.9.9",
		libDir: path.join(root, "target"),
		dryRun: false,
		embeddings: false,
		say: () => {},
		...over,
	});

	test("a dry run with a matching library already there runs no self-test and writes no cache", async () => {
		const libDir = realpathSync(process.env.OMP_MEMORY_TEST_LIB_DIR ?? path.join(tmpdir(), "omp-memory-test-lib", TEST_OMP_VERSION));
		const cache = path.join(root, "cache");
		const saved = process.env.OMP_MEMORY_CACHE_DIR;
		process.env.OMP_MEMORY_CACHE_DIR = cache;
		const lines: string[] = [];
		try {
			const report = await runSetup(base({ ompVersion: TEST_OMP_VERSION, libDir, dryRun: true, say: line => lines.push(line) }));
			expect(report.action).toBe("dry-run");
			expect(report.ok).toBe(true);
			expect(report.selfTest).toBe("not run");
			expect(report.library).toMatchObject({ version: TEST_OMP_VERSION });
			expect(lines.join("\n")).not.toContain("running the library self-test");
			expect(existsSync(cache)).toBe(false);
		} finally {
			if (saved === undefined) delete process.env.OMP_MEMORY_CACHE_DIR;
			else process.env.OMP_MEMORY_CACHE_DIR = saved;
		}
	});

	test("credentials in a registry URL are refused without echoing them, dry run or not", async () => {
		for (const registry of ["https://user:hunter2@registry.example.test/", "https://token-only@registry.example.test/", "https://registry.example.test/?token=hunter2"]) {
			for (const dryRun of [true, false]) {
				const lines: string[] = [];
				let message = "";
				try {
					await runSetup(base({ registry, dryRun, say: line => lines.push(line) }));
				} catch (error) {
					expect(error).toBeInstanceOf(UsageError);
					message = String(error);
				}
				expect(message).toContain("registry");
				expect(`${message}\n${lines.join("\n")}`).not.toContain("hunter2");
				expect(`${message}\n${lines.join("\n")}`).not.toContain("token-only");
			}
		}
		expect(existsSync(path.join(root, "target"))).toBe(false);
	});

	test("a URL that does not parse is refused without echoing it", async () => {
		await expect(runSetup(base({ registry: "https://user:hunter2@" }))).rejects.toThrow(/registry/);
		let message = "";
		try {
			await runSetup(base({ registry: "https://user:hunter2@" }));
		} catch (error) {
			message = String(error);
		}
		expect(message).not.toContain("hunter2");
	});

	test("the install directory, its package.json and the marker are private", async () => {
		const savedUmask = process.umask(0o022);
		const pack = path.join(root, "pack");
		mkdirSync(path.join(pack, "package", "src", "core", "beam"), { recursive: true });
		writeFileSync(path.join(pack, "package", "package.json"), JSON.stringify({ name: "@oh-my-pi/pi-mnemopi", version: "9.9.9", type: "module", main: "./src/index.ts" }));
		writeFileSync(path.join(pack, "package", "src", "index.ts"), 'export class Mnemopi { constructor() { throw new Error("this library cannot open a bank"); } }\n');
		writeFileSync(path.join(pack, "package", "src", "core", "beam", "helpers.ts"), "export const scheduleEmbedding = () => {};\n");
		writeFileSync(path.join(pack, "package", "src", "core", "beam", "store.ts"), "export const forgetWorking = () => false;\n");
		writeFileSync(path.join(pack, "package", "src", "core", "runtime-options.ts"), "export const withMnemopiRuntimeOptions = (_o: unknown, fn: () => unknown) => fn();\n");
		const tarball = path.join(pack, "pi-mnemopi-9.9.9.tgz");
		expect(Bun.spawnSync(["tar", "-czf", tarball, "-C", pack, "package"]).exitCode).toBe(0);
		const bytes = readFileSync(tarball);
		const integrity = `sha512-${new Bun.CryptoHasher("sha512").update(bytes).digest("base64")}`;
		const shasum = new Bun.CryptoHasher("sha1").update(bytes).digest("hex");
		const target = path.join(root, "private-target");
		const seen: { dir?: number; json?: number; marker?: number } = {};
		const where = { port: 0 };
		const server = Bun.serve({
			port: 0,
			hostname: "127.0.0.1",
			fetch(request): Response {
				const pathname = decodeURIComponent(new URL(request.url).pathname);
				if (pathname.endsWith(".tgz")) {
					seen.dir ??= statSync(target).mode & 0o777;
					seen.json ??= statSync(path.join(target, "package.json")).mode & 0o777;
					return new Response(bytes);
				}
				const dist = { tarball: `http://127.0.0.1:${where.port}/pi-mnemopi-9.9.9.tgz`, integrity, shasum };
				const version = { name: "@oh-my-pi/pi-mnemopi", version: "9.9.9", dist };
				if (pathname.endsWith("/9.9.9")) return Response.json(version);
				return Response.json({ name: "@oh-my-pi/pi-mnemopi", "dist-tags": { latest: "9.9.9" }, versions: { "9.9.9": version } });
			},
		});
		where.port = server.port ?? 0;
		const savedCache = process.env.BUN_INSTALL_CACHE_DIR;
		process.env.BUN_INSTALL_CACHE_DIR = path.join(root, "bun-cache"); // the fake package never enters the user's Bun cache
		try {
			await runSetup(
				base({
					libDir: target,
					registry: `http://127.0.0.1:${where.port}`,
					say: line => {
						if (line.startsWith("installed ")) seen.marker = statSync(path.join(target, SETUP_MARKER)).mode & 0o777;
					},
				}),
			);
		} finally {
			server.stop(true);
			process.umask(savedUmask);
			if (savedCache === undefined) delete process.env.BUN_INSTALL_CACHE_DIR;
			else process.env.BUN_INSTALL_CACHE_DIR = savedCache;
		}
		expect(seen).toEqual({ dir: 0o700, json: 0o600, marker: 0o600 });
	});
});

describe("files in the memory directory", () => {
	let env: Env;
	let cwd: string;
	let projectDir: string;
	let memoryDir: string;
	beforeAll(async () => {
		env = await makeEnv();
		cwd = env.folder("files");
		projectDir = env.project(cwd);
		env.memory(projectDir, "good.md", { name: "Good note" }, "A good note in the files project.");
		memoryDir = path.join(projectDir, "memory");
	});
	afterAll(() => env.cleanup());

	afterEach(() => {
		for (const name of readdirSync(memoryDir)) if (name !== "good.md") rmSync(path.join(memoryDir, name), { recursive: true, force: true });
	});

	const origins = async (): Promise<{ items: string[]; excluded: Record<string, number>; warnings: { where: string; error: string }[] }> => {
		const collected = await collectClaudeMemory({ claudeHome: env.home });
		return { items: collected.items.map(item => path.basename(item.origin)), excluded: collected.excluded, warnings: collected.warnings };
	};

	test("a symlink to a private file outside the memory directory is skipped with a reason", async () => {
		const outside = path.join(env.root, "private");
		mkdirSync(outside);
		writeFileSync(path.join(outside, "diary.txt"), "A private diary that is not a memory file.\n");
		symlinkSync(path.join(outside, "diary.txt"), path.join(memoryDir, "leak.md"));
		const found = await origins();
		expect(found.items).toEqual(["good.md"]);
		expect(Object.keys(found.excluded).some(key => /outside the memory directory/.test(key))).toBe(true);
		expect(found.warnings.some(item => item.where.endsWith("leak.md") && /outside the memory directory/.test(item.error))).toBe(true);
		rmSync(path.join(memoryDir, "leak.md"));
	});

	test("a symlink to a device is skipped, not read", async () => {
		symlinkSync("/dev/null", path.join(memoryDir, "null.md"));
		const found = await origins();
		expect(found.items).toEqual(["good.md"]);
		expect(Object.keys(found.excluded).some(key => /not outside|not a regular file|outside the memory directory/.test(key))).toBe(true);
		rmSync(path.join(memoryDir, "null.md"));
	});

	test("a symlink to a note inside the same memory directory is still allowed and counted once", async () => {
		symlinkSync(path.join(memoryDir, "good.md"), path.join(memoryDir, "again.md"));
		const found = await origins();
		expect(found.items).toEqual(["again.md"]);
		expect(found.excluded["symlink seen twice"]).toBe(1);
		rmSync(path.join(memoryDir, "again.md"));
	});

	test("a memory directory that is a link out of its project is skipped as a whole", async () => {
		const shared = path.join(env.root, "shared-notes");
		mkdirSync(shared);
		writeFileSync(path.join(shared, "shared.md"), "---\nname: Shared\n---\n\nShared elsewhere.\n");
		const other = env.project(env.folder("files-linked"));
		symlinkSync(shared, path.join(other, "memory"));
		const found = await origins();
		expect(found.items).toEqual(["good.md"]);
		expect(Object.keys(found.excluded).some(key => /memory directory/.test(key))).toBe(true);
	});

	test("a file over the byte limit is not read and a FIFO does not block the run", async () => {
		const big = path.join(memoryDir, "big.md");
		writeFileSync(big, Buffer.alloc(4_000_001, 0x61));
		const fifo = path.join(memoryDir, "pipe.md");
		const made = Bun.spawnSync(["mkfifo", fifo]);
		expect(made.exitCode).toBe(0);
		// a subprocess with a deadline: a reader that blocks on the FIFO is killed instead of hanging this test
		const script = `import { collectClaudeMemory } from ${JSON.stringify(path.resolve(import.meta.dir, "../sources/claude_memory"))};
const c = await collectClaudeMemory({ claudeHome: ${JSON.stringify(env.home)} });
console.log(JSON.stringify({ items: c.items.map(i => i.origin.split("/").pop()), excluded: c.excluded }));`;
		const child = Bun.spawnSync([process.execPath, "-e", script], { timeout: 30_000, env: { ...process.env, HOME: env.home } });
		try {
			expect(child.exitCode).toBe(0);
			const found = JSON.parse(child.stdout.toString().trim().split("\n").pop() ?? "{}") as { items: string[]; excluded: Record<string, number> };
			expect(found.items).toEqual(["good.md"]);
			expect(Object.keys(found.excluded).some(key => /not a regular file/.test(key))).toBe(true);
			expect(Object.keys(found.excluded).some(key => /not read/.test(key))).toBe(true);
		} finally {
			rmSync(fifo, { force: true });
			rmSync(big, { force: true });
		}
	});

	test("a text file over the character limit but under the byte limit is still read and reported as too large", async () => {
		const mid = path.join(memoryDir, "mid.md");
		writeFileSync(mid, Buffer.alloc(1_200_000, 0x62));
		const plan = await runImport(env.options({ command: "dry-run" }));
		expect(plan.tooLarge.map(item => item.originKey.split("#").pop())).toEqual(["mid"]);
		rmSync(mid);
	});
});

describe("what a source can make the importer print and other source checks", () => {
	let env: Env;
	let cwd: string;
	let projectDir: string;
	beforeAll(async () => {
		env = await makeEnv();
		cwd = env.folder("print");
		projectDir = env.project(cwd);
		env.emptyBank(env.bankOf(cwd));
	});
	afterAll(() => env.cleanup());

	test("a file name with an escape sequence is held under an opaque key and nothing prints it raw", async () => {
		env.memory(projectDir, "\u001b[31mred\u001b[0m.md", { name: "Red note" }, "A body that is fine, in a file with a colourful name.");
		env.memory(projectDir, "plain.md", { name: "Plain note" }, "Another body that is fine, in a plain file.");
		const plan = await runImport(env.options({ command: "dry-run" }));
		expect(plan.held).toEqual([{ originKey: expect.stringMatching(/^held#\d+$/), kinds: ["hold:provenance-unprintable"] }]);
		expect(plan.counts.wouldImport).toBe(1);
		const text = JSON.stringify(plan);
		expect(text).not.toContain("u001b");
		expect(text).not.toContain("\u001b");
	});

	test("an unreadable Hindsight export is reported by category, never with what the parser saw", async () => {
		const file = path.join(env.root, "broken-export.json");
		writeFileSync(file, `{"banks": ${TOKEN}}`);
		const plan = await runImport(env.options({ mode: "hindsight", hindsightExport: file, command: "dry-run" }));
		expect(plan.errors.map(item => item.error)).toEqual(["unreadable export: not valid JSON"]);
		expect(JSON.stringify(plan)).not.toContain(TOKEN);
	});

	test("a fact type is matched as a property of its own: `constructor` is not an imported type", async () => {
		const file = path.join(env.root, "types-export.json");
		const fact = (id: string, factType: string) => ({ id, text: `A fact of type ${factType}.`, fact_type: factType, state: "valid", tags: [], date: "2026-10-01T10:00:00Z" });
		writeFileSync(file, JSON.stringify({ banks: { one: [fact("t1", "world"), fact("t2", "constructor"), fact("t3", "toString"), fact("t4", "__proto__")] } }));
		const plan = await runImport(env.options({ mode: "hindsight", hindsightExport: file, command: "dry-run" }));
		expect(plan.counts.found).toBe(1);
		expect(Object.keys(plan.excluded).sort()).toEqual(["fact_type __proto__", "fact_type constructor", "fact_type toString"]);
	});

	test("--release-held reads a file only when the value is written as a path", () => {
		const dir = scratch();
		const saved = process.cwd();
		try {
			writeFileSync(path.join(dir, "keys.txt"), "from-the-file#one\n");
			process.chdir(dir);
			expect(releaseList("keys.txt")).toEqual(["keys.txt"]);
			expect(releaseList("./keys.txt")).toEqual(["from-the-file#one"]);
			expect(releaseList(path.join(dir, "keys.txt"))).toEqual(["from-the-file#one"]);
			expect(releaseList("a#b,c#d")).toEqual(["a#b", "c#d"]);
		} finally {
			process.chdir(saved);
			rmSync(dir, { recursive: true, force: true });
		}
	});
});

describe("the claude-mem snapshot copy", () => {
	let dir: string;
	let dbPath: string;
	beforeAll(() => {
		dir = scratch();
		const weird = path.join(dir, "we?ird#dir%41");
		mkdirSync(weird);
		dbPath = path.join(weird, "claude-mem.db");
		const db = new Database(dbPath);
		db.run("CREATE TABLE t (x TEXT)");
		db.run("INSERT INTO t VALUES ('hello')");
		db.close();
	});
	afterAll(() => rmSync(dir, { recursive: true, force: true }));

	test("the database path is percent-encoded: a path with ?, # and % is read as written", () => {
		expect(sqliteUri("/a b/c?d#e%f.db")).toBe("file:/a%20b/c%3Fd%23e%25f.db?mode=ro");
		const snapshot = snapshotDatabase(dbPath);
		try {
			const copy = new Database(snapshot.file, { readonly: true });
			expect(copy.query("SELECT x FROM t").get()).toEqual({ x: "hello" });
			copy.close();
			expect(statSync(snapshot.dir).mode & 0o777).toBe(0o700);
			expect(statSync(snapshot.file).mode & 0o777).toBe(0o600);
		} finally {
			removeSnapshot(snapshot.dir);
		}
		expect(existsSync(snapshot.dir)).toBe(false);
	});

	test("a termination signal between the copy and its removal removes the copy", () => {
		const temp = path.join(dir, "tmp");
		mkdirSync(temp);
		const script = `import { snapshotDatabase } from ${JSON.stringify(path.resolve(import.meta.dir, "../sources/claude_mem"))};
const snapshot = snapshotDatabase(${JSON.stringify(dbPath)});
console.log(snapshot.dir);
process.kill(process.pid, "SIGTERM");
await new Promise(resolve => setTimeout(resolve, 10_000));`;
		const child = Bun.spawnSync([process.execPath, "-e", script], { timeout: 30_000, env: { ...process.env, TMPDIR: temp } });
		const copy = child.stdout.toString().trim().split("\n")[0] ?? "";
		expect(copy.startsWith(temp)).toBe(true);
		expect(existsSync(copy)).toBe(false);
		expect(readdirSync(temp)).toEqual([]);
	});
});

describe("provenance of a home under a random-looking path, and of ordinary long names", () => {
	let env: Env;
	let home: string;
	beforeAll(async () => {
		env = await makeEnv();
		// like macOS's default temp directory: a random-looking segment above the home directory
		home = path.join(env.root, "39", "gmld5wy16qzc6l4j_t1wc93r0000gn", "T", "tmp.k3Jx9QwZ", "home");
		mkdirSync(home, { recursive: true });
	});
	afterAll(() => env.cleanup());

	/** A project folder under `home` and its Claude project directory with one memory file. */
	function note(folderName: string, file: string): string {
		const folder = path.join(home, "work", folderName);
		mkdirSync(folder, { recursive: true });
		const dir = path.join(home, ".claude", "projects", encodeProjectName(folder));
		mkdirSync(path.join(dir, "memory"), { recursive: true });
		writeFileSync(path.join(dir, "11111111-1111-4111-8111-111111111111.jsonl"), `${JSON.stringify({ type: "user", cwd: folder })}\n`);
		writeFileSync(path.join(dir, "memory", file), `---\nname: Plain note\n---\n\nA plain body about the project.\n`);
		return folder;
	}

	test("the home prefix, as written or by real path, is not scanned: the plan holds nothing", async () => {
		note("plain", "plain.md");
		const plan = await runImport(env.options({ command: "dry-run", claudeHome: home }));
		expect(plan.held).toEqual([]);
		expect(plan.counts).toMatchObject({ found: 1, wouldImport: 1, skippedHeld: 0 });
	});

	test("a hash-like folder name and a long file name without secret context are not held", async () => {
		note("worktree-3f9a1c2e7b8d4e6f9a0b1c2d3e4f5a6b", "token_refresh_flow_design.md");
		note("Cfg_AbCdEfGhIjKlMnOpQrStUvWxYz0123", "notes.md");
		const plan = await runImport(env.options({ command: "dry-run", claudeHome: home }));
		expect(plan.held).toEqual([]);
		expect(plan.counts.found).toBe(3);
	});

	test("a token in a folder name below home is still held, under an opaque key", async () => {
		note(`leak-${TOKEN}`, "leak.md");
		const plan = await runImport(env.options({ command: "dry-run", claudeHome: home }));
		expect(plan.held).toEqual([{ originKey: expect.stringMatching(/^held#\d+$/), kinds: ["hold:provenance-secret"] }]);
		expect(JSON.stringify(plan)).not.toContain(TOKEN);
	});

	test("the bank id derived from such a folder is named opaquely in every report, and no bank is made under that name", async () => {
		const out = path.join(env.root, "derived.json");
		const plan = await runImport(env.options({ command: "dry-run", claudeHome: home, out }));
		expect(plan.perBank.map(item => item.bank).filter(bank => bank.startsWith("bank#"))).toEqual([expect.stringMatching(/^bank#\d+$/)]);
		const scan = await runScan(env.options({ command: "scan", claudeHome: home }));
		for (const text of [JSON.stringify(scan), readFileSync(out, "utf8"), readFileSync(plan.samplesFile ?? "", "utf8")]) expect(text).not.toContain(TOKEN);
		expect(scan.perBank.map(item => item.bank)).toContain(plan.perBank.find(item => item.bank.startsWith("bank#"))?.bank ?? "");
		const applied = await runImport(env.options({ command: "import", apply: true, claudeHome: home }));
		expect(applied.counts.imported).toBe(3);
		expect(JSON.stringify(applied)).not.toContain(TOKEN);
		expect(readdirSync(env.banksDir).filter(name => name.includes(TOKEN))).toEqual([]);
		expect(readFileSync(env.ledger, "utf8")).not.toContain(TOKEN);
	});
});

describe("project-map output", () => {
	let dir: string;
	beforeAll(() => {
		dir = scratch();
		const fact = (id: string, tag: string) => ({ id, text: `A fact about ${tag}.`, fact_type: "world", state: "valid", tags: [tag], date: "2026-10-01T10:00:00Z" });
		writeFileSync(path.join(dir, "export.json"), JSON.stringify({ banks: { one: [fact("a", "project:plain"), fact("b", `project:${TOKEN}`), fact("c", "project:\u001b[31mred")] } }));
		mkdirSync(path.join(dir, "home", "work", "plain"), { recursive: true });
	});
	afterAll(() => rmSync(dir, { recursive: true, force: true }));

	test("a project tag that carries a secret or an escape sequence is named by an opaque label in the draft, on stdout and in the file", () => {
		const out = path.join(dir, "map.json");
		const folders = [path.join(dir, "home", "work", "plain")];
		const draft = draftProjectMap(path.join(dir, "export.json"), folders);
		const keys = Object.keys(draft.projects);
		expect(keys).toContain("plain");
		const opaque = keys.filter(key => key !== "plain");
		expect(opaque).toHaveLength(2);
		for (const key of opaque) expect(key).toMatch(/^project#\d+$/);
		expect(draft.projects.plain).toBe(folders[0]);
		const ran = Bun.spawnSync([process.execPath, path.resolve(import.meta.dir, "../cli.ts"), "project-map", "--hindsight-export", path.join(dir, "export.json"), "--folder-roots", path.join(dir, "home", "work"), "--claude-home", path.join(dir, "home"), "--out", out], {
			env: { ...process.env, HOME: path.join(dir, "home") },
			timeout: 60_000,
		});
		expect(ran.exitCode).toBe(0);
		for (const text of [ran.stdout.toString(), ran.stderr.toString(), readFileSync(out, "utf8")]) {
			expect(text).not.toContain(TOKEN);
			expect(text).not.toContain("\u001b");
		}
		expect(JSON.parse(readFileSync(out, "utf8")).format).toBe("omp-memory-project-map/1");
	});
});

describe("files this tool writes carry a format marker", () => {
	let env: Env;
	beforeAll(async () => {
		env = await makeEnv();
		const cwd = env.folder("marked");
		env.memory(env.project(cwd), "note.md", { name: "Marked note" }, "A plain note for the marked project.");
		env.emptyBank(env.bankOf(cwd));
	});
	afterAll(() => env.cleanup());

	test("reports and samples files are marked", async () => {
		const out = path.join(env.root, "marked.json");
		const plan = await runImport(env.options({ command: "dry-run", out }));
		expect(JSON.parse(readFileSync(out, "utf8")).format).toBe("omp-memory-report/1");
		expect(JSON.parse(readFileSync(plan.samplesFile ?? "", "utf8")).format).toBe("omp-memory-samples/1");
	});

	test("unrelated JSON that has the keys of a report is not replaced; a marked file is", async () => {
		const lookalike = path.join(env.root, "lookalike.json");
		const text = JSON.stringify({ runId: "x", counts: { found: 1 }, problems: [], note: "mine", samples: {}, projects: { a: "/b" } });
		writeFileSync(lookalike, text);
		await expect(runScan(env.options({ command: "scan", out: lookalike }))).rejects.toThrow(/format marker/);
		expect(readFileSync(lookalike, "utf8")).toBe(text);

		const marked = path.join(env.root, "marked-again.json");
		await runScan(env.options({ command: "scan", out: marked }));
		await runScan(env.options({ command: "scan", out: marked }));
		expect(JSON.parse(readFileSync(marked, "utf8")).format).toBe("omp-memory-report/1");

		const old = path.join(env.root, "unmarked.json");
		const { format: _dropped, ...unmarked } = JSON.parse(readFileSync(marked, "utf8"));
		writeFileSync(old, JSON.stringify(unmarked));
		await expect(runScan(env.options({ command: "scan", out: old }))).rejects.toThrow(/remove it first/);
	});
});

describe("the shared embedding runtime directory after a failed embedding", () => {
	const failing = (onEmbed: () => void) => (): { embed(): Promise<number[][]>; close(): void } => ({
		embed: async () => {
			onEmbed();
			throw new Error("the model could not be downloaded");
		},
		close() {},
	});

	/**
	 * Runs a setup whose embedding fails, against the runtime directory the library would use (only inside a scratch home: nothing
	 * outside the system temp directory is ever touched). `before` plays whatever exists before the run, `onEmbed` what happens
	 * to the directory during the embedding step.
	 */
	async function failingSetup(onEmbed: (dir: string) => void, before?: (dir: string) => void): Promise<{ dir: string; lines: string[] } | null> {
		const libDir = await ensureTestLibrary();
		const dir = await fastembedRuntimeDir(libDir);
		if (dir === null || !isInside(realpathSync(tmpdir()), dir) || existsSync(dir)) return null;
		const cache = scratch();
		const saved = process.env.OMP_MEMORY_CACHE_DIR;
		process.env.OMP_MEMORY_CACHE_DIR = cache;
		before?.(dir);
		const lines: string[] = [];
		try {
			const report = await runSetup({ ompBin: "omp", ompVersion: TEST_OMP_VERSION, libDir, dryRun: false, embeddings: true, embedderFactory: failing(() => onEmbed(dir)), say: line => lines.push(line) });
			expect(report.ok).toBe(false);
			return { dir, lines };
		} finally {
			rmSync(cache, { recursive: true, force: true });
			if (saved === undefined) delete process.env.OMP_MEMORY_CACHE_DIR;
			else process.env.OMP_MEMORY_CACHE_DIR = saved;
		}
	}

	const ownerFiles = (dir: string): string[] => readdirSync(dir).filter(name => name.startsWith(".omp-memory-owner-"));

	test("a directory that was there before the run is never claimed or touched", async () => {
		const ran = await failingSetup(
			() => {},
			dir => {
				mkdirSync(dir, { recursive: true });
				writeFileSync(path.join(dir, "keep.txt"), "an installed runtime\n");
			},
		);
		if (ran === null) return;
		try {
			expect(readFileSync(path.join(ran.dir, "keep.txt"), "utf8")).toBe("an installed runtime\n");
			expect(ownerFiles(ran.dir)).toEqual([]);
			expect(ran.lines.join("\n")).toContain("was not made by this run");
		} finally {
			rmSync(ran.dir, { recursive: true, force: true });
		}
	});

	test("a directory this run made and nothing wrote to is removed again, with the empty parents it made", async () => {
		const ran = await failingSetup(() => {});
		if (ran === null) return;
		expect(existsSync(ran.dir)).toBe(false);
		expect(ran.lines.join("\n")).toContain("removed the empty embedding runtime directory");
	});

	test("install files that appear during the failing step, quiet before the failure, are all preserved; only the token goes", async () => {
		const ran = await failingSetup(dir => {
			mkdirSync(path.join(dir, "node_modules", "x"), { recursive: true });
			writeFileSync(path.join(dir, "node_modules", "x", "index.js"), "module.exports = 1;\n");
			writeFileSync(path.join(dir, "package.json"), "{}\n");
			writeFileSync(path.join(dir, "bun.lock"), "lock\n");
		});
		if (ran === null) return;
		try {
			expect(readFileSync(path.join(ran.dir, "node_modules", "x", "index.js"), "utf8")).toBe("module.exports = 1;\n");
			expect(readFileSync(path.join(ran.dir, "package.json"), "utf8")).toBe("{}\n");
			expect(readFileSync(path.join(ran.dir, "bun.lock"), "utf8")).toBe("lock\n");
			expect(ownerFiles(ran.dir)).toEqual([]);
			expect(ran.lines.join("\n")).toContain("this run did not make");
			expect(ran.lines.join("\n")).toContain("remove that directory yourself");
		} finally {
			rmSync(ran.dir, { recursive: true, force: true });
		}
	});

	test("any other file another process leaves there is preserved too", async () => {
		const ran = await failingSetup(dir => writeFileSync(path.join(dir, "keep.txt"), "another process was here\n"));
		if (ran === null) return;
		try {
			expect(readFileSync(path.join(ran.dir, "keep.txt"), "utf8")).toBe("another process was here\n");
			expect(ownerFiles(ran.dir)).toEqual([]);
		} finally {
			rmSync(ran.dir, { recursive: true, force: true });
		}
	});

	test("the claim and its release: only the token and the empty directories this run made go, each only while it is as recorded", () => {
		const root = scratch();
		try {
			expect(claimRuntimeDir(root)).toBeNull(); // there already
			const nothing = claimRuntimeDir(path.join(root, "a", "b", "c"));
			expect(nothing?.created).toEqual([path.join(root, "a"), path.join(root, "a", "b"), path.join(root, "a", "b", "c")]);
			expect(ownerFiles(path.join(root, "a", "b", "c"))).toHaveLength(1);
			expect(releaseRuntimeClaim(nothing as NonNullable<typeof nothing>, true)).toContain("removed the empty embedding runtime directory");
			expect(existsSync(path.join(root, "a"))).toBe(false);

			const success = claimRuntimeDir(path.join(root, "d", "e"));
			expect(releaseRuntimeClaim(success as NonNullable<typeof success>, false)).toBe("");
			expect(readdirSync(path.join(root, "d", "e"))).toEqual([]); // after a success the directories stay: they hold the runtime

			const filled = claimRuntimeDir(path.join(root, "f", "g"));
			writeFileSync(path.join(root, "f", "g", "theirs.txt"), "x\n");
			expect(releaseRuntimeClaim(filled as NonNullable<typeof filled>, true)).toContain("holds 1 entry this run did not make");
			expect(readdirSync(path.join(root, "f", "g"))).toEqual(["theirs.txt"]);

			const rewritten = claimRuntimeDir(path.join(root, "h"));
			writeFileSync((rewritten as NonNullable<typeof rewritten>).tokenFile, "no longer an empty token\n");
			releaseRuntimeClaim(rewritten as NonNullable<typeof rewritten>, true);
			expect(ownerFiles(path.join(root, "h"))).toHaveLength(1); // not what it recorded: left alone, and so is its directory

			const replaced = claimRuntimeDir(path.join(root, "i"));
			rmSync(path.join(root, "i"), { recursive: true });
			mkdirSync(path.join(root, "i"));
			expect(releaseRuntimeClaim(replaced as NonNullable<typeof replaced>, true)).toContain("changed after this run created it");
			expect(existsSync(path.join(root, "i"))).toBe(true);
		} finally {
			rmSync(root, { recursive: true, force: true });
		}
	});
});

describe("withheld labels are decided on the raw name and reveal nothing", () => {
	const SECRET_A = "Kj83Hq72";
	const PIN = "73915";
	let env: Env;
	let home: string;
	beforeAll(async () => {
		env = await makeEnv();
		home = env.home;
	});
	afterAll(() => env.cleanup());

	/** A project folder `name` under home with one plain note (the name is only in the path). */
	function note(name: string, file = "note.md"): string {
		const folder = path.join(home, "work", name);
		mkdirSync(folder, { recursive: true });
		const dir = path.join(home, ".claude", "projects", encodeProjectName(folder));
		mkdirSync(path.join(dir, "memory"), { recursive: true });
		writeFileSync(path.join(dir, "11111111-1111-4111-8111-111111111111.jsonl"), `${JSON.stringify({ type: "user", cwd: folder })}\n`);
		writeFileSync(path.join(dir, "memory", file), "---\nname: Plain note\n---\n\nA plain body about nothing in particular.\n");
		return folder;
	}

	const positives = [
		`password=${SECRET_A}`,
		`password-${PIN}`,
		`password_${PIN}`,
		`PASSWORD=${SECRET_A}`,
		`password%3D${PIN}`,
		`password%253D${PIN}`,
		`token: ${SECRET_A}`,
		`my_password_${PIN}_notes`,
		`secret.${PIN}`,
		`password${PIN}`,
		`API_KEY=${SECRET_A}`,
		`api key ${PIN}`,
		`work/token%3A${SECRET_A}/sub`,
	];
	const negatives = ["token-service", "Secret Santa", "password-policy", "token_refresh_flow_design", "tokenizer-2", "passwords", "docs-site", "worktree-3f9a1c2e7b8d4e6f9a0b1c2d3e4f5a6b"];

	test("a key word followed by a value is found however a separator or an escape is spelled", () => {
		for (const name of positives) expect({ name, found: carriesSecret(name) }).toEqual({ name, found: true });
		for (const name of negatives) expect({ name, found: carriesSecret(name) }).toEqual({ name, found: false });
	});

	test("every report names the banks of such folders by an index, and no value, hash or form of it appears anywhere", async () => {
		const folders = [`password=${SECRET_A}`, `password-${PIN}`, `PASSWORD=${SECRET_A}x`, `password%3D${PIN}1`, `my_password_${PIN}2_notes`, `secret.${PIN}3`, "docs-site"];
		const banks = folders.map(name => env.bankOf(note(name)));
		const out = path.join(env.root, "labels.json");
		const plan = await runImport(env.options({ command: "dry-run", out }));
		const scan = await runScan(env.options({ command: "scan" }));
		const applied = await runImport(env.options({ command: "import", apply: true }));
		const texts = [JSON.stringify(plan), JSON.stringify(scan), JSON.stringify(applied), readFileSync(out, "utf8"), readFileSync(plan.samplesFile ?? "", "utf8"), readFileSync(env.ledger, "utf8")];
		const forbidden = [SECRET_A, PIN, "Kj83", "73915"];
		for (const value of [...folders.slice(0, 6), ...banks.slice(0, 6)]) forbidden.push(value, new Bun.CryptoHasher("sha256").update(value).digest("hex").slice(0, 16), new Bun.CryptoHasher("sha256").update(value).digest("hex").slice(0, 8));
		for (const text of texts) for (const value of forbidden) expect(text.includes(value)).toBe(false);

		const labels = plan.perBank.map(item => item.bank).filter(bank => bank.startsWith("bank#"));
		expect(labels).toEqual([1, 2, 3, 4, 5, 6].map(n => `bank#${n}`));
		expect(plan.perBank.find(item => item.bank === banks[6])).toBeDefined(); // the plain folder keeps its own id
		expect(plan.held).toHaveLength(6);
		expect(new Set(plan.held.map(item => item.originKey)).size).toBe(6);
		for (const item of plan.held) expect(item.originKey).toMatch(/^held#\d+$/);
		expect(applied.counts.imported).toBe(1);
		expect(readdirSync(env.banksDir).filter(name => /password|secret|Kj83|73915/i.test(name))).toEqual([]);
	});

	test("a repeated run gives the same labels, and one more secret folder takes the next index", async () => {
		const labelsOf = async () => (await runImport(env.options({ command: "dry-run" }))).perBank.map(item => item.bank).filter(bank => bank.startsWith("bank#"));
		const first = await labelsOf();
		expect(await labelsOf()).toEqual(first);
		note(`zz-token-Qw5Er6Ty`);
		expect(await labelsOf()).toEqual([...first, `bank#${first.length + 1}`]);
	});

	test("a Hindsight tag with a short secret is held by index and shows in no draft or report", async () => {
		const folder = env.folder("hs-labels");
		env.emptyBank(env.bankOf(folder));
		const exportFile = path.join(env.root, "hs-labels.json");
		const fact = (id: string, tags: string[]) => ({ id, text: `A fact about ${id}.`, fact_type: "world", state: "valid", tags, date: "2026-10-01T10:00:00Z" });
		writeFileSync(
			exportFile,
			JSON.stringify({ banks: { one: [fact("t1", ["project:plain"]), fact("t2", ["project:plain", `password=${PIN}9`]), fact("t3", [`project:token-${SECRET_A}`]), fact("t4", ["project:Password%3D" + PIN + "8"])] } }),
		);
		const map = path.join(env.root, "hs-labels-map.json");
		writeFileSync(map, JSON.stringify({ projects: { plain: folder } }));
		const plan = await runImport(env.options({ mode: "hindsight", hindsightExport: exportFile, projectMap: map, command: "dry-run", out: path.join(env.root, "hs-labels-plan.json") }));
		expect(plan.held.length).toBeGreaterThanOrEqual(1);
		const text = `${JSON.stringify(plan)}${readFileSync(plan.samplesFile ?? "", "utf8")}`;
		for (const value of [PIN, SECRET_A]) expect(text.includes(value)).toBe(false);
		const out = path.join(env.root, "hs-draft.json");
		const draft = Bun.spawnSync([process.execPath, path.resolve(import.meta.dir, "../cli.ts"), "project-map", "--hindsight-export", exportFile, "--out", out, "--claude-home", home], {
			env: { ...process.env, HOME: home },
			timeout: 60_000,
		});
		expect(draft.exitCode).toBe(0);
		for (const shown of [draft.stdout.toString(), draft.stderr.toString(), readFileSync(out, "utf8")]) for (const value of [PIN, SECRET_A]) expect(shown.includes(value)).toBe(false);
		const keys = Object.keys(JSON.parse(readFileSync(out, "utf8")).projects);
		expect(keys.filter(key => key !== "plain").every(key => /^project#\d+$/.test(key))).toBe(true);
	});
});
