// The version, id and protocol numbers are stated in several files; they must not drift apart.
import { describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { EXTENSION_ID, PLUGIN_NAME, PLUGIN_VERSION } from "../../src/omp-services";

const PLUGIN_DIR = path.resolve(import.meta.dir, "../..");
const REPO = path.resolve(PLUGIN_DIR, "../..");
const read = (p: string) => fs.readFileSync(p, "utf8");
const json = (p: string) => JSON.parse(read(p));
const pkg = json(path.join(PLUGIN_DIR, "package.json"));
const catalog = json(path.join(REPO, ".omp-plugin/marketplace.json"));
const compat = json(path.join(REPO, "compat.json"));
const init = read(path.join(PLUGIN_DIR, "python/omp_services/__init__.py"));
const pyVersion = (name: string) => new RegExp(`^${name}\\s*=\\s*"([^"]+)"`, "m").exec(init)?.[1];

describe("metadata agrees across files", () => {
	test("name, version and extension id", () => {
		expect(pkg.name).toBe(PLUGIN_NAME);
		expect(pkg.version).toBe(PLUGIN_VERSION);
		expect(pyVersion("__version__")).toBe(PLUGIN_VERSION);
		expect(EXTENSION_ID).toBe(`extension-module:${PLUGIN_NAME}`);
		expect(compat.plugins[PLUGIN_NAME].version).toBe(PLUGIN_VERSION);
		expect(compat.plugins[PLUGIN_NAME].extensionId).toBe(EXTENSION_ID);
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
		expect(read(path.join(PLUGIN_DIR, "CHANGELOG.md"))).toContain(`## ${PLUGIN_VERSION}`);
		expect(read(path.join(PLUGIN_DIR, "LICENSE"))).toBe(read(path.join(REPO, "LICENSE")));
	});

	test("the package has no runtime dependencies, no install scripts and no bin", () => {
		for (const key of ["dependencies", "optionalDependencies", "devDependencies", "scripts", "bin"]) expect(pkg[key]).toBeUndefined();
		expect(fs.existsSync(path.join(PLUGIN_DIR, "pyproject.toml"))).toBe(false); // no pip route: it could not supply the runtime layout
	});

	test("supported omp versions: the code and compat.json state the same minimum and tested list", () => {
		const min = /MIN_OMP = \((\d+), (\d+), (\d+)\)/.exec(init);
		expect(`${min?.[1]}.${min?.[2]}.${min?.[3]}`).toBe(compat.omp.minimum);
		const tested = /TESTED_OMP = \(([^)]*)\)/.exec(init)?.[1]?.match(/"([^"]+)"/g)?.map(s => s.replace(/"/g, ""));
		expect(tested).toEqual(compat.omp.tested);
		expect(pyVersion("MIN_PYTHON")).toBeUndefined(); // a tuple, not a string
		expect(init).toMatch(/MIN_PYTHON = \(3, 10\)/);
	});

	test("compat.json lists the protocols this plugin reads, with their documents", () => {
		expect(compat.protocols.health).toEqual([1]);
		expect(compat.protocols.services).toEqual([1]);
		expect(compat.protocols.config).toEqual([1]);
		for (const doc of ["health-1.md", "services-1.md"]) expect(fs.existsSync(path.join(REPO, "docs/protocol", doc))).toBe(true);
		for (const schema of ["config-1", "health-1", "services-1"]) {
			const s = json(path.join(PLUGIN_DIR, `python/omp_services/schemas/${schema}.schema.json`));
			expect(s.$id).toContain(`${schema}.schema.json`);
		}
		const health = read(path.join(PLUGIN_DIR, "python/omp_services/schemas/health-1.schema.json"));
		expect(health).toContain('"const": "health/1"');
	});

	test("the extension-load gate expects the command this plugin registers", () => {
		const expectations = json(path.join(REPO, "tools/extension-load-gate/expectations.json"));
		expect(expectations[PLUGIN_NAME]).toEqual({ commands: ["omp-services"] });
	});

	test("the files list names things that exist", () => {
		for (const entry of pkg.files) expect(fs.existsSync(path.join(PLUGIN_DIR, entry))).toBe(true);
	});

	test("the launcher and installer are executable and the public docs exist", () => {
		for (const f of ["bin/omp-services", "install.sh", "python/omp_services_main.py"]) expect((fs.statSync(path.join(PLUGIN_DIR, f)).mode & 0o111) !== 0).toBe(true);
		for (const f of ["README.md", "SECURITY.md", "docs/threat-model.md"]) expect(fs.existsSync(path.join(PLUGIN_DIR, f))).toBe(true);
	});
});
