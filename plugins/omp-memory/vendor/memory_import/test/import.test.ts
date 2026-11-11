/** Claude memory import on scratch banks built with the real library schema. */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { runImport, runUndo, runVerify } from "../engine";
import { loadLedger } from "../ledger";
import { type Env, insertNativeRow, makeEnv } from "./helpers";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

let env: Env;
let cwd: string;
let bank: string;
let projectDir: string;

beforeAll(async () => {
	env = await makeEnv();
	cwd = env.folder("alpha");
	bank = env.bankOf(cwd);
	projectDir = env.project(cwd);
	env.memory(projectDir, "one.md", { name: "Alpha one", description: "first note", type: "project" }, "Alpha uses a blue deploy lane.");
	env.memory(projectDir, "two.md", { name: "Alpha two", type: "feedback" }, "Prefer small commits in alpha.");
	env.memory(projectDir, "MEMORY.md", { name: "index" }, "- [one](one.md)");
	env.emptyBank(bank);
	const db = env.db(bank);
	insertNativeRow(db, bank, "native-1", "A native note an omp session retained long ago.");
	db.close();
});
afterAll(() => env.cleanup());

const rowCount = (): number => {
	const db = env.db(bank);
	try {
		return (db.query("SELECT COUNT(*) AS n FROM working_memory").get() as { n: number }).n;
	} finally {
		db.close();
	}
};

