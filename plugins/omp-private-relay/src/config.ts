// Configuration: one JSON file, a JSON Schema that describes it, and OMP_PRIVATE_RELAY_* environment overrides.
//
// Precedence for every key: environment variable, then the file, then the default. The result is validated against
// `schemas/config-1.schema.json` as a whole, so a bad environment value is refused exactly like a bad file value.
// Secrets are never part of the configuration; they live in separate 0600 files in 0700 directories.
import * as fs from "node:fs";
import * as path from "node:path";
import { NONBLOCK, NOFOLLOW, UID } from "./fsutil";
import { configDir, dataDir, pluginRoot } from "./paths";
import { type Schema, validate } from "./schema";

export type AuthAdapterName = "token" | "tailscale-serve";

export interface Limits {
	maxRooms: number;
	maxGuestsPerRoom: number;
	/** Open sockets of every kind (hosts and browsers, admitted or not yet admitted). */
	maxConnections: number;
	maxHostFrame: number;
	maxGuestFrame: number;
	connQueueMax: number;
	relayQueueMax: number;
	recheckMs: number;
	restartGraceMs: number;
}

/** The configuration with every default filled in and every path made absolute. */
export interface Config {
	schemaVersion: 1;
	omp: { bin: string | null };
	listen: { hostPort: number; guestPort: number };
	auth: {
		adapter: AuthAdapterName;
		token: { allowedHosts: string[] };
		tailscale: { bin: string; servePort: number | null; owner: string; publicHost: string };
	};
	pairing: { mode: "own" | "shared"; dir: string; cookieName: string };
	web: { dir: string };
	checkDir: string;
	limits: Limits;
}

const MiB = 1024 * 1024;

export const DEFAULT_PORTS = { hostPort: 17797, guestPort: 17796 } as const;

export const DEFAULT_LIMITS: Limits = {
	maxRooms: 32,
	maxGuestsPerRoom: 8,
	maxConnections: 256,
	maxHostFrame: 16 * MiB,
	maxGuestFrame: 4 * MiB,
	connQueueMax: 32 * MiB,
	relayQueueMax: 128 * MiB,
	recheckMs: 10_000,
	restartGraceMs: 60_000,
};

type Env = Record<string, string | undefined>;

/** One configuration key that an environment variable can override. */
export interface EnvOverride {
	env: string;
	path: string[];
	kind: "int" | "string";
}

export const ENV_OVERRIDES: EnvOverride[] = [
	{ env: "OMP_PRIVATE_RELAY_OMP_BIN", path: ["omp", "bin"], kind: "string" },
	{ env: "OMP_PRIVATE_RELAY_HOST_PORT", path: ["listen", "hostPort"], kind: "int" },
	{ env: "OMP_PRIVATE_RELAY_GUEST_PORT", path: ["listen", "guestPort"], kind: "int" },
	{ env: "OMP_PRIVATE_RELAY_AUTH", path: ["auth", "adapter"], kind: "string" },
	{ env: "OMP_PRIVATE_RELAY_TAILSCALE_BIN", path: ["auth", "tailscale", "bin"], kind: "string" },
	{ env: "OMP_PRIVATE_RELAY_TAILSCALE_SERVE_PORT", path: ["auth", "tailscale", "servePort"], kind: "int" },
	{ env: "OMP_PRIVATE_RELAY_TAILSCALE_OWNER", path: ["auth", "tailscale", "owner"], kind: "string" },
	{ env: "OMP_PRIVATE_RELAY_TAILSCALE_HOST", path: ["auth", "tailscale", "publicHost"], kind: "string" },
	{ env: "OMP_PRIVATE_RELAY_PAIRING_MODE", path: ["pairing", "mode"], kind: "string" },
	{ env: "OMP_PRIVATE_RELAY_PAIRING_DIR", path: ["pairing", "dir"], kind: "string" },
	{ env: "OMP_PRIVATE_RELAY_COOKIE_NAME", path: ["pairing", "cookieName"], kind: "string" },
	{ env: "OMP_PRIVATE_RELAY_WEB_DIR", path: ["web", "dir"], kind: "string" },
	{ env: "OMP_PRIVATE_RELAY_CHECK_DIR", path: ["checkDir"], kind: "string" },
	{ env: "OMP_PRIVATE_RELAY_MAX_ROOMS", path: ["limits", "maxRooms"], kind: "int" },
	{ env: "OMP_PRIVATE_RELAY_MAX_GUESTS", path: ["limits", "maxGuestsPerRoom"], kind: "int" },
	{ env: "OMP_PRIVATE_RELAY_MAX_CONNECTIONS", path: ["limits", "maxConnections"], kind: "int" },
	{ env: "OMP_PRIVATE_RELAY_MAX_HOST_FRAME", path: ["limits", "maxHostFrame"], kind: "int" },
	{ env: "OMP_PRIVATE_RELAY_MAX_GUEST_FRAME", path: ["limits", "maxGuestFrame"], kind: "int" },
	{ env: "OMP_PRIVATE_RELAY_CONN_QUEUE_MAX", path: ["limits", "connQueueMax"], kind: "int" },
	{ env: "OMP_PRIVATE_RELAY_QUEUE_MAX", path: ["limits", "relayQueueMax"], kind: "int" },
	{ env: "OMP_PRIVATE_RELAY_RECHECK_MS", path: ["limits", "recheckMs"], kind: "int" },
	{ env: "OMP_PRIVATE_RELAY_RESTART_GRACE_MS", path: ["limits", "restartGraceMs"], kind: "int" },
];

