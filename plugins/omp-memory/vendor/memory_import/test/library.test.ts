/** Library resolution (omp's plugin directory, the cache `setup` fills, an explicit root) and the `setup` command's refusals. */
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { cacheLibraryRoot, memoryCacheDir, ompPluginsDir, packageDirOf, PARITY_REQUIREMENTS, resolveLibrary, SETUP_MARKER, sqlRegex, unmetRequirements } from "../lib";
import { Refusal } from "../model";
import { fastembedRuntimeDir, runSetup } from "../setup";
import { ensureTestLibrary, TEST_OMP_VERSION } from "./helpers";
import { useLongTimeouts } from "./timeout";

useLongTimeouts();

let root: string;
beforeAll(() => {
	root = realpathSync(mkdtempSync(path.join(tmpdir(), "memory-import-lib-test-")));
});
afterAll(() => rmSync(root, { recursive: true, force: true }));

/** An install root that looks like a bun install of the package: its package.json, an entry file, and pinned dependencies. */
function fakeInstall(dir: string, version: string, deps: Record<string, string> = { "@oh-my-pi/pi-utils": version }, installed: Record<string, string> = deps): string {
	const pkgDir = packageDirOf(dir);
	mkdirSync(path.join(pkgDir, "src"), { recursive: true });
	writeFileSync(path.join(pkgDir, "package.json"), JSON.stringify({ name: "@oh-my-pi/pi-mnemopi", version, dependencies: deps }));
	writeFileSync(path.join(pkgDir, "src", "index.ts"), "export {};\n");
	for (const [dep, depVersion] of Object.entries(installed)) {
		mkdirSync(path.join(dir, "node_modules", dep), { recursive: true });
		writeFileSync(path.join(dir, "node_modules", dep, "package.json"), JSON.stringify({ name: dep, version: depVersion }));
	}
	return dir;
}

const complete = (dir: string): void => writeFileSync(path.join(dir, SETUP_MARKER), "{}\n");

function scratch(name: string): { plugins: string; cache: string; env: NodeJS.ProcessEnv } {
	const base = path.join(root, name);
	return { plugins: path.join(base, "plugins"), cache: path.join(base, "cache"), env: { OMP_PLUGINS_DIR: path.join(base, "plugins"), OMP_MEMORY_CACHE_DIR: path.join(base, "cache") } };
}

describe("where the library is looked for", () => {
	test("the directories come from the environment, with documented defaults", () => {
		expect(ompPluginsDir({ OMP_PLUGINS_DIR: "/x/plugins" })).toBe("/x/plugins");
		expect(ompPluginsDir({})).toMatch(/\.omp[\\/]plugins$/);
		expect(memoryCacheDir({ OMP_MEMORY_CACHE_DIR: "/x/c" })).toBe("/x/c");
		expect(memoryCacheDir({ XDG_CACHE_HOME: "/x/xdg" })).toBe("/x/xdg/omp-memory");
		expect(memoryCacheDir({})).toMatch(/\.cache[\\/]omp-memory$/);
		expect(cacheLibraryRoot("1.2.3", { OMP_MEMORY_CACHE_DIR: "/x/c" })).toBe("/x/c/lib/1.2.3");
	});

	test("omp's plugin directory is used when its package is exactly omp's version", () => {
		const s = scratch("plugins-exact");
		fakeInstall(s.plugins, "1.2.3");
		const found = resolveLibrary("1.2.3", { env: s.env });
		expect(found.location).toMatchObject({ kind: "omp-plugins", root: s.plugins, version: "1.2.3" });
	});

	test("a different version in the plugin directory is not used; a completed cache install of the right one is", () => {
		const s = scratch("plugins-old");
		fakeInstall(s.plugins, "1.2.2");
		const cache = cacheLibraryRoot("1.2.3", s.env);
		fakeInstall(cache, "1.2.3");
		complete(cache);
		const found = resolveLibrary("1.2.3", { env: s.env });
		expect(found.location?.kind).toBe("cache");
		expect(found.candidates[0]).toMatchObject({ kind: "omp-plugins", usable: false, version: "1.2.2" });
		expect(found.candidates[0]?.why).toBe("it is 1.2.2, omp is 1.2.3");
	});

	test("a cache install without its completion marker is an interrupted install and is not used", () => {
		const s = scratch("cache-partial");
		fakeInstall(cacheLibraryRoot("1.2.3", s.env), "1.2.3");
		const found = resolveLibrary("1.2.3", { env: s.env });
		expect(found.location).toBeNull();
		expect(found.candidates[1]?.why).toContain("incomplete install");
	});

	test("a dependency that is not at the version the package pins disqualifies the install, as does a missing one", () => {
		const s = scratch("deps");
		fakeInstall(s.plugins, "1.2.3", { "@oh-my-pi/pi-utils": "1.2.3", "@oh-my-pi/pi-ai": "1.2.3" }, { "@oh-my-pi/pi-utils": "1.2.2" });
		const found = resolveLibrary("1.2.3", { env: s.env });
		expect(found.location).toBeNull();
		expect(found.candidates[0]?.why).toContain("dependency @oh-my-pi/pi-utils is 1.2.2, the package pins 1.2.3");
		expect(found.candidates[0]?.why).toContain("dependency @oh-my-pi/pi-ai is not installed");
	});

	test("an explicit install root is the only place looked at: a configured library is used or refused, never replaced", () => {
		const s = scratch("explicit");
		fakeInstall(s.plugins, "1.2.3");
		const mine = fakeInstall(path.join(root, "explicit", "mine"), "1.2.4");
		const found = resolveLibrary("1.2.3", { libDir: mine, env: s.env });
		expect(found.location).toBeNull();
		expect(found.candidates.map(candidate => candidate.kind)).toEqual(["explicit"]);
		expect(resolveLibrary("1.2.4", { libDir: mine, env: s.env }).location?.kind).toBe("explicit");
	});

	test("nothing installed: every place looked at is listed", () => {
		const s = scratch("nothing");
		const found = resolveLibrary("1.2.3", { env: s.env });
		expect(found.location).toBeNull();
		expect(found.candidates.map(candidate => candidate.pkgDir)).toEqual([packageDirOf(s.plugins), packageDirOf(cacheLibraryRoot("1.2.3", s.env))]);
	});

	test("the published package installed by setup resolves for its own version only", async () => {
		const libDir = await ensureTestLibrary();
		expect(resolveLibrary(TEST_OMP_VERSION, { libDir }).location?.version).toBe(TEST_OMP_VERSION);
		expect(resolveLibrary("18.18.99", { libDir }).location).toBeNull();
	});
});

