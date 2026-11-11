/**
 * Exit statuses of the command line: 0 success, 1 a refusal or a failed check, 2 wrong usage or an unusable environment,
 * 3 an unexpected failure. The README states the same convention for the whole of omp-memory.
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import path from "node:path";
import { holdOpen, type Env, makeEnv } from "./helpers";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

const CLI = path.join(import.meta.dir, "..", "cli.ts");

function cli(args: string[], extraEnv: Record<string, string> = {}): { code: number; err: string; out: string } {
	const ran = Bun.spawnSync([process.execPath, CLI, ...args], { stdout: "pipe", stderr: "pipe", env: { ...process.env, ...extraEnv } });
	return { code: ran.exitCode, err: ran.stderr.toString(), out: ran.stdout.toString() };
}

let env: Env;
let holder: { pid: number; close(): void } | null = null;
let bank = "";

beforeAll(async () => {
	env = await makeEnv();
	const cwd = env.folder("codes");
	bank = env.bankOf(cwd);
	env.memory(env.project(cwd), "n.md", { name: "N" }, "A note for the exit code tests.");
	env.emptyBank(bank);
	holder = await holdOpen(path.join(env.banksDir, bank, "mnemopi.db"));
});
afterAll(() => {
	holder?.close();
	env.cleanup();
});

const common = (): string[] => ["--banks-dir", env.banksDir, "--ledger", env.ledger, "--lib-dir", env.libDir, "--omp-bin", env.fakeOmp, "--claude-home", env.home];

describe("exit statuses", () => {
	test("wrong usage is 2: unknown flag, unknown command, missing value, import without --apply, undo without --run", () => {
		expect(cli(["dry-run", "--no-such-flag"]).code).toBe(2);
		expect(cli(["frobnicate"]).code).toBe(2);
		expect(cli(["dry-run", "--banks-dir"]).code).toBe(2);
		expect(cli(["import", ...common()]).code).toBe(2);
		expect(cli(["undo", ...common()]).code).toBe(2);
		expect(cli(["dry-run", "--source", "nonsense"]).code).toBe(2);
	});

	test("a refusal is 1: a bank another process has open, a missing library, a missing source file, an offline registry", () => {
		const open = cli(["import", "--apply", "--no-plan-check", "--no-backup-check", ...common()]);
		expect(open.err).toContain("open in process");
		expect(open.code).toBe(1);
		const noLibrary = cli(["dry-run", "--banks-dir", env.banksDir, "--lib-dir", path.join(env.root, "no-library"), "--omp-bin", env.fakeOmp]);
		expect(noLibrary.err).toContain("refused:");
		expect(noLibrary.code).toBe(1);
		const offline = cli(["setup", "--omp-version", "9.9.9", "--lib-dir", path.join(env.root, "setup-offline"), "--registry", "http://127.0.0.1:1", "--no-embeddings"]);
		expect(offline.err).toContain("cannot reach the npm registry");
		expect(offline.code).toBe(1);
	});

	test("an environment problem is 2: no omp version to work with", () => {
		const noOmp = cli(["setup", "--omp-bin", path.join(env.root, "no-such-omp"), "--registry", "http://127.0.0.1:1"]);
		expect(noOmp.err).toContain("could not read an omp version");
		expect(noOmp.code).toBe(2);
	});

	test("success is 0 and a failed check is 1", () => {
		expect(cli(["dry-run", ...common()]).code).toBe(0);
		expect(cli(["verify", ...common()]).code).toBe(1); // no run in the ledger yet
	});
});