/** Variable naming the config file itself (not a key inside it). */
export const CONFIG_FILE_ENV = "OMP_PRIVATE_RELAY_CONFIG";

export class ConfigError extends Error {
	constructor(readonly problems: string[]) {
		super(`invalid configuration:\n  ${problems.join("\n  ")}`);
		this.name = "ConfigError";
	}
}

export function schemaFile(): string {
	return path.join(pluginRoot(), "schemas", "config-1.schema.json");
}

export function loadSchema(): Schema {
	return JSON.parse(fs.readFileSync(schemaFile(), "utf8")) as Schema;
}

export function configFilePath(env: Env = process.env): string {
	const explicit = env[CONFIG_FILE_ENV];
	if (explicit) {
		if (!path.isAbsolute(explicit)) throw new ConfigError([`${CONFIG_FILE_ENV} must be an absolute path`]);
		return explicit;
	}
	return path.join(configDir(env), "config.json");
}

type Json = Record<string, unknown>;

function isObject(value: unknown): value is Json {
	return value !== null && typeof value === "object" && !Array.isArray(value);
}

function setPath(target: Json, keys: string[], value: unknown): void {
	let node = target;
	for (const key of keys.slice(0, -1)) {
		const next = node[key];
		if (isObject(next)) node = next;
		else {
			const fresh: Json = {};
			node[key] = fresh;
			node = fresh;
		}
	}
	node[keys[keys.length - 1] as string] = value;
}

/** The config file as written (not yet validated), or null when there is none. Refuses an untrustworthy file. */
export function readConfigFile(file: string): Json | null {
	// The parent directory decides who can replace the file: a directory that group or others can write to (and that is not
	// sticky) lets another account swap it, so it is refused before the file is looked at.
	try {
		const parent = fs.statSync(path.dirname(file));
		if (parent.mode & 0o022 && !(parent.mode & 0o1000)) throw new ConfigError([`${file}: its directory is writable by group or others, who could replace the file`]);
	} catch (err) {
		if (err instanceof ConfigError) throw err;
		if ((err as NodeJS.ErrnoException).code === "ENOENT") return null;
	}
	// One open, one check on the open descriptor, one bounded read from it: nothing can be swapped in between.
	let fd: number;
	try {
		fd = fs.openSync(file, fs.constants.O_RDONLY | NOFOLLOW | NONBLOCK);
	} catch (err) {
		const code = (err as NodeJS.ErrnoException).code;
		if (code === "ENOENT") return null;
		if (code === "ELOOP" || code === "EMLINK") throw new ConfigError([`${file}: must be a regular file, not a link or special file`]);
		throw new ConfigError([`${file}: cannot be read (${code ?? "error"})`]);
	}
	try {
		const st = fs.fstatSync(fd);
		if (!st.isFile()) throw new ConfigError([`${file}: must be a regular file, not a link or special file`]);
		if (st.uid !== UID) throw new ConfigError([`${file}: is owned by another account`]);
		if (st.mode & 0o022) throw new ConfigError([`${file}: is writable by group or others; run: chmod 600 '${file}'`]);
		if (st.size > 256 * 1024) throw new ConfigError([`${file}: is larger than a configuration file should be`]);
		let parsed: unknown;
		try {
			parsed = JSON.parse(fs.readFileSync(fd, "utf8"));
		} catch {
			throw new ConfigError([`${file}: is not valid JSON`]);
		}
		if (!isObject(parsed)) throw new ConfigError([`${file}: must hold a JSON object`]);
		return parsed;
	} finally {
		fs.closeSync(fd);
	}
}