describe("setup", () => {
	const base = (over: Partial<Parameters<typeof runSetup>[0]> = {}): Parameters<typeof runSetup>[0] => ({
		ompBin: "omp",
		ompVersion: "9.9.9",
		libDir: path.join(root, "setup-target"),
		dryRun: false,
		embeddings: false,
		say: () => {},
		...over,
	});

	test("a dry run says what it would fetch, fetches nothing and creates nothing", async () => {
		const lines: string[] = [];
		const report = await runSetup(base({ dryRun: true, registry: "http://127.0.0.1:1", say: line => lines.push(line) }));
		expect(report.action).toBe("dry-run");
		expect(lines.join("\n")).toContain("will fetch from http://127.0.0.1:1: @oh-my-pi/pi-mnemopi@9.9.9");
		expect(lines.join("\n")).toContain(path.join(root, "setup-target"));
		expect(existsSync(path.join(root, "setup-target"))).toBe(false);
	});

	test("a version that does not exist on the registry is refused and nothing is installed", async () => {
		const requested: string[] = [];
		const server = Bun.serve({
			port: 0,
			hostname: "127.0.0.1",
			fetch(request) {
				requested.push(new URL(request.url).pathname);
				return new Response("{}", { status: 404 });
			},
		});
		try {
			await expect(runSetup(base({ registry: `http://127.0.0.1:${server.port}` }))).rejects.toThrow(/@oh-my-pi\/pi-mnemopi@9\.9\.9 does not exist .* nothing was installed/);
		} finally {
			server.stop(true);
		}
		expect(requested).toEqual(["/@oh-my-pi%2fpi-mnemopi/9.9.9"]);
		expect(existsSync(path.join(root, "setup-target"))).toBe(false);
	});

	test("a registry that answers with an error, or cannot be reached, installs nothing", async () => {
		const server = Bun.serve({ port: 0, hostname: "127.0.0.1", fetch: () => new Response("no", { status: 503 }) });
		try {
			await expect(runSetup(base({ registry: `http://127.0.0.1:${server.port}` }))).rejects.toThrow(/answered 503/);
		} finally {
			server.stop(true);
		}
		await expect(runSetup(base({ registry: "http://127.0.0.1:1" }))).rejects.toThrow(/cannot reach the npm registry/);
		expect(existsSync(path.join(root, "setup-target"))).toBe(false);
	});

	test("a plain-http registry is refused unless it is on this machine", async () => {
		await expect(runSetup(base({ registry: "http://registry.example.test" }))).rejects.toThrow(Refusal);
	});

	test("without an omp version it refuses and says how to give one", async () => {
		await expect(runSetup(base({ ompVersion: undefined, ompBin: path.join(root, "no-such-omp") }))).rejects.toThrow(/--omp-version/);
	});

	test("when a matching library is already there nothing is fetched", async () => {
		const libDir = await ensureTestLibrary();
		const lines: string[] = [];
		const report = await runSetup(base({ ompVersion: TEST_OMP_VERSION, libDir, registry: "http://127.0.0.1:1", say: line => lines.push(line) }));
		expect(report.action).toBe("already-available");
		expect(report.library).toMatchObject({ kind: "explicit", version: TEST_OMP_VERSION });
		expect(lines.join("\n")).toContain("nothing to fetch");
		expect(readdirSync(libDir)).toContain("node_modules");
	});
});

