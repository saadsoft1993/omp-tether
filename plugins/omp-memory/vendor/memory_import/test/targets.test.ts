/** --only-new-banks and --banks: target filters, with an existing bank held open by a real process. */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import path from "node:path";
import { runImport } from "../engine";
import { loadLedger } from "../ledger";
import { type Env, holdOpen, makeEnv } from "./helpers";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

let env: Env;
let bankA: string;
let bankB: string;
let bankC: string;
let holder: { pid: number; close(): void } | null = null;

const rowsIn = (bank: string): number => {
	const db = env.db(bank);
	try {
		return (db.query("SELECT COUNT(*) AS n FROM working_memory").get() as { n: number }).n;
	} finally {
		db.close();
	}
};

beforeAll(async () => {
	env = await makeEnv();
	const [a, b, c] = [env.folder("ta"), env.folder("tb"), env.folder("tc")] as [string, string, string];
	[bankA, bankB, bankC] = [env.bankOf(a), env.bankOf(b), env.bankOf(c)];
	env.memory(env.project(a), "a1.md", { name: "A one" }, "Target A first note.");
	env.memory(env.project(b), "b1.md", { name: "B one" }, "Target B first note.");
	env.memory(env.project(b), "b2.md", { name: "B two" }, "Target B second note.");
	env.memory(env.project(c), "c1.md", { name: "C one" }, "Target C first note.");
	env.emptyBank(bankA); // A exists; B and C are new
	holder = await holdOpen(path.join(env.banksDir, bankA, "mnemopi.db"));
});
afterAll(() => {
	holder?.close();
	env.cleanup();
});

const opts = (over = {}) => env.options({ forceClosed: false, ...over });

describe("target filters", () => {
	test("dry-run honours --only-new-banks and its counts equal what apply then does", async () => {
		const plan = await runImport(opts({ command: "dry-run", onlyNewBanks: true }));
		expect(plan.counts.skippedNotTargeted).toBe(1);
		const planned = plan.perBank.reduce((sum, item) => sum + item.wouldImport, 0);
		expect(planned).toBe(3);
		expect(plan.perBank.find(item => item.bank === bankA)).toMatchObject({ wouldImport: 0, notTargeted: 1 });
		const applied = await runImport(opts({ command: "import", apply: true, onlyNewBanks: true }));
		expect(applied.counts.imported).toBe(planned);
		expect(applied.counts.skippedNotTargeted).toBe(plan.counts.skippedNotTargeted);
		expect(rowsIn(bankB)).toBe(2);
		expect(rowsIn(bankC)).toBe(1);
	});

	test("the open existing bank is untouched and nothing of it is in the ledger", () => {
		expect(rowsIn(bankA)).toBe(0);
		expect(Object.values(loadLedger(env.ledger).entries).some(entry => entry.bank === bankA)).toBe(false);
	});

	test("an open bank that is targeted still refuses, by name or without a filter", async () => {
		await expect(runImport(opts({ command: "import", apply: true, banks: [bankA] }))).rejects.toThrow(/open in process/);
		await expect(runImport(opts({ command: "import", apply: true }))).rejects.toThrow(/open in process/);
		await expect(runImport(opts({ command: "import", apply: true, onlyNewBanks: true, banks: [bankA] }))).rejects.toThrow(/open in process/);
		expect(rowsIn(bankA)).toBe(0);
	});

	test("unknown bank names are refused and listed", async () => {
		await expect(runImport(opts({ command: "dry-run", banks: [bankB, "nope-1", "nope-2"] }))).rejects.toThrow(/nope-1, nope-2/);
	});

	test("--banks targets only the named bank", async () => {
		const plan = await runImport(opts({ command: "dry-run", banks: [bankB] }));
		expect(plan.counts.skippedUnchanged).toBe(3); // B and C were imported above
		expect(plan.counts.skippedNotTargeted).toBe(1); // A's note: its bank is not named
	});

	test("after the bank is closed, a run without the filter imports the remainder, no duplicates", async () => {
		holder?.close();
		holder = null;
		const before = rowsIn(bankB) + rowsIn(bankC);
		const rest = await runImport(opts({ command: "import", apply: true }));
		expect(rest.counts.imported).toBe(1);
		expect(rest.counts.skippedUnchanged).toBe(3);
		expect(rest.counts.skippedNotTargeted).toBe(0);
		expect(rowsIn(bankA)).toBe(1);
		expect(rowsIn(bankB) + rowsIn(bankC)).toBe(before);
		expect(Object.keys(loadLedger(env.ledger).entries)).toHaveLength(4);
	});
});
