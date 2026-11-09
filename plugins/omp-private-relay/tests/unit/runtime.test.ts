// The runtime helpers and the launcher script: which Bun, which omp, and where BUN_BE_BUN is (and is not) set.
import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { checkBun, envForBun, envForOmp, findOmp, MIN_BUN, minorOf, ompVersionOf, probeOmp } from "../../src/runtime";
import { writeFakeOmp } from "./support/fake-omp";
import { PLUGIN_ROOT } from "./support/harness";

const roots: string[] = [];
const tmp = (): string => {
	const r = fs.mkdtempSync(path.join(os.tmpdir(), "oprr-"));
	roots.push(r);
	return r;
};
afterEach(() => {
	for (const r of roots.splice(0)) fs.rmSync(r, { recursive: true, force: true });
});

describe("Bun and omp versions", () => {
	test("Bun older than the minimum is refused with a message, newer or equal passes", () => {
		expect(checkBun("1.3.5")).toContain(MIN_BUN);
		expect(checkBun("1.4.3")).not.toBeNull();
		expect(checkBun("1.4.4")).toBeNull();
		expect(checkBun("1.10.0")).toBeNull();
		expect(checkBun(Bun.version)).toBeNull(); // the suite itself must run on a supported Bun
	});

	test("omp --version output is read as omp/<x.y.z> and nothing else", () => {
		expect(ompVersionOf("omp/18.18.3\n")).toBe("18.18.3");
		expect(ompVersionOf("1.4.4")).toBeNull(); // what a plain Bun prints: the BUN_BE_BUN trap
		expect(ompVersionOf("omp/18.18")).toBeNull();
		expect(ompVersionOf("")).toBeNull();
		expect(minorOf("18.18.3")).toBe("18.18");
	});

	test("BUN_BE_BUN is dropped for omp itself and set only for the child that must be Bun", () => {
		const base = { A: "1", BUN_BE_BUN: "1", B: undefined };
		expect(envForOmp(base)).toEqual({ A: "1" });
		expect(envForBun({ A: "1" })).toEqual({ A: "1", BUN_BE_BUN: "1" });
		expect(envForBun(base)).toEqual({ A: "1", BUN_BE_BUN: "1" });
	});

	test("probeOmp asks with the variable cleared, even when the parent has it set", () => {
		const root = tmp();
		const fake = writeFakeOmp(root, { version: "18.18.7" });
		process.env.BUN_BE_BUN = "1";
		try {
			expect(probeOmp(fake.bin)).toEqual({ bin: fake.bin, version: "18.18.7" });
		} finally {
			delete process.env.BUN_BE_BUN;
		}
		expect(probeOmp(path.join(root, "missing")).version).toBeNull();
	});

	test("findOmp: the configured path, then OMP_BIN, then PATH; relative values are ignored", () => {
		const root = tmp();
		const fake = writeFakeOmp(root);
		expect(findOmp("/a/b/omp", { OMP_BIN: "/c/omp" })).toBe("/a/b/omp");
		expect(findOmp(null, { OMP_BIN: "/c/omp" })).toBe("/c/omp");
		expect(findOmp(null, { OMP_BIN: "relative/omp", PATH: path.dirname(fake.bin) })).toBe(fake.bin);
		expect(findOmp("relative", { PATH: "/nonexistent" })).toBeNull();
	});
});

describe("bin/omp-private-relay", () => {
	const launcher = path.join(PLUGIN_ROOT, "bin", "omp-private-relay");
	const run = (env: Record<string, string>, args: string[] = []) => Bun.spawnSync([launcher, ...args], { env: { HOME: tmp(), ...env }, stdout: "pipe", stderr: "pipe" });

	test("is executable and runs src/cli.ts through the found omp with BUN_BE_BUN set for that one command", () => {
		const root = tmp();
		const fake = writeFakeOmp(root);
		const r = run({ OMP_BIN: fake.bin, PATH: "/usr/bin:/bin" }, ["status", "--json"]);
		expect(r.exitCode).toBe(0);
		expect(fake.calls()).toEqual([`BUN_BE_BUN=1 ${path.join(PLUGIN_ROOT, "src", "cli.ts")} status --json`]);
	});

	test("arguments with spaces and quotes reach the script unchanged; the fake path has a space too", () => {
		const root = tmp();
		const fake = writeFakeOmp(root);
		run({ OMP_BIN: fake.bin }, ["revoke", "a b", "it's"]);
		expect(fake.calls()[0]).toContain("revoke a b it's");
	});

	test("found through PATH when OMP_BIN is unset, and through the well-known places under HOME", () => {
		const root = tmp();
		const fake = writeFakeOmp(root);
		run({ PATH: `${path.dirname(fake.bin)}:/usr/bin:/bin` }, ["x"]);
		expect(fake.calls()).toHaveLength(1);
		const home = tmp();
		fs.mkdirSync(path.join(home, ".local", "bin"), { recursive: true });
		fs.copyFileSync(fake.bin, path.join(home, ".local", "bin", "omp"));
		fs.chmodSync(path.join(home, ".local", "bin", "omp"), 0o755);
		const r = Bun.spawnSync([launcher, "y"], { env: { HOME: home, PATH: "/usr/bin:/bin" }, stdout: "pipe", stderr: "pipe" });
		expect(r.exitCode).toBe(0);
	});

	test("no omp: a clear message and a non-zero exit", () => {
		const r = run({ PATH: "/nonexistent", HOME: tmp() }, ["status"]);
		expect(r.exitCode).toBe(1);
		expect(r.stderr.toString()).toContain("omp was not found");
	});

	test("run through a symlink (omp links installed plugins), it still finds src/cli.ts", () => {
		const root = tmp();
		const fake = writeFakeOmp(root);
		const link = path.join(tmp(), "link-to-launcher");
		fs.symlinkSync(launcher, link);
		const r = Bun.spawnSync([link, "z"], { env: { HOME: tmp(), OMP_BIN: fake.bin }, stdout: "pipe", stderr: "pipe" });
		expect(r.exitCode).toBe(0);
		expect(fake.calls()[0]).toContain(path.join(PLUGIN_ROOT, "src", "cli.ts"));
	});

	test("the real embedded Bun of the installed omp runs it, when there is one (BUN_BE_BUN scoped to the command)", () => {
		const omp = Bun.which("omp");
		if (!omp) return;
		const r = Bun.spawnSync([launcher, "help"], { env: { HOME: tmp(), OMP_BIN: omp, PATH: "/usr/bin:/bin" }, stdout: "pipe", stderr: "pipe" });
		expect(r.exitCode).toBe(0);
		expect(r.stdout.toString()).toContain("a private Collab relay");
		expect(process.env.BUN_BE_BUN).toBeUndefined();
	});
});