describe("structural SQL matching", () => {
	const embedding = PARITY_REQUIREMENTS.find(requirement => requirement.name === "embedding store");
	const restore = PARITY_REQUIREMENTS.find(requirement => requirement.name === "restore insert (library import path)");

	test("letter case, line breaks, indentation and VALUES against SELECT do not matter", () => {
		const values = "insert or replace into memory_embeddings(memory_id, embedding_json, model) values (?, ?, ?)";
		const select = "\n\tINSERT OR REPLACE INTO memory_embeddings(memory_id, embedding_json, model)\n\t\tSELECT ?, ?, ?\n\t\tWHERE EXISTS (SELECT 1)";
		const spaced = "Insert  Or  Replace Into memory_embeddings ( memory_id ,\n embedding_json ,\n model ) VALUES(?,?,?)";
		for (const text of [values, select, spaced]) expect(unmetRequirements(text, embedding ? [embedding] : [])).toEqual([]);
		expect(sqlRegex("a b(c, d)").test("A\n\tB ( C ,D )")).toBe(true);
	});

	test("a statement that is really different is still unmet", () => {
		const other = "INSERT OR REPLACE INTO memory_embeddings(memory_id, vector, model) VALUES (?, ?, ?)";
		expect(unmetRequirements(other, embedding ? [embedding] : [])).toEqual(["embedding store"]);
		expect(unmetRequirements("DELETE FROM working_memory WHERE id = ?", PARITY_REQUIREMENTS.filter(requirement => requirement.name === "forget"))).toEqual(["forget"]);
	});

	test("a column list only has to contain the columns the importer writes: extra columns pass, a missing one does not", () => {
		const columns = restore?.columnsOf?.columns ?? [];
		const insert = (list: readonly string[]): string => `INSERT INTO working_memory (\n  ${list.join(",\n  ")}\n) VALUES (${list.map(() => "?").join(", ")})`;
		const only = restore ? [restore] : [];
		expect(unmetRequirements(insert(columns), only)).toEqual([]);
		expect(unmetRequirements(insert([...columns, "a_new_column"]), only)).toEqual([]);
		expect(unmetRequirements(insert([...columns].reverse()), only)).toEqual([]);
		expect(unmetRequirements(insert(columns.filter(column => column !== "trust_tier")), only)).toEqual(["restore insert (library import path)"]);
	});

	test("every requirement's own sample satisfies it", () => {
		for (const requirement of PARITY_REQUIREMENTS) expect(unmetRequirements(requirement.sample, [requirement])).toEqual([]);
	});

	test("the installed package meets all of them", () => {
		const pkgDir = packageDirOf(process.env.OMP_MEMORY_TEST_LIB_DIR ?? path.join(tmpdir(), "omp-memory-test-lib", TEST_OMP_VERSION));
		const source = ["src/core/beam/store.ts", "src/core/beam/schema.ts", "src/core/annotations.ts", "src/core/beam/helpers.ts"].map(file => readFileSync(path.join(pkgDir, file), "utf8")).join("\n");
		expect(unmetRequirements(source)).toEqual([]);
	});
});

