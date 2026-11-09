// Where the plugin keeps things. Everything follows the XDG base directories; a relative XDG value is ignored, as the
// specification says. Nothing here touches the disk.
import * as os from "node:os";
import * as path from "node:path";

export const PLUGIN_NAME = "omp-private-relay";

type Env = Record<string, string | undefined>;

function home(env: Env): string {
	const h = env.HOME;
	return h && path.isAbsolute(h) ? h : os.homedir();
}

function xdg(env: Env, variable: string, fallback: string): string {
	const value = env[variable];
	return value && path.isAbsolute(value) ? value : path.join(home(env), fallback);
}

/** Configuration and the private files next to it (pairing store, listener check key). */
export function configDir(env: Env = process.env): string {
	return path.join(xdg(env, "XDG_CONFIG_HOME", ".config"), PLUGIN_NAME);
}

/** Data that can be rebuilt: the downloaded web client. */
export function dataDir(env: Env = process.env): string {
	return path.join(xdg(env, "XDG_DATA_HOME", path.join(".local", "share")), PLUGIN_NAME);
}

/** The drop-in directories of omp-services, which this plugin only writes to when asked. */
export function servicesConfigDir(env: Env = process.env): string {
	return path.join(xdg(env, "XDG_CONFIG_HOME", ".config"), "omp-services");
}

/** The plugin's own tree (this file lives in <root>/src). */
export function pluginRoot(): string {
	return path.resolve(import.meta.dir, "..");
}
