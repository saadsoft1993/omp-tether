/**
 * The behavioural self-test: it passes on the published library, and it names the failing step when the library is changed so
 * that one capability the importer relies on is gone. Each mutation edits ONE behaviour in a copy of the package (the copy
 * shares the original's dependencies through symlinks), so these tests prove the test can fail, not only that it passes.
 */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { loadLibrary, type Library, packageDirOf, resolveLibrary } from "../lib";
import { Refusal } from "../model";
import { ensureSelfTest, libraryFingerprint, readSelfTest, requireSelfTest, runSelfTest, SELFTEST_VERSION, stubEmbedder, VERIFIED_OMP_RANGE } from "../selftest";
import { ensureTestLibrary, TEST_OMP_VERSION } from "./helpers";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

let scratch: string;
let realLib: Library;
let counter = 0;

beforeAll(async () => {
	scratch = realpathSync(mkdtempSync(path.join(tmpdir(), "memory-import-selftest-test-")));
	const libDir = await ensureTestLibrary();
	const location = resolveLibrary(TEST_OMP_VERSION, { libDir }).location;
	if (location === null) throw new Error("test library missing");
	realLib = await loadLibrary(location);
});
afterAll(() => rmSync(scratch, { recursive: true, force: true }));

type Edit = [file: string, find: RegExp, replacement: string];

/** A copy of the test library with `edits` applied to its sources; every other dependency is shared by symlink. */
async function mutated(edits: Edit[]): Promise<Library> {
	counter += 1;
	const root = path.join(scratch, `mutant-${counter}`);
	const scope = path.join(root, "node_modules", "@oh-my-pi");
	mkdirSync(scope, { recursive: true });
	const sourceScope = path.dirname(realLib.pkgDir);
	for (const name of readdirSync(sourceScope)) {
		const from = path.join(sourceScope, name);
		if (path.join(sourceScope, name) === realLib.pkgDir) cpSync(from, path.join(scope, name), { recursive: true });
		else symlinkSync(realpathSync(from), path.join(scope, name));
	}
	const pkgDir = packageDirOf(root);
	for (const [file, find, replacement] of edits) {
		const target = path.join(pkgDir, file);
		const before = readFileSync(target, "utf8");
		const after = before.replace(find, replacement);
		if (after === before) throw new Error(`mutation did not apply: ${file} ${find}`);
		writeFileSync(target, after);
	}
	return loadLibrary({ kind: "explicit", root, pkgDir, version: realLib.version });
}

const run = (lib: Library) => runSelfTest(lib, { embedder: stubEmbedder(), kind: "stub" });

describe("the published library", () => {
	test("passes every step of the write sequence, in a scratch bank that is removed again", async () => {
		const before = readdirSync(tmpdir()).filter(name => name.startsWith("memory-import-selftest-") && !name.startsWith("memory-import-selftest-test-"));
		const result = await run(realLib);
		expect(result.failedStep).toBeNull();
		expect(result.ok).toBe(true);
		expect(result.steps.map(step => step.step)).toEqual(["restore insert", "full-text entry", "annotation", "embedding", "consolidated_at", "trim exemption", "forget"]);
		expect(result.steps.every(step => step.ok)).toBe(true);
		const after = readdirSync(tmpdir()).filter(name => name.startsWith("memory-import-selftest-") && !name.startsWith("memory-import-selftest-test-"));
		expect(after.sort()).toEqual(before.sort());
	});
});

describe("a library that lost one capability fails, naming the step", () => {
	const cases: { name: string; step: string; edits: Edit[] }[] = [
		{
			name: "restored rows are not stamped as consolidated",
			step: "consolidated_at",
			edits: [["src/core/beam/store.ts", /item\.consolidated_at == null \? importedAt :/, "item.consolidated_at == null ? null :"]],
		},
		{
			name: "restored rows lose their trust tier",
			step: "restore insert",
			edits: [["src/core/beam/store.ts", /sqlBinding\(item\.trust_tier, "STATED"\)/, '"STATED"']],
		},
		{
			name: "the embedding table no longer takes the importer's embedding statement",
			step: "embedding",
			edits: [["src/core/beam/schema.ts", /embedding_json TEXT NOT NULL/, "vector_json TEXT NOT NULL"]],
		},
		{
			name: "annotations are not stored",
			step: "annotation",
			edits: [["src/core/annotations.ts", /add\(memoryId: string, kind: string, value: string,[^)]*\): number \{/, "$& return 0;"]],
		},
		{
			name: "the full-text index is not filled",
			step: "full-text entry",
			edits: [["src/core/beam/schema.ts", /INSERT INTO fts_working\(id, content\) VALUES \(new\.id, COALESCE\(new\.embed_text, new\.content\)\);/, "SELECT 1;"]],
		},
		{
			name: "the trim also removes imported rows",
			step: "trim exemption",
			edits: [
				["src/core/beam/store.ts", /AND consolidated_at IS NULL/g, ""],
				["src/core/beam/store.ts", /AND trust_tier IS NOT 'IMPORTED'/g, ""],
			],
		},
		{
			name: "deleting a row leaves its embedding behind",
			step: "forget",
			edits: [["src/core/beam/store.ts", /\n\s*db\.run\(`DELETE FROM memory_embeddings WHERE memory_id IN \(\$\{placeholders\}\)`, \[\.\.\.ids\]\);/, ""]],
		},
	];
	for (const { name, step, edits } of cases) {
		test(`${name}: fails at "${step}"`, async () => {
			const result = await run(await mutated(edits));
			expect(result.ok).toBe(false);
			expect(result.failedStep).toBe(step);
			expect(result.steps.at(-1)).toMatchObject({ step, ok: false });
		});
	}

	test("the refusal names the step, both versions and what the importer was verified against", async () => {
		const lib = await mutated(cases[2]?.edits ?? []);
		const refusal = await requireSelfTest(lib, "18.18.8", { OMP_MEMORY_CACHE_DIR: path.join(scratch, "cache-refusal") }).catch((error: unknown) => error);
		expect(refusal).toBeInstanceOf(Refusal);
		const message = (refusal as Refusal).message;
		expect(message).toContain('step "embedding"');
		expect(message).toContain("omp 18.18.8");
		expect(message).toContain(`@oh-my-pi/pi-mnemopi ${realLib.version}`);
		expect(message).toContain(`omp ${VERIFIED_OMP_RANGE.first} to ${VERIFIED_OMP_RANGE.last}`);
		expect(message).toContain("doctor --deep");
	});
});