describe("setup cleans up after itself", () => {
	/** A registry that serves one package, @oh-my-pi/pi-mnemopi@9.9.9, whose Mnemopi class cannot open a database. */
	function brokenRegistry(): { url: string; stop(): void } {
		const pack = path.join(root, "broken-pack");
		mkdirSync(path.join(pack, "package", "src", "core", "beam"), { recursive: true });
		writeFileSync(path.join(pack, "package", "package.json"), JSON.stringify({ name: "@oh-my-pi/pi-mnemopi", version: "9.9.9", type: "module", main: "./src/index.ts" }));
		writeFileSync(path.join(pack, "package", "src", "index.ts"), 'export class Mnemopi { constructor() { throw new Error("this library cannot open a bank"); } }\n');
		writeFileSync(path.join(pack, "package", "src", "core", "beam", "helpers.ts"), "export const scheduleEmbedding = () => {};\n");
		writeFileSync(path.join(pack, "package", "src", "core", "beam", "store.ts"), "export const forgetWorking = () => false;\n");
		writeFileSync(path.join(pack, "package", "src", "core", "runtime-options.ts"), "export const withMnemopiRuntimeOptions = (_o: unknown, fn: () => unknown) => fn();\n");
		const tarball = path.join(pack, "pi-mnemopi-9.9.9.tgz");
		const tar = Bun.spawnSync(["tar", "-czf", tarball, "-C", pack, "package"]);
		if (tar.exitCode !== 0) throw new Error(`tar failed: ${tar.stderr.toString()}`);
		const bytes = readFileSync(tarball);
		const integrity = `sha512-${new Bun.CryptoHasher("sha512").update(bytes).digest("base64")}`;
		const shasum = new Bun.CryptoHasher("sha1").update(bytes).digest("hex");
		const where = { port: 0 };
		const server = Bun.serve({
			port: 0,
			hostname: "127.0.0.1",
			fetch(request): Response {
				const pathname = decodeURIComponent(new URL(request.url).pathname);
				if (pathname.endsWith(".tgz")) return new Response(bytes);
				const dist = { tarball: `http://127.0.0.1:${where.port}/pi-mnemopi-9.9.9.tgz`, integrity, shasum };
				const version = { name: "@oh-my-pi/pi-mnemopi", version: "9.9.9", dist };
				if (pathname.endsWith("/9.9.9")) return Response.json(version);
				return Response.json({ name: "@oh-my-pi/pi-mnemopi", "dist-tags": { latest: "9.9.9" }, versions: { "9.9.9": version } });
			},
		});
		where.port = server.port ?? 0;
		return { url: `http://127.0.0.1:${where.port}`, stop: () => server.stop(true) };
	}

	test("a library that fails the self-test is removed again: nothing is left installed and setup says so", async () => {
		const registry = brokenRegistry();
		const target = path.join(root, "setup-broken");
		const lines: string[] = [];
		const savedCache = process.env.BUN_INSTALL_CACHE_DIR;
		process.env.BUN_INSTALL_CACHE_DIR = path.join(root, "bun-cache"); // the fake package never enters the user's Bun cache
		try {
			const report = await runSetup({ ompBin: "omp", ompVersion: "9.9.9", libDir: target, registry: registry.url, dryRun: false, embeddings: false, say: line => lines.push(line) });
			expect(report.ok).toBe(false);
			expect(report.action).toBe("removed");
			expect(report.library).toBeNull();
			expect(report.selfTest).toContain("failed");
			expect(report.problems.join(" ")).toContain("self-test failed");
			expect(existsSync(target)).toBe(false);
			expect(lines.join("\n")).toContain("the library was not left installed");
		} finally {
			registry.stop();
			if (savedCache === undefined) delete process.env.BUN_INSTALL_CACHE_DIR;
			else process.env.BUN_INSTALL_CACHE_DIR = savedCache;
		}
	});

	test("a directory that setup did not make is never replaced or removed", async () => {
		const target = path.join(root, "someones-folder");
		mkdirSync(target, { recursive: true });
		writeFileSync(path.join(target, "keep.txt"), "mine\n");
		const server = Bun.serve({ port: 0, hostname: "127.0.0.1", fetch: () => Response.json({}) });
		try {
			await expect(runSetup({ ompBin: "omp", ompVersion: "9.9.9", libDir: target, registry: `http://127.0.0.1:${server.port}`, dryRun: false, embeddings: false, say: () => {} })).rejects.toThrow(/was not made by omp-memory setup/);
		} finally {
			server.stop(true);
		}
		expect(readFileSync(path.join(target, "keep.txt"), "utf8")).toBe("mine\n");
	});
});

describe("the embedding runtime a failed setup cleans", () => {
	test("is the library's own per-version directory in omp's cache (the code that removes it is rmSync of exactly this path)", async () => {
		const libDir = await ensureTestLibrary();
		const dir = await fastembedRuntimeDir(libDir);
		expect(dir).not.toBeNull();
		expect(dir).toMatch(/[\\/]cache[\\/]fastembed-runtime[\\/]fastembed-/);
		expect(await fastembedRuntimeDir(path.join(root, "no-library-here"))).toBeNull();
	});
});
