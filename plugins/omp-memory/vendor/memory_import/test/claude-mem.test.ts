/** claude-mem session summaries: folder from the session file, snapshot copy, empty rows reported. */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { Database } from "bun:sqlite";
import { mkdirSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { runImport } from "../engine";
import { type Env, makeEnv } from "./helpers";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

let env: Env;
let cwd: string;

const SESSION = "11111111-1111-4111-8111-111111111111";

beforeAll(async () => {
	env = await makeEnv();
	cwd = env.folder("zeta");
	env.project(cwd);
	env.emptyBank(env.bankOf(cwd));
	mkdirSync(path.dirname(env.options().claudeMemDb), { recursive: true });
	const db = new Database(env.options().claudeMemDb);
	db.exec(`CREATE TABLE sdk_sessions (id INTEGER PRIMARY KEY, content_session_id TEXT UNIQUE, memory_session_id TEXT UNIQUE, project TEXT);
		CREATE TABLE session_summaries (id INTEGER PRIMARY KEY AUTOINCREMENT, memory_session_id TEXT, project TEXT, request TEXT, investigated TEXT, learned TEXT, completed TEXT, next_steps TEXT, files_read TEXT, files_edited TEXT, notes TEXT, prompt_number INTEGER, discovery_tokens INTEGER, created_at TEXT, created_at_epoch INTEGER);
		CREATE TABLE observations (id INTEGER PRIMARY KEY, text TEXT);
		CREATE TABLE user_prompts (id INTEGER PRIMARY KEY, text TEXT);`);
	db.run("INSERT INTO sdk_sessions VALUES (1, ?, 'mem-1', 'zeta')", [SESSION]);
	db.run("INSERT INTO sdk_sessions VALUES (2, 'no-such-session-file', 'mem-2', 'wanderer')");
	const insert = db.prepare("INSERT INTO session_summaries (memory_session_id, project, request, learned, notes, created_at, created_at_epoch) VALUES (?, ?, ?, ?, ?, ?, 0)");
	insert.run("mem-1", "zeta", "Fix the zeta cache", "The cache key was wrong", null, "2026-03-01T10:00:00.000Z");
	insert.run("mem-2", "wanderer", "Explore wanderer", "Nothing certain", null, "2026-03-02T10:00:00.000Z");
	insert.run("mem-1", "zeta", null, null, null, "2026-03-03T10:00:00.000Z");
	db.run("INSERT INTO observations VALUES (1, 'observation text that must never be imported')");
	db.run("INSERT INTO user_prompts VALUES (1, 'prompt text that must never be imported')");
	db.close();
});
afterAll(() => env.cleanup());

const snapshots = (): string[] => readdirSync(tmpdir()).filter(name => name.startsWith("memory-import-snapshot-"));

describe("claude-mem summaries", () => {
	test("dry-run maps by session file, lists the unmapped project, reports the empty summary", async () => {
		const before = snapshots().length;
		const report = await runImport(env.options({ mode: "claude-mem-summaries", command: "dry-run" }));
		expect(report.counts.found).toBe(2);
		expect(report.counts.errors).toBe(1);
		expect(report.errors[0]?.error).toBe("empty summary");
		expect(report.perBank.find(item => item.bank === env.bankOf(cwd))?.wouldImport).toBe(1);
		expect(report.perBank.find(item => item.bank === "unassigned")?.wouldImport).toBe(1);
		expect(report.unresolved[0]?.where).toBe('claude-mem project "wanderer"');
		expect(snapshots().length).toBe(before); // the private snapshot copy is removed
	});

	test("apply imports summaries as episodes; observations and prompts are never read", async () => {
		const report = await runImport(env.options({ mode: "claude-mem-summaries", command: "import", apply: true }));
		expect(report.counts.imported).toBe(2);
		const db = env.db(env.bankOf(cwd));
		try {
			const row = db.query("SELECT content, memory_type, metadata_json, timestamp FROM working_memory").get() as { content: string; memory_type: string; metadata_json: string; timestamp: string };
			expect(row.memory_type).toBe("episode");
			expect(row.content).toContain("Request: Fix the zeta cache");
			expect(row.content).not.toContain("observation text");
			expect(row.timestamp).toBe("2026-03-01T10:00:00.000Z");
			expect(JSON.parse(row.metadata_json)).toMatchObject({ source: "claude-mem", sourceKind: "summary", originKey: "summaries#1" });
		} finally {
			db.close();
		}
		const unassigned = env.db("unassigned");
		try {
			expect(unassigned.query("SELECT COUNT(*) AS n FROM working_memory WHERE content LIKE '%prompt text%' OR content LIKE '%observation text%'").get()).toEqual({ n: 0 });
		} finally {
			unassigned.close();
		}
		const again = await runImport(env.options({ mode: "claude-mem-summaries", command: "import", apply: true }));
		expect(again.counts.imported).toBe(0);
		expect(again.counts.skippedUnchanged).toBe(2);
	});
});