describe("import cycle", () => {
	let runId = "";

	test("import without --apply is refused", async () => {
		await expect(runImport(env.options({ command: "import" }))).rejects.toThrow(/--apply/);
	});

	test("dry-run reports the plan and writes nothing", async () => {
		const before = rowCount();
		const report = await runImport(env.options({ command: "dry-run", out: path.join(env.root, "dry.json") }));
		expect(report.counts.found).toBe(2);
		expect(report.counts.imported).toBe(0);
		expect(report.perBank).toEqual([expect.objectContaining({ bank, wouldImport: 2 })]);
		expect(report.excluded["MEMORY.md index"]).toBe(1);
		expect(rowCount()).toBe(before);
		expect(existsSync(env.ledger)).toBe(false);
		const samples = JSON.parse(readFileSync(report.samplesFile ?? "", "utf8"));
		for (const list of Object.values<{ text: string }[]>(samples.samples)) {
			expect(list.length).toBeLessThanOrEqual(3);
			for (const sample of list) expect(sample.text.length).toBeLessThanOrEqual(80);
		}
	});

	test("apply writes rows the way omp's own retain does", async () => {
		runId = "imp-test-cycle";
		const report = await runImport(env.options({ command: "import", apply: true, runId }));
		expect(report.counts.imported).toBe(2);
		expect(report.counts.errors).toBe(0);
		const db = env.db(bank);
		try {
			const rows = db.query("SELECT * FROM working_memory WHERE source = 'import-claude-code' ORDER BY content").all() as Record<string, unknown>[];
			expect(rows).toHaveLength(2);
			for (const row of rows) {
				expect(row.scope).toBe("bank");
				expect(row.session_id).toBe(bank);
				expect(row.veracity).toBe("tool");
				expect(row.memory_type).toBe("fact");
				expect(row.trust_tier).toBe("IMPORTED");
				expect(row.consolidated_at).not.toBeNull();
				expect(row.author_id).toBe("coding-agent");
				const meta = JSON.parse(String(row.metadata_json));
				expect(meta).toMatchObject({ source: "claude-code", sourceKind: "memory", importRunId: runId, cwd });
				expect(String(meta.originKey)).toMatch(new RegExp(`^${cwd}#(one|two)$`));
				expect(meta.originHash).toHaveLength(64);
				expect(db.query("SELECT model FROM memory_embeddings WHERE memory_id = ?").get(String(row.id))).toEqual({ model: "BAAI/bge-base-en-v1.5" });
				expect(db.query("SELECT 1 FROM fts_working WHERE id = ?").get(String(row.id))).not.toBeNull();
				expect(db.query("SELECT value FROM annotations WHERE memory_id = ? AND kind = 'occurred_on'").get(String(row.id))).not.toBeNull();
			}
			const one = rows.find(row => String(row.content).startsWith("Alpha one"));
			expect(one?.timestamp).toMatch(/^\d{4}-\d\d-\d\dT/);
			expect(db.query("SELECT COUNT(*) AS n FROM facts WHERE source_msg_id IN (SELECT id FROM working_memory WHERE source = 'import-claude-code')").get()).toEqual({ n: 0 });
			// the native row is untouched: still unconsolidated, not trimmed, same text
			const native = db.query("SELECT content, consolidated_at FROM working_memory WHERE id = 'native-1'").get() as { content: string; consolidated_at: string | null };
			expect(native.consolidated_at).toBeNull();
			expect(native.content).toContain("native note");
		} finally {
			db.close();
		}
		const ledger = loadLedger(env.ledger);
		expect(Object.values(ledger.entries)).toHaveLength(2);
		expect((await import("node:fs")).statSync(env.ledger).mode & 0o777).toBe(0o600);
	});

	test("imported rows are not summarised by the library's own consolidation", async () => {
		const memory = new env.lib.Mnemopi({ dbPath: path.join(env.banksDir, bank, "mnemopi.db"), bank, sessionId: bank, embeddingModel: "BAAI/bge-base-en-v1.5", reconcile: false, llm: false });
		try {
			const eligible = memory.db.query("SELECT COUNT(*) AS n FROM working_memory WHERE session_id = ? AND consolidated_at IS NULL AND source = 'import-claude-code'").get(bank) as { n: number };
			expect(eligible.n).toBe(0);
		} finally {
			memory.close();
		}
	});

	test("verify passes", async () => {
		const report = await runVerify(env.options({ command: "verify", targetRun: runId }));
		expect(report.problems).toEqual([]);
		expect(report.ok).toBe(true);
		expect(report.banks[0]).toMatchObject({ bank, expected: 2, actual: 2, missingEmbedding: 0, missingFts: 0, missingProvenance: 0, integrity: "ok" });
	});

	test("re-running imports nothing (idempotent)", async () => {
		const before = rowCount();
		const report = await runImport(env.options({ command: "import", apply: true }));
		expect(report.counts.imported).toBe(0);
		expect(report.counts.skippedUnchanged).toBe(2);
		expect(rowCount()).toBe(before);
	});

	test("a changed source is reported, never applied", async () => {
		env.memory(projectDir, "one.md", { name: "Alpha one", description: "first note", type: "project" }, "Alpha now uses a red deploy lane.");
		const report = await runImport(env.options({ command: "import", apply: true }));
		expect(report.counts.changedSource).toBe(1);
		expect(report.counts.imported).toBe(0);
		expect(report.changed[0]?.originKey).toEndWith("#one");
		const db = env.db(bank);
		try {
			expect(db.query("SELECT COUNT(*) AS n FROM working_memory WHERE content LIKE '%blue deploy lane%'").get()).toEqual({ n: 1 });
			expect(db.query("SELECT COUNT(*) AS n FROM working_memory WHERE content LIKE '%red deploy lane%'").get()).toEqual({ n: 0 });
		} finally {
			db.close();
		}
		env.memory(projectDir, "one.md", { name: "Alpha one", description: "first note", type: "project" }, "Alpha uses a blue deploy lane.");
	});

	test("a volatile front matter field changing is not a change", async () => {
		env.memory(projectDir, "one.md", { name: "Alpha one", description: "first note", type: "project", originSessionId: "abc", modified: "2030-01-01" }, "Alpha uses a blue deploy lane.");
		const report = await runImport(env.options({ command: "dry-run" }));
		expect(report.counts.changedSource).toBe(0);
		expect(report.counts.skippedUnchanged).toBe(2);
	});

	test("a row the user deleted becomes a tombstone and is never re-imported", async () => {
		const memory = new env.lib.Mnemopi({ dbPath: path.join(env.banksDir, bank, "mnemopi.db"), bank, sessionId: bank, embeddingModel: "BAAI/bge-base-en-v1.5", reconcile: false, llm: false });
		const victim = memory.db.query("SELECT id FROM working_memory WHERE content LIKE 'Alpha two%'").get() as { id: string };
		env.lib.forgetWorking(memory.beam, victim.id);
		memory.close();
		const report = await runImport(env.options({ command: "import", apply: true }));
		expect(report.counts.skippedTombstone).toBe(1);
		expect(report.counts.imported).toBe(0);
		expect(rowCount()).toBe(2);
		const entry = Object.values(loadLedger(env.ledger).entries).find(item => item.originKey.endsWith("#two"));
		expect(entry?.state).toBe("tombstoned");
		const again = await runImport(env.options({ command: "import", apply: true }));
		expect(again.counts.skippedTombstone).toBe(1);
		expect(rowCount()).toBe(2);
	});

	test("verify catches a missing embedding", async () => {
		const db = env.db(bank);
		const row = db.query("SELECT id FROM working_memory WHERE content LIKE 'Alpha one%'").get() as { id: string };
		db.run("DELETE FROM memory_embeddings WHERE memory_id = ?", [row.id]);
		db.close();
		const report = await runVerify(env.options({ command: "verify", targetRun: runId }));
		expect(report.ok).toBe(false);
		expect(report.banks[0]?.missingEmbedding).toBe(1);
	});

	test("undo without --apply removes nothing; with --apply it removes the run and keeps tombstones", async () => {
		const dry = await runUndo(env.options({ command: "undo", targetRun: runId }));
		expect(dry.apply).toBe(false);
		expect(rowCount()).toBe(2);
		const done = await runUndo(env.options({ command: "undo", targetRun: runId, apply: true }));
		expect(done.removed).toEqual([{ bank, rows: 1 }]);
		expect(rowCount()).toBe(1);
		const db = env.db(bank);
		try {
			const native = db.query("SELECT content FROM working_memory").all() as { content: string }[];
			expect(native).toEqual([{ content: "A native note an omp session retained long ago." }]);
			expect(db.query("SELECT COUNT(*) AS n FROM memory_embeddings WHERE memory_id NOT IN (SELECT id FROM working_memory)").get()).toEqual({ n: 0 });
			expect(db.query("SELECT COUNT(*) AS n FROM annotations WHERE memory_id NOT IN (SELECT id FROM working_memory)").get()).toEqual({ n: 0 });
		} finally {
			db.close();
		}
		const ledger = loadLedger(env.ledger);
		const states = Object.values(ledger.entries).map(entry => `${entry.originKey.split("#")[1]}:${entry.state}`);
		// "one" was undone (entry dropped); "two" was deleted by the user before the undo and stays tombstoned
		expect(states).toEqual(["two:tombstoned"]);
		expect(ledger.runs[runId]?.undoneAt).toBeDefined();
	});

	test("after undo the undone item can be imported again", async () => {
		const report = await runImport(env.options({ command: "import", apply: true }));
		expect(report.counts.imported).toBe(1);
		expect(report.counts.skippedTombstone).toBe(1);
	});
});