describe("the cached result", () => {
	test("is per library and fingerprint: a pass is reused, a failure is shown, an edited package starts over", async () => {
		const env = { OMP_MEMORY_CACHE_DIR: path.join(scratch, "cache-record") };
		expect(readSelfTest(realLib.pkgDir, realLib.version, env).status).toBe("not-run");
		const first = await ensureSelfTest(realLib, { reuse: true, env });
		expect(first.cached).toBe(false);
		expect(readSelfTest(realLib.pkgDir, realLib.version, env)).toMatchObject({ status: "passed" });
		const again = await ensureSelfTest(realLib, { reuse: true, env });
		expect(again.cached).toBe(true);
		expect(again.result.at).toBe(first.result.at);

		const broken = await mutated([["src/core/beam/schema.ts", /embedding_json TEXT NOT NULL/, "vector_json TEXT NOT NULL"]]);
		expect(readSelfTest(broken.pkgDir, broken.version, env).status).toBe("not-run"); // another fingerprint: nothing is carried over
		expect(libraryFingerprint(broken.pkgDir)).not.toBe(libraryFingerprint(realLib.pkgDir));
		const failed = await ensureSelfTest(broken, { reuse: true, env });
		expect(failed.result.ok).toBe(false);
		const status = readSelfTest(broken.pkgDir, broken.version, env);
		expect(status).toMatchObject({ status: "failed", failedStep: "embedding" });
		expect(status.detail).toContain('step "embedding"');
		expect(readSelfTest(realLib.pkgDir, realLib.version, env).status).toBe("passed");
	});

	test("a record made for another sequence version or another Bun is not trusted", async () => {
		const env = { OMP_MEMORY_CACHE_DIR: path.join(scratch, "cache-stale") };
		await ensureSelfTest(realLib, { reuse: false, env });
		const dir = path.join(env.OMP_MEMORY_CACHE_DIR, "selftest");
		const file = path.join(dir, readdirSync(dir)[0] ?? "");
		const record = JSON.parse(readFileSync(file, "utf8")) as { runs: { stub: { selfTestVersion: number; bun: string } } };
		record.runs.stub.selfTestVersion = SELFTEST_VERSION + 1;
		writeFileSync(file, JSON.stringify(record));
		expect(readSelfTest(realLib.pkgDir, realLib.version, env).status).toBe("not-run");
		record.runs.stub.selfTestVersion = SELFTEST_VERSION;
		record.runs.stub.bun = "0.0.1";
		writeFileSync(file, JSON.stringify(record));
		expect(readSelfTest(realLib.pkgDir, realLib.version, env).status).toBe("not-run");
	});

	test("a cache directory that cannot be written does not stop the test or the write that follows it", async () => {
		const blocker = path.join(scratch, "not-a-directory");
		writeFileSync(blocker, "a file where the cache directory would be\n");
		const { result, cached } = await ensureSelfTest(realLib, { reuse: true, env: { OMP_MEMORY_CACHE_DIR: blocker } });
		expect(result.ok).toBe(true);
		expect(cached).toBe(false);
		expect((await requireSelfTest(realLib, "18.18.3", { OMP_MEMORY_CACHE_DIR: blocker })).ok).toBe(true);
	});

	test("the cache file is private to the user", async () => {
		const env = { OMP_MEMORY_CACHE_DIR: path.join(scratch, "cache-mode") };
		await ensureSelfTest(realLib, { reuse: false, env });
		const dir = path.join(env.OMP_MEMORY_CACHE_DIR, "selftest");
		expect(existsSync(dir)).toBe(true);
		const file = path.join(dir, readdirSync(dir)[0] ?? "");
		expect(statSync(file).mode & 0o077).toBe(0);
	});
});
