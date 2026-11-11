/**
 * Content that is never imported (binary, oversize), what `scan` promises about its numbers, the number a dry run records
 * (`counts.wouldImport`), and what counts as a new bank after an undo.
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync, realpathSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { binaryReason, charsOverLimit, MAX_REPLACEMENT_FRACTION, MAX_TEXT_CHARS } from "../content_guard";
import { runImport, runScan, runUndo } from "../engine";
import { loadLedger } from "../ledger";
import { tildeText } from "../model";
import { type Env, insertNativeRow, makeEnv } from "./helpers";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

describe("what counts as binary or too large", () => {
	test("a NUL byte, or more than 2% replacement characters, is binary; a few replacement characters are not", () => {
		expect(binaryReason("plain text\n")).toBeNull();
		expect(binaryReason("")).toBeNull();
		expect(binaryReason("a\0b")).toContain("NUL");
		expect(binaryReason(`${"x".repeat(98)}\uFFFD\uFFFD`)).toBeNull(); // exactly 2%
		expect(binaryReason(`${"x".repeat(97)}\uFFFD\uFFFD\uFFFD`)).toContain("not valid text");
		expect(MAX_REPLACEMENT_FRACTION).toBe(0.02);
	});

	test("the limit is the redaction step's own: 1,000,000 characters, counted as code points", () => {
		expect(charsOverLimit("x".repeat(MAX_TEXT_CHARS))).toBeNull();
		expect(charsOverLimit("x".repeat(MAX_TEXT_CHARS + 1))).toBe(MAX_TEXT_CHARS + 1);
		expect(charsOverLimit("\u{1F600}".repeat(MAX_TEXT_CHARS / 2 + 1))).toBeNull(); // 500,001 emoji are 1,000,002 UTF-16 units but under the limit
		const redact = readFileSync(path.join(import.meta.dir, "..", "..", "hindsight_proxy", "redact.py"), "utf8");
		expect(redact).toMatch(new RegExp(`MAX_TEXT_CHARS\\s*=\\s*${MAX_TEXT_CHARS.toString().replace(/(\d)(?=(\d{3})+$)/g, "$1_?")}`));
	});
});

describe("binary and oversize files in an import", () => {
	let env: Env;
	let bank: string;
	let ledgerSnapshot = "";

	beforeAll(async () => {
		env = await makeEnv();
		const cwd = env.folder("content");
		bank = env.bankOf(cwd);
		const project = env.project(cwd);
		env.memory(project, "good.md", { name: "Good", type: "project" }, "A plain note about the content project.");
		const memoryDir = path.join(project, "memory");
		const random = new Uint8Array(200_000);
		crypto.getRandomValues(random);
		for (const [index, byte] of random.entries()) if (byte === 0) random[index] = 0xfe; // no NUL: this file is caught by the replacement-character rule
		random[0] = 0xff; // never valid UTF-8
		writeFileSync(path.join(memoryDir, "random.md"), random);
		writeFileSync(path.join(memoryDir, "nul.md"), "---\nname: Nul\n---\n\nbefore\0after, with a NUL byte in the middle\n");
		writeFileSync(path.join(memoryDir, "huge.md"), `---\nname: Huge\n---\n\n${"y".repeat(MAX_TEXT_CHARS + 10)}\n`);
		env.emptyBank(bank);
	});
	afterAll(() => env.cleanup());

	test("the dry run lists them by key with a reason, imports none of them and counts no error", async () => {
		const report = await runImport(env.options({ command: "dry-run", out: path.join(env.root, "plan.json") }));
		expect(report.counts).toMatchObject({ found: 4, wouldImport: 1, skippedBinary: 2, skippedTooLarge: 1, errors: 0, skippedHeld: 0 });
		expect(report.binary.map(item => item.originKey.split("#").pop()).sort()).toEqual(["nul", "random"]);
		const nul = report.binary.find(item => item.originKey.endsWith("#nul"));
		expect(nul?.reason).toContain("NUL");
		const random = report.binary.find(item => item.originKey.endsWith("#random"));
		expect(random?.reason).toContain("not valid text");
		expect(report.tooLarge).toHaveLength(1);
		expect(report.tooLarge[0]).toMatchObject({ chars: expect.any(Number), limit: 1_000_000 });
		expect(report.tooLarge[0]?.reason).toContain("the limit is 1,000,000 characters");
		expect(report.perBank).toEqual([expect.objectContaining({ bank, wouldImport: 1 })]);
		const written = readFileSync(path.join(env.root, "plan.json"), "utf8");
		expect(written).not.toContain("with a NUL byte"); // nothing of a skipped file is in the report
		expect(readFileSync(report.samplesFile ?? "", "utf8")).not.toContain("NUL byte");
	});

	test("an apply imports only the good file, and a re-run skips the same three the same way", async () => {
		const applied = await runImport(env.options({ command: "import", apply: true }));
		expect(applied.counts).toMatchObject({ imported: 1, skippedBinary: 2, skippedTooLarge: 1, errors: 0 });
		const db = env.db(bank);
		try {
			const rows = db.query("SELECT content FROM working_memory").all() as { content: string }[];
			expect(rows).toHaveLength(1);
			expect(rows.every(row => !row.content.includes("\0") && !row.content.includes("\uFFFD"))).toBe(true);
		} finally {
			db.close();
		}
		const again = await runImport(env.options({ command: "dry-run" }));
		expect(again.counts).toMatchObject({ wouldImport: 0, skippedUnchanged: 1, skippedBinary: 2, skippedTooLarge: 1 });
		ledgerSnapshot = readFileSync(env.ledger, "utf8");
		expect(Object.keys(loadLedger(env.ledger).entries)).toHaveLength(1);
	});

	test("scan recognises them too, labels its numbers as an upper bound, and never goes below the dry run", async () => {
		const scan = await runScan(env.options({ command: "scan" }));
		expect(scan.counts).toMatchObject({ found: 4, skippedBinary: 2, skippedTooLarge: 1 });
		expect(scan.extra).toMatchObject({ wouldImportIsUpperBound: true });
		expect(String(scan.extra?.note)).toContain("upper bound");
		const plan = await runImport(env.options({ command: "dry-run" }));
		expect(scan.counts.wouldImport).toBeGreaterThanOrEqual(plan.counts.wouldImport);
		expect(readFileSync(env.ledger, "utf8")).toBe(ledgerSnapshot); // scan opens no ledger for writing
	});
});

describe("scan against dry run, and the recorded plan size", () => {
	let env: Env;

	beforeAll(async () => {
		env = await makeEnv();
		const cwd = env.folder("numbers");
		const project = env.project(cwd);
		env.memory(project, "a.md", { name: "A" }, "First ordinary note.");
		env.memory(project, "b.md", { name: "B" }, "Second ordinary note.");
		env.memory(project, "secret.md", { name: "Secret" }, "the deploy password: hunter2hunter2 is in the vault");
		env.emptyBank(env.bankOf(cwd));
	});
	afterAll(() => env.cleanup());

	test("a held item is in scan's wouldImport but not in the dry run's; counts.wouldImport is what the dry run would write", async () => {
		const scan = await runScan(env.options({ command: "scan" }));
		const plan = await runImport(env.options({ command: "dry-run" }));
		expect(plan.counts.skippedHeld).toBe(1);
		expect(scan.counts.wouldImport).toBe(3);
		expect(plan.counts.wouldImport).toBe(2);
		expect(plan.perBank.reduce((sum, item) => sum + item.wouldImport, 0)).toBe(plan.counts.wouldImport);
		const applied = await runImport(env.options({ command: "import", apply: true }));
		expect(applied.counts.imported).toBe(plan.counts.wouldImport);
	});
});

describe("--only-new-banks after an undo", () => {
	let env: Env;
	let bankNew: string;
	let bankOld: string;
	let secondRun = "";

	beforeAll(async () => {
		env = await makeEnv();
		const [fresh, old] = [env.folder("fresh"), env.folder("old")] as [string, string];
		[bankNew, bankOld] = [env.bankOf(fresh), env.bankOf(old)];
		env.memory(env.project(fresh), "f.md", { name: "F" }, "Note for the bank this tool creates.");
		env.memory(env.project(old), "o.md", { name: "O" }, "Note for a bank that already existed.");
		env.emptyBank(bankOld); // an empty bank omp made: not created by this tool, so never "new"
	});
	afterAll(() => env.cleanup());

	test("a bank the importer created is new again once its rows are undone; one that was there before is not", async () => {
		const first = await runImport(env.options({ command: "import", apply: true, onlyNewBanks: true }));
		expect(first.counts.imported).toBe(1);
		const ledger = loadLedger(env.ledger);
		expect(Object.keys(ledger.banksCreated)).toEqual([bankNew]);

		await runUndo(env.options({ command: "undo", apply: true, targetRun: first.runId }));
		const plan = await runImport(env.options({ command: "dry-run", onlyNewBanks: true }));
		expect(plan.perBank.find(item => item.bank === bankNew)).toMatchObject({ wouldImport: 1, notTargeted: 0 });
		expect(plan.perBank.find(item => item.bank === bankOld)).toMatchObject({ wouldImport: 0, notTargeted: 1 });
		const again = await runImport(env.options({ command: "import", apply: true, onlyNewBanks: true }));
		expect(again.counts.imported).toBe(1);
		secondRun = again.runId;
	});

	test("a created bank that holds a row of omp's own is no longer new", async () => {
		await runUndo(env.options({ command: "undo", apply: true, targetRun: secondRun }));
		const db = env.db(bankNew);
		insertNativeRow(db, bankNew, "native-after-undo", "A note omp retained after the undo.");
		db.close();
		const plan = await runImport(env.options({ command: "dry-run", onlyNewBanks: true }));
		expect(plan.perBank.find(item => item.bank === bankNew)).toMatchObject({ wouldImport: 0, notTargeted: 1 });
	});
});

describe("paths are printed under ~", () => {
	test("tildeText handles a home written as a link and a real path, and leaves other paths alone", () => {
		const real = realpathSync(mkdtempSync(path.join(tmpdir(), "tilde-test-")));
		const link = `${real}-link`;
		symlinkSync(real, link);
		expect(tildeText(`held: ${real}/work/x#secret\tk`, link)).toBe("held: ~/work/x#secret\tk");
		expect(tildeText(`into ${link}/.cache/x`, link)).toBe("into ~/.cache/x");
		expect(tildeText("/elsewhere/x", link)).toBe("/elsewhere/x");
	});
});
