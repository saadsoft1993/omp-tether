// omp-services: the in-session face of the omp-services command line. It registers one slash command,
// `/omp-services`, with two subcommands:
//
//   /omp-services doctor   runs `omp-services doctor --json` and shows what is wrong, in words
//   /omp-services setup    runs `omp-services init --plan --json`, shows the complete plan, and only after you confirm
//                          runs `omp-services init --yes --expect-plan <digest>`: the same plan, or nothing
//
// Nothing else: no events, no background work, no network, no files written by this extension. Everything real happens
// in the command line tool, which is a separate, bounded child process (no shell, a hard deadline, capped output).
// Installing this extension changes nothing; it only does something when you type the command.
//
// The entry file name is the extension id (`extension-module:omp-services`): keep it stable. The file has no runtime
// dependencies; it imports only node:* modules and (type-only) omp itself, all by string-literal specifiers.
//
// A throw inside a callback that omp does not wrap would be an uncaught exception, which omp treats as fatal for the
// whole session, so every callback handed to a raw Node API goes through `guarded`, and the command handler catches
// everything. tests/unit/crash-safety.test.ts audits this.
import * as childProcess from "node:child_process";
import * as fs from "node:fs";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

export const PLUGIN_NAME = "omp-services";
export const PLUGIN_VERSION = "0.1.1";
export const EXTENSION_ID = `extension-module:${path.basename(import.meta.path, path.extname(import.meta.path))}`;

const DOCTOR_TIMEOUT_MS = 90_000;
const PLAN_TIMEOUT_MS = 60_000;
const INIT_TIMEOUT_MS = 300_000;
const MAX_OUTPUT_BYTES = 1024 * 1024;
const MAX_NOTICE_CHARS = 3500;
const MAX_DIALOG_CHARS = 6000; // a plan longer than this is never cut to fit: it is read in a terminal instead
const USAGE = "usage: /omp-services doctor | setup";

export interface Finding {
	id: string;
	level: "ok" | "info" | "warn" | "fail";
	message: string;
	hint?: string;
}

export interface RunResult {
	code: number | null;
	stdout: string;
	stderr: string;
	failed?: string;
}

/** Text of any thrown value; never throws itself. */
export function errorText(error: unknown): string {
	try {
		if (error instanceof Error) return String(error.message).slice(0, 300);
		return String(error).slice(0, 300);
	} catch {
		return "unprintable error";
	}
}

/** Wraps a callback handed to a raw Node API so that a throw is swallowed instead of reaching `uncaughtException`. */
function guarded<A extends unknown[]>(fn: (...args: A) => void): (...args: A) => void {
	return (...args: A) => {
		try {
			fn(...args);
		} catch {}
	};
}

/** Where the command line tool is: OMP_SERVICES_BIN (absolute), else bin/omp-services next to this plugin's src/. */
export function locateCli(env: NodeJS.ProcessEnv = process.env, here: string = import.meta.path): string | undefined {
	const candidates: string[] = [];
	const forced = env.OMP_SERVICES_BIN;
	if (forced && path.isAbsolute(forced)) candidates.push(forced);
	candidates.push(path.resolve(path.dirname(here), "..", "bin", "omp-services"));
	for (const candidate of candidates) {
		try {
			fs.accessSync(candidate, fs.constants.X_OK);
			if (fs.statSync(candidate).isFile()) return candidate;
		} catch {}
	}
	return undefined;
}

/** How long a command line that ignores SIGTERM gets after its deadline before it is killed outright. */
export const KILL_GRACE_MS = 5000;

/**
 * Runs the tool without a shell, with a deadline, and never rejects. The child never sees BUN_BE_BUN. At the deadline it
 * gets SIGTERM (its handler stops the commands its checks started); a child that ignores that is sent SIGKILL
 * `killGraceMs` later, so the promise settles whatever the child does.
 */
export function runCli(
	cli: string,
	args: string[],
	timeoutMs: number,
	env: NodeJS.ProcessEnv = process.env,
	killGraceMs: number = KILL_GRACE_MS,
): Promise<RunResult> {
	const { promise, resolve } = Promise.withResolvers<RunResult>();
	const clean: NodeJS.ProcessEnv = { ...env };
	delete clean.BUN_BE_BUN;
	try {
		const child = childProcess.execFile(
			cli,
			args,
			{ timeout: timeoutMs, maxBuffer: MAX_OUTPUT_BYTES, env: clean, windowsHide: true, killSignal: "SIGTERM" },
			guarded((error: (Error & { code?: number | string; killed?: boolean }) | null, stdout: string | Buffer, stderr: string | Buffer) => {
				try {
					const out = String(stdout ?? "");
					const err = String(stderr ?? "");
					if (!error) return resolve({ code: 0, stdout: out, stderr: err });
					if (error.killed) return resolve({ code: null, stdout: out, stderr: err, failed: `did not finish within ${Math.round(timeoutMs / 1000)} s` });
					if (typeof error.code === "number") return resolve({ code: error.code, stdout: out, stderr: err });
					resolve({ code: null, stdout: out, stderr: err, failed: errorText(error) });
				} catch (inner) {
					resolve({ code: null, stdout: "", stderr: "", failed: errorText(inner) });
				}
			}),
		);
		const hardStop = AbortSignal.timeout(timeoutMs + killGraceMs);
		hardStop.addEventListener(
			"abort",
			guarded(() => {
				try {
					child.kill("SIGKILL");
				} catch {}
			}),
			{ once: true },
		);
	} catch (error) {
		resolve({ code: null, stdout: "", stderr: "", failed: errorText(error) });
	}
	return promise;
}

