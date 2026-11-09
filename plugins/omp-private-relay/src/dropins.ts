// What this plugin hands to omp-services: a services/1 drop-in (how to keep the relay running) and a health/1 drop-in
// (what to watch). This plugin never installs a unit itself. See docs/protocol/services-1.md and health-1.md in the
// omp-tether repository for the two formats; both documents are the contract, and omp-services owns the renderer.
//
// The drop-ins only hold paths and port numbers (no secrets), and are written only when you ask for them.
import * as fs from "node:fs";
import * as path from "node:path";
import { type Config, CONFIG_FILE_ENV, configFilePath, ENV_OVERRIDES } from "./config";
import { ensurePrivateDir } from "./fsutil";
import { configDir, dataDir, PLUGIN_NAME, pluginRoot, servicesConfigDir } from "./paths";

type Json = Record<string, unknown>;

export interface DropinContext {
	config: Config;
	/** Absolute path of the omp binary the service will be pinned to. */
	ompBin: string;
	env?: Record<string, string | undefined>;
}

/** The path a unit should name: the stable plugin link omp keeps up to date when it exists, else the real tree. */
export function stableScript(file: string, env: Record<string, string | undefined> = process.env): string {
	const real = pluginRoot();
	const home = env.HOME && path.isAbsolute(env.HOME) ? env.HOME : "";
	const link = home ? path.join(home, ".omp", "plugins", "node_modules", PLUGIN_NAME) : "";
	try {
		if (link && fs.realpathSync(link) === fs.realpathSync(real)) return path.join(link, file);
	} catch {
		// no such link: fall through
	}
	return path.join(real, file);
}

/**
 * The environment that makes the service and the proof command read the SAME configuration as the one shown to the person:
 * the absolute path of the config file, and the XDG roots when they are set (the defaults inside the config derive from them).
 */
export function pinnedEnv(env: Record<string, string | undefined>): Record<string, string> {
	// The RESOLVED roots (the defaults included), so a service manager or a heartbeat with other XDG variables in its own
	// environment cannot move the pairing, check and web paths away from the ones the person was shown.
	return {
		[CONFIG_FILE_ENV]: configFilePath(env),
		XDG_CONFIG_HOME: path.dirname(configDir(env)),
		XDG_DATA_HOME: path.dirname(dataDir(env)),
	};
}

/** Names of OMP_PRIVATE_RELAY_* variables set in `env` that a service would not see (everything but the config path). */
export function strayOverrides(env: Record<string, string | undefined>): string[] {
	return ENV_OVERRIDES.map(o => o.env).filter(name => env[name] !== undefined && env[name] !== "");
}

export function servicesDropin(ctx: DropinContext): Json {
	const env = ctx.env ?? process.env;
	return {
		protocol: "services/1",
		plugin: PLUGIN_NAME,
		services: [
			{
				name: "relay",
				description: "Private Collab relay for omp",
				program: { type: "omp-script", script: stableScript("src/main.ts", env) },
				env: pinnedEnv(env),
				protected: false,
				throttleSeconds: 10,
			},
		],
	};
}

export function healthDropin(ctx: DropinContext): Json {
	const { config } = ctx;
	const host = config.listen.hostPort;
	const guest = config.listen.guestPort;
	const env = ctx.env ?? process.env;
	const checks: Json[] = [
		{ id: "relay-health", type: "url", url: `http://127.0.0.1:${host}/healthz` },
		{ id: "guest-port", type: "port", port: guest },
		{ id: "host-owner", type: "uid-owner", port: host },
		{ id: "guest-owner", type: "uid-owner", port: guest },
		// Both listeners answer a fresh nonce with an HMAC under the private key file: proof that they are this relay.
		{
			id: "relay-proof",
			type: "command",
			argv: ["/usr/bin/env", ...Object.entries(pinnedEnv(env)).map(([k, v]) => `${k}=${v}`), "BUN_BE_BUN=1", ctx.ompBin, stableScript("src/cli.ts", env), "check"],
			timeoutSeconds: 30,
		},
		{ id: "relay-service", type: "watchdog", service: "relay" },
	];
	if (config.auth.adapter === "tailscale-serve") {
		checks.push({
			id: "serve-route",
			type: "serve-route",
			port: config.auth.tailscale.servePort ?? guest,
			path: "/",
			target: `http://127.0.0.1:${guest}`,
		});
	}
	return { protocol: "health/1", plugin: PLUGIN_NAME, checks };
}

export function dropinPaths(env: Record<string, string | undefined> = process.env): { services: string; health: string } {
	const base = servicesConfigDir(env);
	return {
		services: path.join(base, "services.d", `${PLUGIN_NAME}.json`),
		health: path.join(base, "health.d", `${PLUGIN_NAME}.json`),
	};
}

/** Writes one drop-in atomically (0600) into its directory (0700, created when missing). Returns the path. */
export function writeDropin(file: string, doc: Json): string {
	ensurePrivateDir(path.dirname(file), { tighten: false });
	if (fs.existsSync(file) && fs.lstatSync(file).isSymbolicLink()) throw new Error(`${file} is a symlink; remove it first`);
	const tmp = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}`);
	fs.rmSync(tmp, { force: true });
	fs.writeFileSync(tmp, `${JSON.stringify(doc, null, "\t")}\n`, { mode: 0o600, flag: "wx" });
	fs.renameSync(tmp, file);
	return file;
}