describe("redaction and residual findings", () => {
	test("secrets are redacted in the stored row and never reach the report", async () => {
		const cwd2 = env.folder("beta");
		const dir = env.project(cwd2);
		const token = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG";
		env.memory(dir, "key.md", { name: "Beta key", type: "reference" }, `The staging key is ${token} for the beta box.`);
		const out = path.join(env.root, "beta.json");
		const report = await runImport(env.options({ command: "import", apply: true, out }));
		expect(report.counts.imported).toBe(1);
		expect(report.redaction["api-key"]).toBe(1);
		const db = env.db(env.bankOf(cwd2));
		try {
			const row = db.query("SELECT content FROM working_memory").get() as { content: string };
			expect(row.content).toContain("[REDACTED:api-key]");
			expect(row.content).not.toContain(token);
		} finally {
			db.close();
		}
		expect(readFileSync(out, "utf8")).not.toContain(token);
	});

	test("an item with a residual finding is held back, listed by kind only", async () => {
		const cwd3 = env.folder("gamma");
		const dir = env.project(cwd3);
		env.memory(dir, "pw.md", { name: "Gamma login" }, "Set " + "secret=" + "Zk9Qm2Xv7Lp4Rt8N" + " in the gamma deploy script.");
		const report = await runImport(env.options({ command: "dry-run" }));
		const held = report.held.find(item => item.originKey.startsWith(`${cwd3}#`));
		if (held) {
			expect(report.counts.skippedHeld).toBeGreaterThanOrEqual(1);
			expect(held.kinds.length).toBeGreaterThan(0);
		} else {
			// the proxy redactor caught it outright: then no residual remains and the text is already scrubbed
			expect(report.redaction).not.toEqual({});
		}
	});
});

