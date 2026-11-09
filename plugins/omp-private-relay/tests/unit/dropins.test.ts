// The omp-services drop-ins: services/1 (how to keep the relay running) and health/1 (what to watch). The documents
// are the contract (docs/protocol/services-1.md, health-1.md in this repository once omp-services is merged); this
// test checks the output against the documented rules, and against omp-services' own JSON Schemas when they are present
// (set OMP_SERVICES_SCHEMAS to a directory that holds health-1.schema.json and services-1.schema.json, or merge the
// plugin: it is looked up in this repository).
import { afterEach, describe, expect, test } from "bun:test";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { loadConfig } from "../../src/config";
import { dropinPaths, healthDropin, servicesDropin, writeDropin } from "../../src/dropins";
import { type Schema, validate } from "../../src/schema";
import { list, obj, strings } from "./support/json";

const roots: string[] = [];
const home = (): string => {
	const r = fs.mkdtempSync(path.join(os.tmpdir(), "oprd-"));
	roots.push(r);
	return r;
};
afterEach(() => {
	for (const r of roots.splice(0)) fs.rmSync(r, { recursive: true, force: true });
});

const OMP = "/opt/omp bin/omp"; // a path with a space: nothing may be split on it

function docs(env: Record<string, string>) {
	const config = loadConfig(env);
	return { config, services: servicesDropin({ config, ompBin: OMP, env }), health: healthDropin({ config, ompBin: OMP, env }) };
}

const ID = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const PLUGIN = /^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$/;
const CHECK_FIELDS: Record<string, string[]> = {
	url: ["id", "type", "severity", "timeoutSeconds", "url", "expectStatus", "freshness"],
	port: ["id", "type", "severity", "timeoutSeconds", "host", "port"],
	"uid-owner": ["id", "type", "severity", "timeoutSeconds", "port", "uid"],
	"serve-route": ["id", "type", "severity", "timeoutSeconds", "port", "path", "target"],
	command: ["id", "type", "severity", "timeoutSeconds", "argv", "expectExit", "expectOutputContains"],
	"file-age": ["id", "type", "severity", "path", "maxAgeSeconds"],
	watchdog: ["id", "type", "severity", "timeoutSeconds", "service", "restart"],
};

