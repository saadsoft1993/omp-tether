/**
 * The command-line entry (cli.ts) installs its last-resort handlers before it loads anything else. A child process runs a copy of
 * the entry next to a stand-in `main.ts` (and the sink and its two dependencies) in a directory whose name carries a secret, and
 * must print no secret and exit with status 3 when: the application module throws while it loads or is missing, a timer throws,
 * a promise is rejected and nobody awaits it, the sink itself cannot be loaded (the message is withheld), or a signal handler's
 * cleanup throws.
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { chmodSync, copyFileSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { Database } from "bun:sqlite";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

const SOURCE = path.resolve(import.meta.dir, "..");
// built at run time so that no file of this repository holds a literal token
const TOKEN = `ghp_${"Zk9Qm2Xv7Lp4Rt8NwB3cD5eF6gH1jK0aS7uY"}`;
const PIN = "password=902817";
const SECRETS = [TOKEN, "902817", PIN];

let root: string;
let dir: string;

/** A fresh copy of the entry and what it needs, in a directory named like a secret, with `main` as the application module. */
function entry(name: string, main: string | null, sink: string | null = null): string {
	const where = path.join(dir, `${PIN}-${name}`);
	mkdirSync(where, { recursive: true });
	for (const file of ["cli.ts", "hold.ts", "omp_redact.ts", "model.ts"]) copyFileSync(path.join(SOURCE, file), path.join(where, file));
	if (sink === null) copyFileSync(path.join(SOURCE, "sink.ts"), path.join(where, "sink.ts"));
	else writeFileSync(path.join(where, "sink.ts"), sink);
	if (main !== null) writeFileSync(path.join(where, "main.ts"), main);
	return path.join(where, "cli.ts");
}

function run(cli: string): { code: number | null; text: string } {
	const child = Bun.spawnSync([process.execPath, cli], { env: { ...process.env, HOME: root }, timeout: 60_000 });
	return { code: child.exitCode, text: `${child.stdout.toString()}\n${child.stderr.toString()}` };
}

const leaks = (text: string): string[] => SECRETS.filter(secret => text.includes(secret) || text.includes(secret.toLowerCase()));

beforeAll(() => {
	root = realpathSync(mkdtempSync(path.join(tmpdir(), "entry-test-")));
	dir = path.join(root, "entries");
	mkdirSync(dir);
});
afterAll(() => rmSync(root, { recursive: true, force: true }));

describe("the entry shows no secret and exits with status 3 when it cannot run the application", () => {
	test("a module that throws while it loads", () => {
		const ran = run(entry("throws-at-import", `throw new Error("cannot start in /srv/${PIN}/work with ${TOKEN}");\nexport const runCommandLine = async (): Promise<number> => 0;\n`));
		expect(ran.code).toBe(3);
		expect(ran.text).toContain("unexpected failure (the importer could not be loaded or stopped)");
		expect(leaks(ran.text)).toEqual([]);
	});

	test("an application module that is not there (the message names a path that carries the secret)", () => {
		const ran = run(entry("missing-main", null));
		expect(ran.code).toBe(3);
		expect(ran.text).toContain("unexpected failure");
		expect(leaks(ran.text)).toEqual([]);
	});

	test("a module with a syntax error", () => {
		const ran = run(entry("syntax-error", `export const ${PIN} = ;\n`));
		expect(ran.code).toBe(3);
		expect(leaks(ran.text)).toEqual([]);
	});

	test("an exception that nobody catches", () => {
		const ran = run(entry("uncaught", `export const runCommandLine = (): Promise<number> => {\n\tsetTimeout(() => {\n\t\tthrow new Error("failed on ${TOKEN} and ${PIN}");\n\t}, 10);\n\treturn new Promise(() => {});\n};\n`));
		expect(ran.code).toBe(3);
		expect(ran.text).toContain("unexpected failure (an uncaught exception)");
		expect(leaks(ran.text)).toEqual([]);
	});

	test("a promise that is rejected and never awaited", () => {
		const ran = run(entry("unhandled", `export const runCommandLine = (): Promise<number> => {\n\tPromise.reject(new Error("rejected: ${PIN} ${TOKEN}"));\n\treturn new Promise(() => {});\n};\n`));
		expect(ran.code).toBe(3);
		expect(ran.text).toContain("unexpected failure (an unhandled rejection)");
		expect(leaks(ran.text)).toEqual([]);
	});

	test("a sink that cannot be loaded: the message is withheld, not printed raw", () => {
		const ran = run(entry("broken-sink", `throw new Error("cannot start: ${PIN} ${TOKEN}");\nexport const runCommandLine = async (): Promise<number> => 0;\n`, "export const broken = ;\n"));
		expect(ran.code).toBe(3);
		expect(ran.text).toContain("(message withheld)");
		expect(leaks(ran.text)).toEqual([]);
	});

	test("a normal run through the entry still gives the command's own exit status", () => {
		const ran = run(entry("fine", "export const runCommandLine = async (): Promise<number> => 7;\n"));
		expect(ran.code).toBe(7);
	});
});

describe("a cleanup that throws inside a signal handler is reported through the sink", () => {
	test("the claude-mem snapshot cleanup on SIGTERM: no secret in the output, status 3", () => {
		const scratch = path.join(dir, `${PIN}-signal`);
		const temp = path.join(scratch, "tmp");
		mkdirSync(temp, { recursive: true });
		const db = path.join(scratch, "claude-mem.db");
		const database = new Database(db);
		database.run("CREATE TABLE t (x TEXT)");
		database.close();
		const script = path.join(scratch, "run.ts");
		writeFileSync(
			script,
			`import { chmodSync } from "node:fs";\nimport { snapshotDatabase } from ${JSON.stringify(path.join(SOURCE, "sources", "claude_mem"))};\nsnapshotDatabase(${JSON.stringify(db)});\nchmodSync(${JSON.stringify(temp)}, 0o500); // the snapshot directory can no longer be removed\nprocess.kill(process.pid, "SIGTERM");\nawait new Promise(resolve => setTimeout(resolve, 5000));\n`,
		);
		try {
			const child = Bun.spawnSync([process.execPath, script], { env: { ...process.env, HOME: root, TMPDIR: temp }, timeout: 60_000 });
			const text = `${child.stdout.toString()}\n${child.stderr.toString()}`;
			expect(child.exitCode).toBe(3);
			expect(text).toContain("unexpected failure (snapshot cleanup on a signal)");
			expect(leaks(text)).toEqual([]);
		} finally {
			chmodSync(temp, 0o700);
		}
		expect(readFileSync(script, "utf8").length).toBeGreaterThan(0);
	});
});
