// Configuration: defaults, the schema, environment overrides, refusal of untrustworthy files and unknown keys, and that
// the schema, the environment table and the documentation agree with the code.
import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { checkRaw, ConfigError, configFilePath, ENV_OVERRIDES, envLayer, loadConfig, loadSchema, readConfigFile } from "../../src/config";
import { validate } from "../../src/schema";
import { type Obj, obj } from "./support/json";

const roots: string[] = [];
function home(): string {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), "oprc-"));
	roots.push(root);
	return root;
}
afterEach(() => {
	for (const r of roots.splice(0)) fs.rmSync(r, { recursive: true, force: true });
});

const envFor = (root: string, extra: Record<string, string> = {}) => ({ HOME: root, ...extra });

function writeConfig(root: string, body: unknown, mode = 0o600): string {
	const dir = path.join(root, ".config", "omp-private-relay");
	fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
	const file = path.join(dir, "config.json");
	fs.writeFileSync(file, typeof body === "string" ? body : JSON.stringify(body), { mode });
	fs.chmodSync(file, mode);
	return file;
}

describe("defaults", () => {
	test("with no file and no environment: token adapter, loopback ports, private paths under the XDG directories", () => {
		const root = home();
		const c = loadConfig(envFor(root));
		expect(c.auth.adapter).toBe("token");
		expect(c.pairing.mode).toBe("own");
		expect(c.listen.hostPort).not.toBe(c.listen.guestPort);
		expect(c.pairing.dir).toBe(path.join(root, ".config", "omp-private-relay", "pairing"));
		expect(c.checkDir).toBe(path.join(root, ".config", "omp-private-relay", "check"));
		expect(c.web.dir).toBe(path.join(root, ".local", "share", "omp-private-relay", "web", "current"));
		expect(c.limits.recheckMs).toBeLessThanOrEqual(30_000);
	});

	test("XDG_CONFIG_HOME and XDG_DATA_HOME are honoured when absolute and ignored when relative", () => {
		const root = home();
		const c = loadConfig({ HOME: root, XDG_CONFIG_HOME: path.join(root, "cfg"), XDG_DATA_HOME: "relative/data" });
		expect(c.pairing.dir).toBe(path.join(root, "cfg", "omp-private-relay", "pairing"));
		expect(c.web.dir).toBe(path.join(root, ".local", "share", "omp-private-relay", "web", "current"));
	});

	test("no default names a person, a machine, a network or a project", () => {
		const text = JSON.stringify(loadConfig(envFor(home()))).toLowerCase();
		expect(text).not.toMatch(new RegExp(["ts\\.net", "enig" + "matix", "link" + "graph", "search" + "atlas", "sa" + "ad"].join("|")));
	});
});

describe("the file", () => {
	test("values from the file override defaults, and environment overrides the file", () => {
		const root = home();
		writeConfig(root, { schemaVersion: 1, listen: { hostPort: 20001, guestPort: 20002 }, limits: { maxRooms: 5 } });
		const c = loadConfig(envFor(root));
		expect([c.listen.hostPort, c.listen.guestPort, c.limits.maxRooms]).toEqual([20001, 20002, 5]);
		const e = loadConfig(envFor(root, { OMP_PRIVATE_RELAY_GUEST_PORT: "20003", OMP_PRIVATE_RELAY_MAX_ROOMS: "9" }));
		expect([e.listen.guestPort, e.limits.maxRooms]).toEqual([20003, 9]);
	});

	test("OMP_PRIVATE_RELAY_CONFIG names another file, and must be absolute", () => {
		const root = home();
		const file = path.join(root, "elsewhere.json");
		fs.writeFileSync(file, JSON.stringify({ schemaVersion: 1, listen: { hostPort: 20010, guestPort: 20011 } }), { mode: 0o600 });
		expect(loadConfig(envFor(root, { OMP_PRIVATE_RELAY_CONFIG: file })).listen.hostPort).toBe(20010);
		expect(() => configFilePath({ HOME: root, OMP_PRIVATE_RELAY_CONFIG: "relative.json" })).toThrow(ConfigError);
	});

	const bad: Array<[string, unknown, RegExp]> = [
		["a missing schemaVersion", { listen: {} }, /schemaVersion/],
		["another schemaVersion", { schemaVersion: 2 }, /schemaVersion/],
		["an unknown top-level key", { schemaVersion: 1, bind: "0.0.0.0" }, /\/bind: unknown property/],
		["a bind address under listen (the relay only ever listens on loopback)", { schemaVersion: 1, listen: { host: "0.0.0.0" } }, /\/listen\/host: unknown property/],
		["a port below 1024", { schemaVersion: 1, listen: { hostPort: 80 } }, /hostPort/],
		["a port above 65535", { schemaVersion: 1, listen: { guestPort: 70000 } }, /guestPort/],
		["a port given as text", { schemaVersion: 1, listen: { guestPort: "8000" } }, /type integer/],
		["the same port for both listeners", { schemaVersion: 1, listen: { hostPort: 20020, guestPort: 20020 } }, /must differ/],
		["an unknown adapter", { schemaVersion: 1, auth: { adapter: "local-tls" } }, /one of/],
		["a relative path", { schemaVersion: 1, web: { dir: "web" } }, /pattern/],
		["a cookie name with a separator", { schemaVersion: 1, pairing: { cookieName: "a;b" } }, /pattern/],
		["recheckMs above 30 s", { schemaVersion: 1, limits: { recheckMs: 60000 } }, /recheckMs/],
		["a tailscale serve port with the token adapter", { schemaVersion: 1, auth: { adapter: "token", tailscale: { servePort: 443 } } }, /servePort/],
		["allowed hosts with the tailscale adapter", { schemaVersion: 1, auth: { adapter: "tailscale-serve", token: { allowedHosts: ["127.0.0.1:2000"] } } }, /allowedHosts/],
		["an allowed host that is not host:port", { schemaVersion: 1, auth: { token: { allowedHosts: ["example"] } } }, /pattern/],
		["a secret-looking extra key", { schemaVersion: 1, pairing: { secret: "x" } }, /unknown property/],
	];
	for (const [name, body, message] of bad) {
		test(`refused: ${name}`, () => {
			const root = home();
			writeConfig(root, body);
			expect(() => loadConfig(envFor(root))).toThrow(message);
		});
	}

	test("refused: a file writable by group or others, a symlink, a non-JSON file, an array", () => {
		const root = home();
		writeConfig(root, { schemaVersion: 1 }, 0o664);
		expect(() => loadConfig(envFor(root))).toThrow(/writable by group or others/);
		const file = writeConfig(root, { schemaVersion: 1 });
		fs.renameSync(file, `${file}.real`);
		fs.symlinkSync(`${file}.real`, file);
		expect(() => loadConfig(envFor(root))).toThrow(/regular file/);
		fs.rmSync(file);
		fs.writeFileSync(file, "{nope", { mode: 0o600 });
		expect(() => readConfigFile(file)).toThrow(/not valid JSON/);
		fs.writeFileSync(file, "[]", { mode: 0o600 });
		expect(() => readConfigFile(file)).toThrow(/JSON object/);
	});

	test("every problem is reported at once", () => {
		const root = home();
		writeConfig(root, { schemaVersion: 1, listen: { hostPort: 80, guestPort: 70000 }, auth: { adapter: "x" } });
		try {
			loadConfig(envFor(root));
			throw new Error("should have thrown");
		} catch (err) {
			expect(err).toBeInstanceOf(ConfigError);
			expect((err as ConfigError).problems.length).toBeGreaterThanOrEqual(3);
		}
	});
});