/** The JSON of `init --plan --json`: the whole plan text, its digest, and whether anything would change. */
export function parsePlan(stdout: string): { plan: string; digest: string; changes: boolean } | undefined {
	try {
		const data = JSON.parse(stdout);
		if (typeof data?.plan !== "string" || typeof data?.digest !== "string" || !/^[0-9a-f]{64}$/.test(data.digest) || typeof data?.changes !== "boolean") return undefined;
		return { plan: data.plan, digest: data.digest, changes: data.changes };
	} catch {
		return undefined;
	}
}

/** The doctor's JSON as lines for a notice: failures first, then warnings, then information; "ok" lines are counted only. */
export function formatDoctor(stdout: string): { text: string; level: "info" | "warning" | "error" } {
	let data: { version?: string; summary?: Record<string, number>; findings?: Finding[] };
	try {
		data = JSON.parse(stdout);
	} catch {
		return { text: "omp-services doctor printed something that is not JSON.", level: "error" };
	}
	const findings = Array.isArray(data.findings) ? data.findings : [];
	const order = { fail: 0, warn: 1, info: 2, ok: 3 } as const;
	const shown = findings.filter(f => f && f.level !== "ok").sort((a, b) => (order[a.level] ?? 9) - (order[b.level] ?? 9));
	const counts = data.summary ?? {};
	const lines = [`omp-services doctor ${data.version ?? ""}: ${counts.ok ?? 0} ok, ${counts.info ?? 0} info, ${counts.warn ?? 0} warning(s), ${counts.fail ?? 0} failure(s)`];
	for (const f of shown) {
		lines.push(`${f.level} ${String(f.id)}: ${String(f.message)}`);
		if (f.hint) lines.push(`    -> ${String(f.hint)}`);
	}
	if (shown.length === 0) lines.push("Nothing to fix.");
	const level = (counts.fail ?? 0) > 0 ? "error" : (counts.warn ?? 0) > 0 ? "warning" : "info";
	return { text: clip(lines.join("\n")), level };
}

function clip(text: string, limit = MAX_NOTICE_CHARS): string {
	return text.length <= limit ? text : `${text.slice(0, limit - 1)}…`;
}

export default async function ompServices(pi: ExtensionAPI): Promise<void> {
	let busy = false;

	pi.registerCommand("omp-services", {
		description: "omp-services: doctor (check the services) | setup (show the plan, ask, then install)",
		getArgumentCompletions: prefix =>
			["doctor", "setup"].filter(v => v.startsWith(prefix.trim())).map(v => ({ value: v, label: v })),
		handler: async (args: string, c: ExtensionContext) => {
			const say = (message: string, type: "info" | "warning" | "error" = "info"): void => {
				try {
					c.ui.notify(message, type);
				} catch {}
			};
			try {
				const sub = args.trim().toLowerCase();
				if (sub !== "doctor" && sub !== "setup") return say(USAGE, sub === "" ? "info" : "warning");
				if (busy) return say("omp-services: another /omp-services command is still running", "warning");
				busy = true;
				try {
					const cli = locateCli();
					if (!cli) {
						return say(
							"omp-services: cannot find the command line tool (bin/omp-services next to this plugin; set OMP_SERVICES_BIN to an absolute path to use another)",
							"error",
						);
					}
					if (sub === "doctor") {
						const run = await runCli(cli, ["doctor", "--json"], DOCTOR_TIMEOUT_MS);
						if (run.failed) return say(`omp-services doctor: ${run.failed}`, "error");
						if (run.code !== 0 && run.code !== 1) return say(`omp-services doctor exited with status ${run.code}: ${clip(run.stderr.trim() || run.stdout.trim(), 600)}`, "error");
						const shown = formatDoctor(run.stdout);
						return say(shown.text, shown.level);
					}
					// setup: show the complete plan, ask, and apply exactly that plan (bound by its digest), nothing else
					if (!c.hasUI) return say("omp-services setup needs an interactive session (a confirmation); run `omp-services init` in a terminal instead", "warning");
					const plan = await runCli(cli, ["init", "--plan", "--json"], PLAN_TIMEOUT_MS);
					if (plan.failed || plan.code !== 0) return say(`omp-services setup: cannot build the plan: ${clip(plan.failed ?? (plan.stderr.trim() || plan.stdout.trim()), 800)}`, "error");
					const parsed = parsePlan(plan.stdout);
					if (!parsed) return say("omp-services setup: the plan could not be read; nothing was changed", "error");
					if (!parsed.changes) return say(clip(parsed.plan.trim()), "info");
					if (parsed.plan.length > MAX_DIALOG_CHARS) {
						return say(`omp-services setup: the plan is ${parsed.plan.length} characters, too long to confirm in a dialog without cutting it. Nothing was changed. Run \`omp-services init\` in a terminal to read all of it and answer there.`, "warning");
					}
					const yes = await c.ui.confirm("omp-services setup", `${parsed.plan.trim()}\n\nApply exactly this plan?`);
					if (yes !== true) return say("omp-services setup: nothing was changed", "info");
					const applied = await runCli(cli, ["init", "--yes", "--expect-plan", parsed.digest], INIT_TIMEOUT_MS);
					if (applied.code === 5) return say("omp-services setup: the plan changed after it was shown (a configuration or drop-in file changed), so nothing was applied. Run /omp-services setup again.", "warning");
					if (applied.failed || applied.code !== 0) return say(`omp-services setup failed: ${clip(applied.failed ?? (applied.stderr.trim() || applied.stdout.trim()), 1200)}`, "error");
					return say(`omp-services setup finished. Run /omp-services doctor to check it.\n${clip(applied.stdout.trim().split("\n").slice(-6).join("\n"), 800)}`, "info");
				} finally {
					busy = false;
				}
			} catch (error) {
				say(`omp-services: ${errorText(error)}`, "error");
			}
		},
	});
}
