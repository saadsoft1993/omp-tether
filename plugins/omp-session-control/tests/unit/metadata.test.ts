// The version, id and settings are stated in several files; they must not drift apart.
import { describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { DEFAULT_IDLE_TIMEOUT_MS, EXTENSION_ID, PLUGIN_NAME, PLUGIN_VERSION, PROTOCOL_VERSIONS } from "../../src/omp-session-control";

const PLUGIN_DIR = path.resolve(import.meta.dir, "../..");
const REPO = path.resolve(PLUGIN_DIR, "../..");
const json = (p: string) => JSON.parse(fs.readFileSync(p, "utf8"));
const pkg = json(path.join(PLUGIN_DIR, "package.json"));
const catalog = json(path.join(REPO, ".omp-plugin/marketplace.json"));
const compat = json(path.join(REPO, "compat.json"));

describe("metadata agrees across files", () => {
	test("package name, version and extension id", () => {
		expect(pkg.name).toBe(PLUGIN_NAME);
		expect(pkg.version).toBe(PLUGIN_VERSION);
		expect(EXTENSION_ID).toBe(`extension-module:${PLUGIN_NAME}`);
		expect(compat.plugins[PLUGIN_NAME].version).toBe(PLUGIN_VERSION);
		expect(compat.plugins[PLUGIN_NAME].extensionId).toBe(EXTENSION_ID);
		expect(compat.protocols["session-control"]).toEqual([...PROTOCOL_VERSIONS]);
	});

	test("the entry file is named after the plugin (omp derives the extension id from the file name)", () => {
		expect(pkg.omp.extensions).toEqual([`./src/${PLUGIN_NAME}.ts`]);
		expect(fs.existsSync(path.join(PLUGIN_DIR, pkg.omp.extensions[0]))).toBe(true);
	});

	test("the catalog entry points at this plugin with the same version", () => {
		const entry = catalog.plugins.find((p: { name: string }) => p.name === PLUGIN_NAME);
		expect(entry.version).toBe(PLUGIN_VERSION);
		expect(path.resolve(REPO, entry.source)).toBe(PLUGIN_DIR);
	});

	test("the changelog and the licence are present and current", () => {
		expect(fs.readFileSync(path.join(PLUGIN_DIR, "CHANGELOG.md"), "utf8")).toContain(`## ${PLUGIN_VERSION}`);
		expect(fs.readFileSync(path.join(PLUGIN_DIR, "LICENSE"), "utf8")).toBe(fs.readFileSync(path.join(REPO, "LICENSE"), "utf8"));
	});

	test("declared settings match the code's defaults and limits", () => {
		const s = pkg.omp.settings;
		expect(Object.keys(s).sort()).toEqual(["controlDir", "debugLog", "idleTimeoutMs"]);
		expect(s.idleTimeoutMs.default).toBe(DEFAULT_IDLE_TIMEOUT_MS);
		expect([s.idleTimeoutMs.min, s.idleTimeoutMs.max]).toEqual([100, 3_600_000]);
		expect(s.controlDir.env).toBe("OMP_SESSION_CONTROL_DIR");
		expect(s.idleTimeoutMs.env).toBe("OMP_SESSION_CONTROL_IDLE_MS");
		expect(s.debugLog.env).toBe("OMP_SESSION_CONTROL_DEBUG");
		expect(s.debugLog.default).toBe(false);
	});

	test("the package has no runtime dependencies and no install scripts", () => {
		for (const key of ["dependencies", "optionalDependencies", "devDependencies", "scripts", "bin"]) expect(pkg[key]).toBeUndefined();
	});

	test("the protocol document and the code state the same limits", () => {
		const doc = fs.readFileSync(path.join(REPO, "docs/protocol/control-1.md"), "utf8");
		const source = fs.readFileSync(path.join(PLUGIN_DIR, "src/omp-session-control.ts"), "utf8");
		for (const [pattern, text] of [
			[/MAX_FRAME_BYTES = 64 \* 1024/, "65 536 bytes"],
			[/MAX_CLIENTS = 4/, "Simultaneous clients | 4"],
			[/MAX_OUTSTANDING = 16/, "Requests in flight | 16"],
			[/MAX_NAME_CHARS = 120/, "Name | 120"],
			[/MAX_REGISTRY_BYTES = 4096/, "Registry entry | 4096"],
			[/MAX_WRITE_BACKLOG = 256 \* 1024/, "256 KiB"],
			[/MAX_LOG_BYTES = 256 \* 1024/, "256 KiB"],
			[/MAX_CONNECTIONS = MAX_CLIENTS \+ 12/, "Open connections | at most 16"],
			[/OP_TIMEOUT_MS = 10_000/, "Operation | 10 s"],
			[/FRAME_DEADLINE_MS = 10_000/, "complete 10 s after its first byte"],
			[/CLOSE_GRACE_MS = 2_000/, "or after 2 s"],
			[/RECHECK_MS = 2_000;/, "recheck every 2 s"],
			[/MAX_LOG_RECORD_BYTES = 2048/, "at most 2 KiB"],
			[/MAX_DEBUG_TEXT = 200/, "cut to 200 characters"],
		] as const) {
			expect(source).toMatch(pattern);
			expect(doc).toContain(text);
		}
	});
});