describe("environment overrides", () => {
	test("a bad value is refused like a bad file value", () => {
		const root = home();
		expect(() => loadConfig(envFor(root, { OMP_PRIVATE_RELAY_GUEST_PORT: "abc" }))).toThrow(/OMP_PRIVATE_RELAY_GUEST_PORT/);
		expect(() => loadConfig(envFor(root, { OMP_PRIVATE_RELAY_GUEST_PORT: "80" }))).toThrow(/guestPort/);
		expect(() => loadConfig(envFor(root, { OMP_PRIVATE_RELAY_AUTH: "open" }))).toThrow(/one of/);
	});

	test("an empty variable is the same as an unset one", () => {
		const { layer } = envLayer({ OMP_PRIVATE_RELAY_GUEST_PORT: "" });
		expect(layer).toEqual({});
	});

	test("every override points at a key the schema has, and every settable key has an override or a reason not to", () => {
		const schema = loadSchema();
		for (const o of ENV_OVERRIDES) {
			let node: Obj = schema;
			for (const key of o.path) {
				const next = obj(node.properties)[key];
				expect(next, `${o.env} -> ${o.path.join(".")}`).toBeDefined();
				node = obj(next);
			}
		}
		const leaves: string[] = [];
		const walk = (node: Obj, trail: string[]) => {
			if (node.properties) for (const [k, v] of Object.entries(obj(node.properties))) walk(obj(v), [...trail, k]);
			else leaves.push(trail.join("."));
		};
		walk(schema, []);
		const overridden = new Set(ENV_OVERRIDES.map(o => o.path.join(".")));
		// schemaVersion is the file's own version; token.allowedHosts is a list, which an environment variable cannot hold well.
		const without = leaves.filter(l => !overridden.has(l)).sort();
		expect(without).toEqual(["auth.token.allowedHosts", "schemaVersion"]);
	});

	test("every environment variable of the plugin is documented in the README", () => {
		const readme = fs.readFileSync(path.join(import.meta.dir, "..", "..", "README.md"), "utf8");
		for (const o of ENV_OVERRIDES) expect(readme, o.env).toContain(o.env);
		expect(readme).toContain("OMP_PRIVATE_RELAY_CONFIG");
	});
});

describe("the schema", () => {
	test("is itself well formed: every $ref resolves, and defaults satisfy their own schema", () => {
		const schema = loadSchema();
		const refs = JSON.stringify(schema).match(/"\$ref":"#\/[^"]+"/g) ?? [];
		for (const r of refs) expect(validate(null, { $ref: r.slice(8, -1) } as never, schema)).toBeDefined();
		expect(checkRaw({ schemaVersion: 1 })).toEqual([]);
	});

	test("a full configuration built from the defaults validates", () => {
		const c = loadConfig(envFor(home()));
		expect(checkRaw(c)).toEqual([]);
	});
});