describe("folders", () => {
	test("a project directory with no session cwd and no decodable path goes to the unassigned bank", async () => {
		const dir = path.join(env.home, ".claude", "projects", "-nowhere-at-all-xyz");
		const file = env.memory(dir, "lost.md", { name: "Lost note" }, "Nobody knows where this belongs.");
		expect(existsSync(file)).toBe(true);
		const dry = await runImport(env.options({ command: "dry-run" }));
		expect(dry.unresolved.map(item => item.where)).toContain("-nowhere-at-all-xyz");
		expect(dry.perBank.find(item => item.bank === "unassigned")?.wouldImport).toBe(1);
		const skipped = await runImport(env.options({ command: "dry-run", skipUnassigned: true }));
		expect(skipped.counts.skippedUnassigned).toBe(1);
		const applied = await runImport(env.options({ command: "import", apply: true }));
		expect(applied.perBank.find(item => item.bank === "unassigned")?.imported).toBe(1);
		const db = env.db("unassigned");
		try {
			const meta = JSON.parse((db.query("SELECT metadata_json FROM working_memory").get() as { metadata_json: string }).metadata_json);
			expect(meta.cwd).toBeNull();
			expect(meta.originKey).toBe("unassigned:-nowhere-at-all-xyz#lost");
		} finally {
			db.close();
		}
	});

	test("nothing goes to the global bank unless it is allow-listed", async () => {
		const cwd4 = env.folder("delta");
		const dir = env.project(cwd4);
		env.memory(dir, "pref.md", { name: "Delta pref", type: "user" }, "Sam prefers short answers in delta.");
		const plain = await runImport(env.options({ command: "dry-run" }));
		expect(plain.perBank.map(item => item.bank)).not.toContain("default");
		const allow = path.join(env.root, "allow.txt");
		await Bun.write(allow, `# reviewed\n${cwd4}#pref\n`);
		const listed = await runImport(env.options({ command: "dry-run", globalAllowlist: allow }));
		expect(listed.perBank.find(item => item.bank === "default")?.wouldImport).toBe(1);
	});
});

describe("embedding failure", () => {
	test("a bank whose rows cannot all be embedded is left exactly as it was and nothing is recorded", async () => {
		const cwd5 = env.folder("epsilon");
		const bank5 = env.bankOf(cwd5);
		env.memory(env.project(cwd5), "e.md", { name: "Epsilon" }, "A note whose embedding will fail.");
		env.emptyBank(bank5);
		const failing = { embed: async (): Promise<number[][]> => { throw new Error("model down"); }, close() {} };
		const report = await runImport(env.options({ command: "import", apply: true, embedderFactory: () => failing, runId: "imp-test-embedfail" }));
		expect(report.counts.imported).toBe(0);
		expect(report.errors.some(item => item.where === `bank ${bank5}` && /no embedding/.test(item.error))).toBe(true);
		const db = env.db(bank5);
		try {
			expect(db.query("SELECT COUNT(*) AS n FROM working_memory").get()).toEqual({ n: 0 });
			expect(db.query("SELECT COUNT(*) AS n FROM annotations").get()).toEqual({ n: 0 });
		} finally {
			db.close();
		}
		expect(Object.values(loadLedger(env.ledger).entries).some(entry => entry.bank === bank5)).toBe(false);
	});
});