/** Environment overrides as a nested object, ready to merge over the file's values. */
export function envLayer(env: Env): { layer: Json; problems: string[] } {
	const layer: Json = {};
	const problems: string[] = [];
	for (const o of ENV_OVERRIDES) {
		const raw = env[o.env];
		if (raw === undefined || raw === "") continue;
		if (o.kind === "int") {
			if (!/^\d{1,10}$/.test(raw)) {
				problems.push(`${o.env}: must be a non-negative integer`);
				continue;
			}
			setPath(layer, o.path, Number(raw));
		} else setPath(layer, o.path, raw);
	}
	return { layer, problems };
}

function merge(base: Json, over: Json): Json {
	const out: Json = { ...base };
	for (const [key, value] of Object.entries(over)) {
		const current = out[key];
		out[key] = isObject(current) && isObject(value) ? merge(current, value) : value;
	}
	return out;
}

function defaults(env: Env): Json {
	return {
		schemaVersion: 1,
		omp: { bin: null },
		listen: { ...DEFAULT_PORTS },
		auth: {
			adapter: "token",
			token: { allowedHosts: [] },
			tailscale: { bin: "tailscale", servePort: null, owner: "", publicHost: "" },
		},
		pairing: { mode: "own", dir: path.join(configDir(env), "pairing"), cookieName: "omp_relay" },
		web: { dir: path.join(dataDir(env), "web", "current") },
		checkDir: path.join(configDir(env), "check"),
		limits: { ...DEFAULT_LIMITS },
	};
}

/** Rules the schema cannot express. */
function semanticProblems(c: Config): string[] {
	const problems: string[] = [];
	if (c.listen.hostPort === c.listen.guestPort) problems.push("listen.hostPort and listen.guestPort must differ");
	if (c.auth.tailscale.servePort !== null && c.auth.adapter !== "tailscale-serve") {
		problems.push("auth.tailscale.servePort only applies when auth.adapter is tailscale-serve");
	}
	if (c.auth.adapter !== "token" && c.auth.token.allowedHosts.length > 0) {
		problems.push("auth.token.allowedHosts only applies when auth.adapter is token");
	}
	for (const host of c.auth.token.allowedHosts) {
		const port = Number(host.slice(host.lastIndexOf(":") + 1));
		if (!(port >= 1 && port <= 65_535)) problems.push(`auth.token.allowedHosts: ${host} has no valid port`);
	}
	return problems;
}

/** Validates a raw, merged configuration object. Returns the problems; an empty list means it is usable. */
export function checkRaw(raw: unknown): string[] {
	return validate(raw, loadSchema());
}

/** Builds the effective configuration. Throws ConfigError, listing every problem found. */
export function loadConfig(env: Env = process.env, opts: { file?: string } = {}): Config {
	const file = opts.file ?? configFilePath(env);
	const fromFile = readConfigFile(file);
	const { layer, problems: envProblems } = envLayer(env);
	const merged = merge(merge(defaults(env), fromFile ?? {}), layer);
	// The defaults carry schemaVersion, so the file's own declaration has to be checked separately.
	const fileProblems = fromFile && !("schemaVersion" in fromFile) ? [`${file}: missing required property "schemaVersion"`] : [];
	const problems = [...fileProblems, ...envProblems, ...checkRaw(merged)];
	if (problems.length === 0) problems.push(...semanticProblems(merged as unknown as Config));
	if (problems.length > 0) throw new ConfigError(problems);
	return merged as unknown as Config;
}

/** The file's own values only (what `init` writes): no defaults, no environment. */
export function minimalFile(parts: Json): Json {
	return { schemaVersion: 1, ...parts };
}

/** The relay's address as omp's `collab.relayUrl` setting wants it. */
export function relayUrl(c: Config): string {
	return `ws://127.0.0.1:${c.listen.hostPort}`;
}
