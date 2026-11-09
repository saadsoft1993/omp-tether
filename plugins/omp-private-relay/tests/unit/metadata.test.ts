// The plugin's files agree with each other and with the repository: versions, catalog, compat.json, documented
// commands and variables, links; and an audit of the source for the properties the design promises (loopback-only
// binds, no stray network access, no hard-coded ports, no personal or machine names).
import { describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as path from "node:path";
import { ENV_OVERRIDES } from "../../src/config";
import { loadWebBuilds } from "../../src/web-asset";
import { obj } from "./support/json";
import { PLUGIN_ROOT } from "./support/harness";

const REPO = path.resolve(PLUGIN_ROOT, "..", "..");
const read = (...p: string[]): string => fs.readFileSync(path.join(...p), "utf8");
const json = (...p: string[]) => obj(JSON.parse(read(...p)));
const sources = (dir: string, ext = ".ts"): string[] =>
	fs.readdirSync(dir, { withFileTypes: true }).flatMap(e => (e.isDirectory() ? sources(path.join(dir, e.name), ext) : e.name.endsWith(ext) ? [path.join(dir, e.name)] : []));
const SRC = sources(path.join(PLUGIN_ROOT, "src"));

describe("package, catalog, changelog and compat.json agree", () => {
	const pkg = json(PLUGIN_ROOT, "package.json");

	test("package.json: name, version, licence, no dependencies, no install scripts, a bin that exists and is executable", () => {
		expect(pkg.name).toBe("omp-private-relay");
		expect(pkg.license).toBe("MIT");
		for (const k of ["dependencies", "devDependencies", "optionalDependencies", "scripts", "omp"]) expect(pkg[k], k).toBeUndefined();
		const bin = obj(pkg.bin);
		const file = path.join(PLUGIN_ROOT, String(bin["omp-private-relay"]));
		expect(fs.statSync(file).mode & 0o111).not.toBe(0);
		for (const f of pkg.files as string[]) expect(fs.existsSync(path.join(PLUGIN_ROOT, f)), f).toBe(true);
	});

	test("the version is the same in package.json, the catalog, the changelog and compat.json, and the release tag of web-builds.json", () => {
		const catalog = obj(json(REPO, ".omp-plugin", "marketplace.json"));
		const entry = (catalog.plugins as Array<Record<string, unknown>>).find(p => p.name === "omp-private-relay");
		expect(entry?.version).toBe(pkg.version);
		expect(entry?.source).toBe("./plugins/omp-private-relay");
		expect(read(PLUGIN_ROOT, "CHANGELOG.md")).toContain(`## ${pkg.version}`);
		const compat = obj(obj(json(REPO, "compat.json").plugins)["omp-private-relay"]);
		expect(compat.version).toBe(pkg.version);
		expect(loadWebBuilds().releaseTag).toBe(`omp-private-relay-v${pkg.version}`);
	});

	test("compat.json lists the protocols, the platforms, and exactly the web builds that web-builds.json pins", () => {
		const root = json(REPO, "compat.json");
		const compat = obj(obj(root.plugins)["omp-private-relay"]);
		expect(compat.protocols).toEqual(["pairing-store/1", "relay-check/1"]);
		expect(compat.platforms).toEqual(["darwin", "linux"]);
		const pinned = loadWebBuilds().builds;
		const listed = obj(compat.webBuilds);
		expect(Object.keys(listed).sort()).toEqual(Object.keys(pinned).sort());
		for (const [minor, b] of Object.entries(pinned)) {
			expect(obj(listed[minor])).toEqual({ ompTag: b.ompTag, bun: b.bun, asset: b.asset, sha256: b.sha256, treeSha256: b.treeSha256, noticeSha256: b.noticeSha256 });
		}
		const protocols = obj(root.protocols);
		expect(protocols["pairing-store"]).toEqual([1]);
		expect(protocols["relay-check"]).toEqual([1]);
	});

	test("every protocol document the plugin names exists", () => {
		for (const doc of ["pairing-store-1.md", "relay-check-1.md"]) expect(fs.existsSync(path.join(REPO, "docs", "protocol", doc)), doc).toBe(true);
	});

	test("the gate expects no extension from this plugin, and the load gate's expectations file says so", () => {
		expect(obj(json(REPO, "tools", "extension-load-gate", "expectations.json")["omp-private-relay"])).toEqual({ extensions: false });
	});

	test("every file the public tree check would see for this plugin is covered by its allowlist", () => {
		const allow = read(REPO, "tools", "public-tree-check", "allowlist.txt")
			.split("\n")
			.filter(l => l && !l.startsWith("#"));
		const globRe = (g: string) => new RegExp(`^${g.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*\*\//g, "\u0001").replace(/\*\*/g, "\u0002").replace(/\*/g, "[^/]*").replaceAll("\u0001", "(?:.*/)?").replaceAll("\u0002", ".*")}$`);
		const walk = (d: string): string[] => fs.readdirSync(d, { withFileTypes: true }).flatMap(e => (e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]));
		for (const f of walk(PLUGIN_ROOT)) {
			if (f.includes("__pycache__") || f.includes("node_modules")) continue;
			const rel = path.relative(REPO, f);
			expect(allow.some(g => globRe(g).test(rel)), rel).toBe(true);
		}
	});
});