describe("health/1 drop-in", () => {
	test("follows the documented rules: protocol, plugin name, 1 to 64 checks, unique ids, known types and fields only", () => {
		const { health } = docs({ HOME: home() });
		expect(health.protocol).toBe("health/1");
		expect(health.plugin).toBe("omp-private-relay");
		expect(String(health.plugin)).toMatch(PLUGIN);
		const checks = health.checks as Array<Record<string, unknown>>;
		expect(checks.length).toBeGreaterThanOrEqual(1);
		expect(checks.length).toBeLessThanOrEqual(64);
		expect(new Set(checks.map(c => c.id)).size).toBe(checks.length);
		for (const c of checks) {
			expect(String(c.id)).toMatch(ID);
			const allowed = CHECK_FIELDS[String(c.type)];
			expect(allowed, `type ${c.type}`).toBeDefined();
			for (const key of Object.keys(c)) expect(allowed, `${c.id}.${key}`).toContain(key);
		}
	});

	test("watches both ports, proves they are this relay, and asks the service manager about the service", () => {
		const { config, health } = docs({ HOME: home(), OMP_PRIVATE_RELAY_HOST_PORT: "21001", OMP_PRIVATE_RELAY_GUEST_PORT: "21002" });
		const byId = Object.fromEntries(list(health.checks).map(c => [String(c.id), c]));
		expect(obj(byId["relay-health"]).url).toBe(`http://127.0.0.1:${config.listen.hostPort}/healthz`);
		expect(byId["host-owner"]).toMatchObject({ type: "uid-owner", port: 21001 });
		expect(byId["guest-owner"]).toMatchObject({ type: "uid-owner", port: 21002 });
		expect(byId["guest-port"]).toMatchObject({ type: "port", port: 21002 });
		expect(byId["relay-service"]).toMatchObject({ type: "watchdog", service: "relay" });
		expect(obj(byId["relay-service"]).restart).toBeUndefined(); // the service manager restarts it; a watchdog only reports
		expect(byId["serve-route"]).toBeUndefined(); // the token adapter has no Serve route
	});

	test("the proof check is one command with an absolute first argument, no shell, the config path pinned and the omp path intact", () => {
		const { health } = docs({ HOME: home() });
		const proof = list(health.checks).find(c => c.id === "relay-proof") ?? {};
		const argv = strings(proof.argv);
		expect(proof.type).toBe("command");
		expect(argv[0]).toBe("/usr/bin/env");
		expect(argv.some(a => /^OMP_PRIVATE_RELAY_CONFIG=\//.test(a))).toBe(true);
		expect(argv).toContain("BUN_BE_BUN=1");
		expect(argv.slice(-3)).toEqual([OMP, expect.stringMatching(/src\/cli\.ts$/) as unknown as string, "check"]);
		expect(argv.some(a => a === "-c" || a === "/bin/sh")).toBe(false); // no shell text to quote
	});

	test("the tailscale adapter adds a Serve route check that points at the guest listener", () => {
		const { health } = docs({ HOME: home(), OMP_PRIVATE_RELAY_AUTH: "tailscale-serve", OMP_PRIVATE_RELAY_TAILSCALE_SERVE_PORT: "8443", OMP_PRIVATE_RELAY_GUEST_PORT: "21003", OMP_PRIVATE_RELAY_HOST_PORT: "21004" });
		const route = list(health.checks).find(c => c.type === "serve-route");
		expect(route).toMatchObject({ port: 8443, path: "/", target: "http://127.0.0.1:21003" });
	});

	test("holds no secret, no user name and no machine name", () => {
		const text = JSON.stringify(docs({ HOME: home() }).health);
		expect(text).not.toMatch(/check\.key|cookie|token|devices\.json|ts\.net/i);
	});
});

describe("services/1 drop-in", () => {
	test("one omp-script service named relay, with an absolute script and no shell", () => {
		const { services } = docs({ HOME: home() });
		expect(services.protocol).toBe("services/1");
		expect(services.plugin).toBe("omp-private-relay");
		const units = list(services.services);
		expect(units).toHaveLength(1);
		const s = units[0] as Record<string, unknown>;
		const program = obj(s.program);
		expect(s.name).toBe("relay");
		expect(program.type).toBe("omp-script");
		expect(path.isAbsolute(String(program.script))).toBe(true);
		expect(program.script).toMatch(/src\/main\.ts$/);
		expect(s.protected).toBe(false);
		expect(obj(s.env).OMP_PRIVATE_RELAY_CONFIG).toMatch(/config\.json$/); // the service reads the file that was shown
		for (const key of Object.keys(s)) expect(["name", "description", "program", "env", "workingDirectory", "protected", "throttleSeconds"]).toContain(key);
		expect(JSON.stringify(s)).not.toContain("BUN_BE_BUN"); // reserved: omp-services sets it
	});

	test("a configuration file in another place is passed on, and the reserved variables are never used", () => {
		const root = home();
		const file = path.join(root, "alt", "relay.json");
		const { services } = docs({ HOME: root, OMP_PRIVATE_RELAY_CONFIG: file, XDG_CONFIG_HOME: path.join(root, "cfg") });
		const env = obj(obj(list(services.services)[0]).env);
		expect(env).toEqual({ OMP_PRIVATE_RELAY_CONFIG: file, XDG_CONFIG_HOME: path.join(root, "cfg"), XDG_DATA_HOME: path.join(root, ".local", "share") });
		for (const name of Object.keys(env)) expect(name).toMatch(/^[A-Z_][A-Z0-9_]*$/);
		for (const reserved of ["PATH", "HOME", "BUN_BE_BUN", "LD_PRELOAD"]) expect(env).not.toHaveProperty(reserved);
	});

	test("the script path is the stable plugin link when omp keeps one", () => {
		const root = home();
		const real = path.resolve(import.meta.dir, "..", "..");
		const modules = path.join(root, ".omp", "plugins", "node_modules");
		fs.mkdirSync(modules, { recursive: true });
		fs.symlinkSync(real, path.join(modules, "omp-private-relay"));
		const { services } = docs({ HOME: root });
		const script = String(obj(obj(list(services.services)[0]).program).script);
		expect(script).toBe(path.join(modules, "omp-private-relay", "src", "main.ts"));
	});
});

describe("writing the drop-ins", () => {
	test("files are 0600 in 0700 directories under the omp-services configuration directory; a symlink target is refused", () => {
		const root = home();
		const env = { HOME: root, XDG_CONFIG_HOME: path.join(root, "cfg") };
		const paths = dropinPaths(env);
		expect(paths.services).toBe(path.join(root, "cfg", "omp-services", "services.d", "omp-private-relay.json"));
		expect(paths.health).toBe(path.join(root, "cfg", "omp-services", "health.d", "omp-private-relay.json"));
		const { services, health } = docs(env);
		writeDropin(paths.services, services);
		writeDropin(paths.health, health);
		for (const f of [paths.services, paths.health]) {
			expect(fs.statSync(f).mode & 0o777).toBe(0o600);
			expect(fs.statSync(path.dirname(f)).mode & 0o777).toBe(0o700);
			expect(JSON.parse(fs.readFileSync(f, "utf8")).plugin).toBe("omp-private-relay");
		}
		expect(fs.readdirSync(path.dirname(paths.health))).toEqual(["omp-private-relay.json"]);
		fs.rmSync(paths.health);
		fs.writeFileSync(path.join(root, "target.json"), "{}");
		fs.symlinkSync(path.join(root, "target.json"), paths.health);
		expect(() => writeDropin(paths.health, health)).toThrow(/symlink/);
		expect(fs.readFileSync(path.join(root, "target.json"), "utf8")).toBe("{}");
	});
});

describe("against omp-services' own schemas, when they are available", () => {
	const candidates = [
		process.env.OMP_SERVICES_SCHEMAS,
		path.resolve(import.meta.dir, "..", "..", "..", "omp-services", "python", "omp_services", "schemas"),
	].filter((x): x is string => Boolean(x));
	const dir = candidates.find(d => fs.existsSync(path.join(d, "health-1.schema.json")));

	test("the release check requires omp-services' schemas to be present", () => {
		if (process.env.OMP_PRIVATE_RELAY_REQUIRE_SERVICES_SCHEMAS) expect(dir, "set OMP_SERVICES_SCHEMAS to omp-services' schemas directory").toBeDefined();
	});

	test.skipIf(!dir)("both drop-ins validate", () => {
		const load = (n: string) => JSON.parse(fs.readFileSync(path.join(dir as string, n), "utf8")) as Schema;
		for (const env of [{ HOME: home() }, { HOME: home(), OMP_PRIVATE_RELAY_AUTH: "tailscale-serve" }] as Array<Record<string, string>>) {
			const { services, health } = docs(env);
			expect(validate(health, load("health-1.schema.json"))).toEqual([]);
			expect(validate(services, load("services-1.schema.json"))).toEqual([]);
		}
	});
});
