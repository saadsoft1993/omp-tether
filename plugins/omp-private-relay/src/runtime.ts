// The Bun the code runs on, and how to run something under omp's embedded Bun.
import * as fs from "node:fs";
import * as path from "node:path";

export const MIN_BUN = "1.4.4";

/** An explanation when `version` is too old for collab-web's build and for the relay, otherwise null. */
export function checkBun(version: string): string | null {
	return Bun.semver.order(version, MIN_BUN) < 0 ? `omp-private-relay needs Bun ${MIN_BUN} or newer, got ${version}` : null;
}

/** The environment for a child process that runs omp itself: `BUN_BE_BUN` would turn omp into a plain Bun. */
export function envForOmp(env: Record<string, string | undefined> = process.env): Record<string, string> {
	const out: Record<string, string> = {};
	for (const [k, v] of Object.entries(env)) if (k !== "BUN_BE_BUN" && v !== undefined) out[k] = v;
	return out;
}

/** The environment for a child process that should be omp's embedded Bun. Scoped to that one child. */
export function envForBun(env: Record<string, string | undefined> = process.env): Record<string, string> {
	return { ...envForOmp(env), BUN_BE_BUN: "1" };
}

export interface OmpInfo {
	bin: string;
	/** `18.18.3` from `omp --version` (`omp/18.18.3`), or null when the output is not that shape. */
	version: string | null;
}

export function ompVersionOf(output: string): string | null {
	const m = /^omp\/(\d+\.\d+\.\d+)/.exec(output.trim());
	return m ? (m[1] as string) : null;
}

/** Asks omp for its version with BUN_BE_BUN cleared (with it set, omp would print Bun's version). */
export function probeOmp(bin: string): OmpInfo {
	let version: string | null = null;
	try {
		const r = Bun.spawnSync([bin, "--version"], { stdout: "pipe", stderr: "ignore", stdin: "ignore", env: envForOmp(), timeout: 15_000 });
		if (r.exitCode === 0) version = ompVersionOf(r.stdout.toString());
	} catch {
		version = null;
	}
	return { bin, version };
}

/** `18.18.3` becomes `18.18`: web clients are built per omp minor. */
export function minorOf(version: string): string {
	return version.split(".").slice(0, 2).join(".");
}

/**
 * The omp binary to use: OMP_PRIVATE_RELAY_OMP_BIN or the configured path (both arrive as `configured`), then OMP_BIN,
 * then `omp` on PATH. Returned as an absolute path; null when there is none.
 */
export function findOmp(configured: string | null, env: Record<string, string | undefined> = process.env): string | null {
	for (const candidate of [configured, env.OMP_BIN]) {
		if (candidate && path.isAbsolute(candidate)) return candidate;
	}
	return Bun.which("omp", { PATH: env.PATH ?? "" });
}

/** Where a Tailscale install puts the command when PATH does not (a service or an ssh session has a small PATH). */
export const TAILSCALE_PLACES: readonly string[] = [
	"/usr/local/bin/tailscale",
	"/opt/homebrew/bin/tailscale",
	"/Applications/Tailscale.app/Contents/MacOS/Tailscale",
];

const isExecutableFile = (file: string): boolean => {
	try {
		if (!fs.statSync(file).isFile()) return false;
		fs.accessSync(file, fs.constants.X_OK);
		return true;
	} catch {
		return false;
	}
};

/**
 * The tailscale command. `configured` is `auth.tailscale.bin` (default `tailscale`): an absolute path is used as it is, or
 * is not found (the user named it, so nothing else is tried in its place); the bare name is looked up on PATH, then in the
 * usual places. Returned as an absolute path; null when there is none.
 */
export function findTailscale(configured: string, env: Record<string, string | undefined> = process.env, places: readonly string[] = TAILSCALE_PLACES): string | null {
	if (path.isAbsolute(configured)) return isExecutableFile(configured) ? configured : null;
	const onPath = Bun.which(configured, { PATH: env.PATH ?? "" });
	if (onPath) return onPath;
	return places.find(isExecutableFile) ?? null;
}