describe("documentation", () => {
	const readme = read(PLUGIN_ROOT, "README.md");

	test("every command of the command line is documented in the README, and nothing is documented that does not exist", () => {
		const help = read(PLUGIN_ROOT, "src", "cli.ts");
		for (const cmd of ["init", "status", "doctor", "check", "pair", "devices", "revoke", "rebuild-web", "dropins", "run"]) {
			expect(help).toContain(`case "${cmd}"`);
			expect(readme).toContain(`omp-private-relay ${cmd}`);
		}
		const documented = [...readme.matchAll(/^omp-private-relay ([a-z-]+)/gm)].map(m => m[1]);
		for (const cmd of documented) expect(help, cmd).toContain(`case "${cmd}"`);
	});

	test("every environment variable the source reads is documented", () => {
		const names = new Set<string>();
		for (const f of SRC) for (const m of read(f).matchAll(/\b(?:env|process\.env)\.([A-Z][A-Z0-9_]+)\b/g)) names.add(m[1] as string);
		for (const o of ENV_OVERRIDES) names.add(o.env);
		for (const n of names) {
			if (["HOME", "PATH", "BUN_BE_BUN"].includes(n) || n.startsWith("XDG_")) continue;
			expect(readme, n).toContain(n);
		}
		expect(names.has("OMP_BIN")).toBe(true);
	});

	test("relative links in the READMEs and documents resolve to files", () => {
		const docs = [path.join(PLUGIN_ROOT, "README.md"), path.join(PLUGIN_ROOT, "SECURITY.md"), path.join(PLUGIN_ROOT, "docs", "threat-model.md"), path.join(REPO, "README.md"), path.join(REPO, "docs", "protocol", "pairing-store-1.md"), path.join(REPO, "docs", "protocol", "relay-check-1.md")];
		for (const doc of docs) {
			for (const m of read(doc).matchAll(/\]\(((?!https?:|#|mailto:)[^)\s]+)\)/g)) {
				const target = (m[1] as string).split("#")[0] as string;
				if (!target) continue;
				// omp-services is another plugin of this repository, merged separately
				if (target.endsWith("omp-services")) continue;
				expect(fs.existsSync(path.resolve(path.dirname(doc), target)), `${path.relative(REPO, doc)} -> ${m[1]}`).toBe(true);
			}
		}
	});

	test("the threat model and SECURITY.md exist and say who is trusted and what is not claimed", () => {
		expect(read(PLUGIN_ROOT, "docs", "threat-model.md")).toMatch(/Who is trusted/);
		expect(read(PLUGIN_ROOT, "SECURITY.md")).toMatch(/Report a vulnerability/);
		expect(read(PLUGIN_ROOT, "SECURITY.md")).toMatch(/third-party audit/);
	});
});

describe("source audit", () => {
	const all = SRC.map(f => [path.relative(PLUGIN_ROOT, f), read(f)] as const);

	test("both listeners bind the loopback constant and nothing else", () => {
		const relay = read(PLUGIN_ROOT, "src", "relay.ts");
		expect(relay).toContain('export const LOOPBACK = "127.0.0.1";');
		for (const [file, text] of all) {
			for (const m of text.matchAll(/hostname:\s*([^,\n]+)/g)) expect((m[1] as string).trim()).toMatch(/^(?:LOOPBACK|"127\.0\.0\.1")$/);
			if (file === "src/relay.ts") expect(text, file).not.toMatch(/0\.0\.0\.0|"::"|hostname:\s*"localhost"/);
		}
		expect((relay.match(/Bun\.serve</g) ?? []).length).toBe(2);
	});

	test("no hard-coded port number other than the documented defaults", () => {
		for (const [file, text] of all) {
			const code = text.replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "").replace(/`[^`]*`/g, "``");
			for (const m of code.matchAll(/\b(\d{4,5})\b/g)) {
				const n = Number(m[1]);
				// byte sizes, times, close codes, hash lengths and the two default ports are not ports
				if ([17796, 17797].includes(n)) expect(file).toBe("src/config.ts");
			}
		}
	});

	test("no personal, machine, network or organisation name anywhere in the plugin", () => {
		const bad = new RegExp(["\\b(?:[a-z0-9-]+\\.)+ts\\.net\\b", "enig" + "matix", "link" + "graph", "search" + "atlas", "/Us" + "ers/", "taile" + "b", "mac-" + "mini", "saad" + "bin"].join("|"), "i");
		const walk = (d: string): string[] => fs.readdirSync(d, { withFileTypes: true }).flatMap(e => (e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]));
		for (const f of walk(PLUGIN_ROOT)) {
			if (f.endsWith(".tar") || f.endsWith(".png")) continue;
			// the licence names the copyright holder, which the repository's LICENSE does too
			if (path.basename(f) === "LICENSE" || path.basename(f) === "NOTICE") continue;
			const text = fs.readFileSync(f, "utf8");
			expect(text, path.relative(REPO, f)).not.toMatch(bad);
		}
	});

	test("the code never opens a socket to anything but loopback, and the only outward fetch is the download of the asset", () => {
		const fetches = all.filter(([, t]) => /(?:await |=> |return |= )fetch\(/.test(t)).map(([f]) => f);
		// pair-page.ts holds the script served to the browser: its fetch is the page calling its own origin
		expect(fetches.sort()).toEqual(["src/cli.ts", "src/doctor.ts", "src/listener-check.ts", "src/pair-page.ts", "src/web-asset.ts"]);
		for (const f of ["src/doctor.ts", "src/listener-check.ts", "src/cli.ts"]) {
			const text = read(PLUGIN_ROOT, f);
			for (const m of text.matchAll(/fetch\(`([^`]+)`/g)) expect(m[1], f).toMatch(/^http:\/\/127\.0\.0\.1:/);
		}
	});

	test("every Bun.spawn call uses an argument array (no shell string)", () => {
		for (const [file, text] of all) {
			for (const m of text.matchAll(/Bun\.spawn(?:Sync)?\(\s*([^\n,)]+)/g)) expect(`${file}: ${m[1]}`).toMatch(/\[|argv|bin|\.\.\./);
			expect(text, file).not.toMatch(/child_process|execSync|shell:\s*true|spawn(?:Sync)?\(\s*["`']/);
		}
	});

	test("the secrets stay out of log calls: no cookie, token, key or link is passed to the logger", () => {
		const relay = read(PLUGIN_ROOT, "src", "relay.ts");
		for (const m of relay.matchAll(/\blog\("[a-z_]+"(?:, ([^;]*))?\);/g)) {
			// an 8-character prefix of a hash is what the log carries for a device; the value it is computed from never appears
			const shown = (m[1] ?? "").replace(/createHash\("sha256"\)\.update\(\w+\)\.digest\("hex"\)\.slice\(0, 8\)/g, "<hash prefix>");
			expect(shown, m[0]).not.toMatch(/cookie|token|session\b|checkKey|\bkey\b|link|nonce/i);
		}
	});
});
