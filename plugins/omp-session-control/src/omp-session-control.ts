// omp-session-control: lets a local dashboard (or any program of the same user) stop the running turn of an
// interactive omp session and rename it, over a private Unix socket. Protocol: docs/protocol/control-1.md.
//
// The entry file name is the extension id (`extension-module:omp-session-control`): keep it stable. The file has no
// runtime dependencies; it imports only node:* modules and (type-only, or dynamically and optionally) omp itself.
//
// Inert by default. Nothing is created, watched or listened on unless the control directory exists, is a private
// directory of this user, and holds an `enabled` marker file. Then, in the main session of an interactive (TUI) run
// only (never in subagents, print or RPC runs), the extension opens:
//   <dir>/<random>.sock   0600 Unix socket
//   <dir>/<nonce>.json    0600 registry entry (pid, socket, sessionId, generation, protocol versions)
//   <dir>/events.log      0600, starts/stops/errors only, capped
// <dir> is 0700; that mode is the access control (the runtime exposes no peer credentials). A file named `off` in
// <dir> closes every session's control at once and stays off until it is removed.
//
// Every callback handed to a raw Node API (sockets, fs.watch, promises) is wrapped so that a throw is logged instead of
// reaching `uncaughtException`, which omp treats as fatal for the whole session. Timers use ctx.setTimeout, which omp
// isolates and clears on shutdown. tests/unit/crash-safety.test.ts audits this.
import * as crypto from "node:crypto";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@oh-my-pi/pi-coding-agent";

export const PLUGIN_NAME = "omp-session-control";
export const PLUGIN_VERSION = "0.5.0";
/** Wire protocol versions this build speaks (docs/protocol/control-1.md). */
export const PROTOCOL_VERSIONS: readonly number[] = [1];
const PROTOCOL_VERSION = 1;
export const OPS = ["snapshot", "abort", "rename", "send-message", "send-status", "ask-review", "ask-answer"] as const;
/** Offered only when the host has ctx.getPendingPlanReview and ctx.resolvePlanReview (feature-detected). */
export const PLAN_OPS = ["plan-review", "plan-answer"] as const;
/**
 * Session commands. Each is offered only when the host has the API behind it (see supportsSessionOp), in this order,
 * after the base and plan operations.
 */
export const SESSION_OPS = ["session-info", "compact", "set-model", "set-thinking", "set-mode", "set-advisor"] as const;
type SessionOp = (typeof SESSION_OPS)[number];

const MAX_FRAME_BYTES = 64 * 1024;
const MAX_CLIENTS = 4;
const MAX_OUTSTANDING = 16;
const MAX_NAME_CHARS = 120;
const MAX_LOG_BYTES = 256 * 1024;
const MAX_LOG_RECORD_BYTES = 2048;
const MAX_DEBUG_TEXT = 200;
const MAX_REGISTRY_BYTES = 4096;
const MAX_WRITE_BACKLOG = 256 * 1024; // a client that never reads its replies is cut off
const MAX_CONNECTIONS = MAX_CLIENTS + 12; // beyond this, connections are dropped without a reply
const OP_TIMEOUT_MS = 10_000; // an operation omp does not finish by then gets a `timeout` reply
/**
 * `compact` returns once the compaction has finished, so it gets a longer deadline: a summary is a model call. A
 * `timeout` reply then means "not finished yet", not "failed": the compaction goes on, and further changes are refused
 * with `mutation_pending` until omp settles it.
 */
const COMPACT_TIMEOUT_MS = 180_000;
/**
 * omp's own `ctx.compact()` can resolve although the compaction failed, was cancelled or had nothing to compact (the
 * interactive controller swallows the outcome), so the only proof is the `session_compact` event, which omp emits after
 * the compaction entry is saved. It can be emitted a moment after compact() resolves; wait this long for it.
 */
const COMPACT_EVENT_GRACE_MS = 2_000;
const COMPACT_NOT_DONE_MESSAGE = "Compaction did not complete (cancelled, failed, or nothing to compact).";
const MAX_INSTRUCTIONS_CHARS = 2000;
const MAX_OBJECTIVE_CHARS = 2000;
const MAX_MODEL_FIELD_CHARS = 128;
const MAX_MODEL_NAME_CHARS = 80;
const MAX_MODELS = 200;
/** omp's built-in chat model roles, in the order its picker lists them; a role nobody configured resolves to nothing. */
const MODEL_ROLES = ["default", "smol", "slow", "vision", "plan", "commit", "tiny", "memory", "task", "advisor"] as const;
const MAX_MODELS_BYTES = 44 * 1024; // the models list alone; the rest of the session-info result is a few hundred bytes, so it stays inside one frame
const FRAME_DEADLINE_MS = 10_000; // a frame must be complete this long after its first byte
const RECHECK_MS = 2_000; // while there is something to wait for, look at the files again this often (see reconcile)
const CLOSE_GRACE_MS = 2_000; // a socket we have said goodbye to is destroyed after this, whatever the peer does
export const DEFAULT_IDLE_TIMEOUT_MS = 60_000;
const MIN_IDLE_TIMEOUT_MS = 100;
const MAX_IDLE_TIMEOUT_MS = 3_600_000;
export const EXTENSION_ID = `extension-module:${path.basename(import.meta.path, path.extname(import.meta.path))}`;

const MUTATING: Readonly<Record<string, boolean>> = {
	snapshot: false,
	abort: true,
	rename: true,
	"send-message": true,
	"send-status": false,
	"ask-review": false,
	"ask-answer": true,
	"plan-review": false,
	"plan-answer": true,
	"plan-notes": true,
	"session-info": false,
	compact: true,
	"set-model": true,
	"set-thinking": true,
	"set-mode": true,
	"set-advisor": true,
};
/** Operations that must name the live session and generation (reads of the plan included: a switch between reads is refused). */
const TARGET_CHECKED: Readonly<Record<string, true>> = {
	abort: true,
	rename: true,
	"send-message": true,
	"send-status": true,
	"ask-review": true,
	"ask-answer": true,
	"plan-review": true,
	"plan-answer": true,
	"plan-notes": true,
	"session-info": true,
	compact: true,
	"set-model": true,
	"set-thinking": true,
	"set-mode": true,
	"set-advisor": true,
};
const MAX_REVIEW_ID_CHARS = 128;
const MAX_REGISTRY_TITLE_CHARS = 200;
const MAX_FEEDBACK_CHARS = 8000;
const MAX_ROLE_CHARS = 64;
const NOT_ACCEPTED_MESSAGE =
	"The terminal did not accept that answer: the review was closed or replaced, or that choice is unavailable.";

// ---------------------------------------------------------------------------------------------------------------------
// Settings: environment > omp plugin settings (`omp plugin config set omp-session-control <key> <value>`) > default.
// omp's own loader applies neither the schema defaults nor the `env` fallback when an extension reads its settings, so
// both are done here.
// ---------------------------------------------------------------------------------------------------------------------

export interface Settings {
	/** Absolute control directory, or "" (control stays off). */
	controlDir: string;
	idleTimeoutMs: number;
	/** Also write the text of unexpected exceptions (bounded) to events.log. Off by default: that text can be sensitive. */
	debug: boolean;
	/** Human-readable notes about values that were ignored. */
	warnings: string[];
}

export interface SettingsInput {
	env: Readonly<Record<string, string | undefined>>;
	stored: Readonly<Record<string, unknown>>;
	home: string | undefined;
}

export function resolveSettings({ env, stored, home }: SettingsInput): Settings {
	const warnings: string[] = [];

	let controlDir = "";
	const dirSource = nonEmptyString(env.OMP_SESSION_CONTROL_DIR) ?? nonEmptyString(stored.controlDir);
	if (dirSource !== undefined) {
		if (path.isAbsolute(dirSource)) controlDir = path.resolve(dirSource);
		else warnings.push("controlDir must be an absolute path; control stays off");
	} else if (home && path.isAbsolute(home)) {
		controlDir = path.join(home, ".config", "omp-session-control");
	} else {
		warnings.push("HOME is not an absolute path and no controlDir is set; control stays off");
	}

	let idleTimeoutMs = DEFAULT_IDLE_TIMEOUT_MS;
	for (const [label, raw] of [
		["OMP_SESSION_CONTROL_IDLE_MS", env.OMP_SESSION_CONTROL_IDLE_MS],
		["idleTimeoutMs", stored.idleTimeoutMs],
	] as const) {
		if (raw === undefined || raw === null || raw === "") continue;
		const n = typeof raw === "number" ? raw : typeof raw === "string" && /^\d+$/.test(raw.trim()) ? Number(raw) : Number.NaN;
		if (Number.isInteger(n) && n >= MIN_IDLE_TIMEOUT_MS && n <= MAX_IDLE_TIMEOUT_MS) {
			idleTimeoutMs = n;
			break;
		}
		warnings.push(`${label} must be an integer between ${MIN_IDLE_TIMEOUT_MS} and ${MAX_IDLE_TIMEOUT_MS}; ignored`);
	}
	const debugRaw = env.OMP_SESSION_CONTROL_DEBUG ?? stored.debugLog;
	const debug = debugRaw === true || (typeof debugRaw === "string" && ["1", "true", "yes", "on"].includes(debugRaw.trim().toLowerCase()));
	return { controlDir, idleTimeoutMs, debug, warnings };
}

function nonEmptyString(value: unknown): string | undefined {
	return typeof value === "string" && value.trim() !== "" ? value.trim() : undefined;
}

// ---------------------------------------------------------------------------------------------------------------------
// Names
// ---------------------------------------------------------------------------------------------------------------------

export class ControlError extends Error {
	constructor(
		readonly code: string,
		message: string,
		readonly detail?: unknown,
	) {
		super(message);
	}
}

/**
 * A category for any thrown value: our own error code, the system error code (EACCES, ...), or the error's class name.
 * Never the message, and never throws. This is what events.log records by default.
 */
export function errorKind(error: unknown): string {
	try {
		if (error instanceof ControlError) return error.code;
		const code = (error as { code?: unknown } | null | undefined)?.code;
		if (typeof code === "string" && /^[A-Z][A-Z0-9_]{1,30}$/.test(code)) return code;
		const name = error instanceof Error ? error.name : typeof error;
		return /^[A-Za-z][A-Za-z0-9_.]{0,40}$/.test(name) ? name : "error";
	} catch {
		return "unprintable";
	}
}

/** Text of any thrown value; never throws itself (a thrown object may have a hostile `toString` or `message`). */
export function errorText(error: unknown): string {
	try {
		return error instanceof Error ? error.message : String(error);
	} catch {
		return "unprintable error";
	}
}

/** Trimmed name, or an error: 1-120 characters; no control characters (C0, DEL, C1, line/paragraph separators)
 * and no invisible format characters (Unicode Cf: bidi overrides, zero-width marks), which could disguise a name. */
export function cleanName(value: unknown): string {
	if (typeof value !== "string") throw new ControlError("invalid_args", "args.name must be a string");
	const name = value.replace(/ +/g, " ").trim(); // omp stores titles with runs of spaces collapsed
	const chars = [...name].length;
	if (chars < 1 || chars > MAX_NAME_CHARS) {
		throw new ControlError("invalid_args", `the name must be 1-${MAX_NAME_CHARS} characters`);
	}
	if (/[\u0000-\u001f\u007f-\u009f\u2028\u2029\p{Cf}]/u.test(name)) {
		throw new ControlError("invalid_args", "the name must not contain control or invisible characters");
	}
	return name;
}

// ---------------------------------------------------------------------------------------------------------------------
// Files
// ---------------------------------------------------------------------------------------------------------------------

type DirState = { kind: "missing" } | { kind: "unsafe"; message: string } | { kind: "ok"; mode: number };

/** Looks at the control directory without creating or changing anything. */
function inspectDir(dir: string): DirState {
	let st: fs.Stats;
	try {
		st = fs.lstatSync(dir);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === "ENOENT") return { kind: "missing" };
		return { kind: "unsafe", message: `${dir} cannot be inspected (${(error as NodeJS.ErrnoException).code ?? "error"})` };
	}
	if (st.isSymbolicLink() || !st.isDirectory()) return { kind: "unsafe", message: `${dir} is not a real directory` };
	if (typeof process.getuid === "function" && st.uid !== process.getuid()) {
		return { kind: "unsafe", message: `${dir} is owned by another user` };
	}
	if ((st.mode & 0o022) !== 0) return { kind: "unsafe", message: `${dir} is writable by other users; run chmod 700 on it` };
	return { kind: "ok", mode: st.mode & 0o777 };
}

/**
 * Verify (and, with `create`, make) a private directory: a real directory, ours, not writable by others, mode 0700.
 * The checks and the mode change go through one open directory handle, so a swap of the path for a symlink between
 * the check and the change cannot make us chmod something else. Returns the directory's identity.
 */
function ensurePrivateDir(dir: string, create: boolean): fs.Stats {
	if (!dir) throw new ControlError("unsafe_path", "no absolute control directory");
	if (create) fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
	let fd: number;
	try {
		fd = fs.openSync(dir, fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW);
	} catch (error) {
		const code = (error as NodeJS.ErrnoException).code;
		if (code === "ENOENT") throw new ControlError("unsafe_path", `${dir} does not exist`);
		if (code === "ELOOP" || code === "ENOTDIR") throw new ControlError("unsafe_path", `${dir} is not a real directory`);
		throw new ControlError("unsafe_path", `${dir} cannot be opened (${code ?? "error"})`);
	}
	try {
		const st = fs.fstatSync(fd);
		if (typeof process.getuid === "function" && st.uid !== process.getuid()) {
			throw new ControlError("unsafe_path", `${dir} is owned by another user`);
		}
		if ((st.mode & 0o022) !== 0) throw new ControlError("unsafe_path", `${dir} is writable by other users; run chmod 700 on it`);
		if ((st.mode & 0o777) !== 0o700) fs.fchmodSync(fd, 0o700);
		return fs.fstatSync(fd);
	} finally {
		fs.closeSync(fd);
	}
}

/** True when `file` exists in any form; fails closed (true) on an unexpected error. Never follows a symlink. */
function existsNoFollow(file: string): boolean {
	try {
		fs.lstatSync(file);
		return true;
	} catch (error) {
		return (error as NodeJS.ErrnoException).code !== "ENOENT";
	}
}

/** The `enabled` marker must be a plain file: a symlink does not count. */
function isRegularFile(file: string): boolean {
	try {
		return fs.lstatSync(file).isFile();
	} catch {
		return false;
	}
}

/**
 * Creates `file` as an empty private regular file if it is absent. Refuses anything else that is already there: opening
 * a FIFO for writing blocks until someone reads it, and a blocked synchronous call cannot be rescued from inside omp.
 */
function touchPrivateFile(file: string): void {
	try {
		if (!fs.lstatSync(file).isFile()) throw new ControlError("unsafe_path", `${file} is not a regular file`);
	} catch (error) {
		if (error instanceof ControlError) throw error;
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
	}
	const fd = fs.openSync(file, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK, 0o600);
	try {
		if (!fs.fstatSync(fd).isFile()) throw new ControlError("unsafe_path", `${file} is not a regular file`);
	} finally {
		fs.closeSync(fd);
	}
}

/** Reads a small plain file without ever blocking; undefined for anything else (FIFO, device, symlink, too big). */
function readSmallFile(file: string, maxBytes: number): string | undefined {
	try {
		if (!fs.lstatSync(file).isFile()) return undefined;
		const fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
		try {
			const st = fs.fstatSync(fd);
			if (!st.isFile() || st.size > maxBytes) return undefined;
			return fs.readFileSync(fd, "utf8");
		} finally {
			fs.closeSync(fd);
		}
	} catch {
		return undefined;
	}
}

function removeIfPresent(file: string): void {
	try {
		fs.unlinkSync(file);
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
	}
}

function pidAlive(pid: number): boolean {
	try {
		process.kill(pid, 0);
		return true;
	} catch (error) {
		return (error as NodeJS.ErrnoException).code === "EPERM";
	}
}

function writeFileAtomic(target: string, content: string): void {
	const tmp = `${target}.${crypto.randomBytes(6).toString("hex")}.tmp`;
	const fd = fs.openSync(
		tmp,
		fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW,
		0o600,
	);
	try {
		try {
			fs.writeSync(fd, content);
			fs.fsyncSync(fd);
		} finally {
			fs.closeSync(fd);
		}
		fs.renameSync(tmp, target); // replaces a symlink at `target` instead of following it
	} catch (error) {
		try {
			fs.unlinkSync(tmp);
		} catch {}
		throw error;
	}
}

/** Longest Unix socket path the platform accepts, in bytes. */
function maxSocketPathBytes(): number {
	return process.platform === "darwin" ? 103 : 107;
}

const UTF8 = new TextDecoder("utf-8", { fatal: true });
const CHURN_FILE = /(?:\.json|\.sock|\.tmp)$|^events\.log$/;

// ---------------------------------------------------------------------------------------------------------------------
// The extension
// ---------------------------------------------------------------------------------------------------------------------

interface Request {
	id: string | number;
	sessionId: string | null;
	generation: number | null;
	op: string;
	args: Record<string, unknown>;
}

interface Client {
	sock: net.Socket;
	buffer: Buffer;
	idle: Timer | undefined;
	/** Deadline for the frame being received; set while a partial frame sits in `buffer`. */
	frame: Timer | undefined;
	dead: boolean;
	/** Requests accepted from this client and not answered yet: a client waiting for a slow one is not idle. */
	pending: number;
	reserved: boolean;
}

type Activation =
	| { state: "ready" }
	| { state: "inert"; reason: string }
	| { state: "off"; reason: string }
	| { state: "unsafe"; reason: string };

/**
 * Runs `load`, which imports an omp-internal module, and returns undefined when that fails: without the module the
 * extension still works, minus one feature. The imports below are dynamic on purpose (a static import would stop the
 * whole extension from loading when omp renames or removes the module) and their specifiers must stay string literals:
 * omp resolves its own modules for extensions at load time, and a specifier held in a variable is not resolved.
 */
async function optional<T>(load: () => Promise<T>): Promise<T | undefined> {
	try {
		return await load();
	} catch {
		return undefined;
	}
}

const loadPluginLoader = () => import("@oh-my-pi/pi-coding-agent/extensibility/plugins/loader");
const loadSettingsRegistry = () => import("@oh-my-pi/pi-coding-agent/config/registry");
const loadRuntimeVersion = () => import("@oh-my-pi/pi-utils/dirs");

// ---------------------------------------------------------------------------------------------------------------------
// Plan review (pure helpers). The shapes mirror omp's PlanReviewRequestedEvent; everything from the host is validated
// before it is kept, because it ends up in the registry and in replies.
// ---------------------------------------------------------------------------------------------------------------------

const PLAN_CHOICES = ["execute", "compact", "keep", "refine", "save"] as const;
type PlanChoice = (typeof PLAN_CHOICES)[number];
const APPROVALS: Readonly<Record<string, true>> = { execute: true, compact: true, keep: true };

interface PlanOption {
	id: PlanChoice;
	label: string;
	disabled: boolean;
}
interface PlanTier {
	role: string;
	provider: string;
	model: string;
	thinkingLevel?: string;
}
interface PlanReview {
	reviewId: string;
	sessionId: string;
	title: string;
	planFilePath: string;
	planContent: string;
	options: PlanOption[];
	tiers: PlanTier[] | null;
	/** When this plugin first saw the review (ISO-8601 UTC). */
	openedAt: string;
}

/** The two host methods, looked up on the context at the time of use: an older omp lacks them. */
interface PlanHost {
	getPendingPlanReview(): unknown;
	resolvePlanReview(reviewId: string, choice: string, input?: { feedback?: string; role?: string }): boolean;
}

function planHost(c: unknown): PlanHost | undefined {
	const host = c as Partial<PlanHost> | null | undefined;
	return host && typeof host.getPendingPlanReview === "function" && typeof host.resolvePlanReview === "function" ? (host as PlanHost) : undefined;
}

const isHigh = (unit: number): boolean => unit >= 0xd800 && unit <= 0xdbff;
const isLow = (unit: number): boolean => unit >= 0xdc00 && unit <= 0xdfff;
/** A review from the host, or undefined when it is not usable (wrong shape, an id the protocol cannot carry back). */
function readReview(value: unknown): Omit<PlanReview, "openedAt"> | undefined {
	if (!value || typeof value !== "object") return undefined;
	const v = value as Record<string, unknown>;
	if (typeof v.reviewId !== "string" || v.reviewId === "" || v.reviewId.length > MAX_REVIEW_ID_CHARS) return undefined;
	if (typeof v.sessionId !== "string" || typeof v.planContent !== "string" || !Array.isArray(v.options) || v.options.length > 8) return undefined;
	const options: PlanOption[] = [];
	for (const o of v.options as unknown[]) {
		if (!o || typeof o !== "object") return undefined;
		const { id, label, disabled } = o as Record<string, unknown>;
		if (!PLAN_CHOICES.includes(id as PlanChoice) || typeof label !== "string" || typeof disabled !== "boolean") return undefined;
		options.push({ id: id as PlanChoice, label, disabled });
	}
	let tiers: PlanTier[] | null = null;
	if (Array.isArray(v.tiers) && v.tiers.length <= 16) {
		const list: PlanTier[] = [];
		for (const t of v.tiers as unknown[]) {
			const { role, provider, model, thinkingLevel } = (t ?? {}) as Record<string, unknown>;
			if (typeof role !== "string" || typeof provider !== "string" || typeof model !== "string") {
				list.length = 0;
				break;
			}
			list.push({ role, provider, model, ...(typeof thinkingLevel === "string" ? { thinkingLevel } : {}) });
		}
		tiers = list.length > 0 ? list : null;
	}
	return {
		reviewId: v.reviewId,
		sessionId: v.sessionId,
		title: typeof v.title === "string" ? v.title : "",
		planFilePath: typeof v.planFilePath === "string" ? v.planFilePath : "",
		planContent: v.planContent,
		options,
		tiers,
	};
}

/** The longest prefix of `title` (at most 200 UTF-16 units, never splitting a surrogate pair) for which `fits` holds; undefined when not even "" fits. */
export function fitTitle(title: string, fits: (t: string) => boolean): string | undefined {
	if (!fits("")) return undefined;
	// Limits are counted in UTF-16 units (JS .length), like every other limit; whole code points only, never half a pair.
	const points: string[] = [];
	let units = 0;
	for (const point of title) {
		units += point.length;
		if (units > MAX_REGISTRY_TITLE_CHARS) break;
		points.push(point);
	}
	let lo = 0;
	let hi = points.length;
	while (lo < hi) {
		const mid = Math.ceil((lo + hi) / 2);
		if (fits(points.slice(0, mid).join(""))) lo = mid;
		else hi = mid - 1;
	}
	return points.slice(0, lo).join("");
}

const isSessionOp = (op: string): op is SessionOp => (SESSION_OPS as readonly string[]).includes(op);
const isFn = (value: unknown): boolean => typeof value === "function";

/** What the host offers, looked up at the time of use: an older omp lacks some of it. */
interface HostSurface {
	models?: { list?: unknown; current?: unknown };
	sessionModes?: { set?: unknown; getState?: unknown };
	advisor?: { getState?: unknown; setEnabled?: unknown; setModel?: unknown };
	compact?: unknown;
}

function supportsSessionOp(op: SessionOp, c: unknown, pi?: unknown): boolean {
	const host = (c ?? {}) as HostSurface;
	const api = (pi ?? {}) as Record<string, unknown>;
	switch (op) {
		case "session-info":
			return isFn(host.models?.list) || isFn(host.sessionModes?.getState) || isFn(api.getThinkingLevel);
		case "compact":
			return isFn(host.compact);
		case "set-model":
			return isFn(api.setModel) && isFn(host.models?.list);
		case "set-thinking":
			return isFn(api.setThinkingLevel) && isFn(api.getThinkingLevel);
		case "set-mode":
			return isFn(host.sessionModes?.set);
		case "set-advisor":
			return isFn(host.advisor?.getState) && isFn(host.advisor?.setEnabled) && isFn(host.advisor?.setModel) && isFn(host.models?.list);
	}
}

interface ModelRef {
	provider: string;
	id: string;
	name?: string;
}

/** A model from the host as provider, id and a bounded name; undefined when it has no usable provider and id. */
function readModel(value: unknown, withName: boolean): ModelRef | undefined {
	if (!value || typeof value !== "object") return undefined;
	const m = value as Record<string, unknown>;
	if (typeof m.provider !== "string" || typeof m.id !== "string") return undefined;
	if (m.provider === "" || m.id === "" || m.provider.length > MAX_MODEL_FIELD_CHARS || m.id.length > MAX_MODEL_FIELD_CHARS) return undefined;
	const out: ModelRef = { provider: m.provider, id: m.id };
	if (withName && typeof m.name === "string" && m.name !== "") out.name = m.name.slice(0, MAX_MODEL_NAME_CHARS);
	return out;
}

/** The thinking levels a model can be set to: off plus its efforts; none for a model without reasoning. */
function thinkingLevelsOf(model: unknown): string[] {
	if (!model || typeof model !== "object") return [];
	const m = model as { reasoning?: unknown; thinking?: { efforts?: unknown } };
	if (m.reasoning !== true) return [];
	const efforts = Array.isArray(m.thinking?.efforts) ? (m.thinking?.efforts as unknown[]).filter((e): e is string => typeof e === "string" && e !== "") : [];
	return ["off", ...efforts.filter(e => e !== "off")];
}

const MODE_STATUS_KEYS = ["plan", "vibe", "goal", "loop"] as const;
/** Allowed mode/action pairs. Loop can only be switched off remotely: starting one needs a prompt and a limit. */
const MODE_ACTIONS: Readonly<Record<string, readonly string[]>> = {
	plan: ["enter", "exit"],
	vibe: ["enter", "exit"],
	goal: ["enter", "pause", "resume", "drop"],
	loop: ["exit"],
};

export default async function ompSessionControl(pi: ExtensionAPI): Promise<void> {
	// -- settings -----------------------------------------------------------------------------------------------------
	const home = process.env.HOME || os.homedir();
	let stored: Record<string, unknown> = {};
	try {
		// omp merges project-level plugin overrides (a repository's .omp folder) over the user's settings for the
		// directory it is given. Ask about the home directory, so that a repository cannot move the control directory.
		const loader = await optional(loadPluginLoader);
		const result = await loader?.getPluginSettings(PLUGIN_NAME, home);
		if (result && typeof result === "object") stored = result as Record<string, unknown>;
	} catch {}
	const settings = resolveSettings({ env: process.env, stored, home });
	const runtimeVersion = await optional(async () => (await loadRuntimeVersion()).VERSION);
	const matchVersion = typeof runtimeVersion === "string" ? runtimeVersion.match(/^(\d+)\.(\d+)\./) : null;
	const planNotesSupported = !!matchVersion && (Number(matchVersion[1]) > 18 || (Number(matchVersion[1]) === 18 && Number(matchVersion[2]) >= 19));
	const CONTROL_DIR = settings.controlDir;
	const OFF_FILE = path.join(CONTROL_DIR, "off");
	const ENABLED_FILE = path.join(CONTROL_DIR, "enabled");
	const EVENT_LOG = path.join(CONTROL_DIR, "events.log");
	const IDLE_TIMEOUT_MS = settings.idleTimeoutMs;
	const SUPPORTED = process.platform !== "win32";

	let dirOk = false; // set once the directory passed ensurePrivateDir; nothing is written there before

	/**
	 * Starts, stops and errors only, as categories (see errorKind); the text of an unexpected exception is added, bounded,
	 * only with the debug setting. Capped, never follows a symlink, never opens anything but a plain file (a FIFO would
	 * block the session), only inside a checked directory. Never throws.
	 */
	const logEvent = (kind: string, data: Record<string, unknown> = {}): void => {
		if (!dirOk || !CONTROL_DIR) return;
		try {
			try {
				if (!fs.lstatSync(EVENT_LOG).isFile()) return;
			} catch (error) {
				if ((error as NodeJS.ErrnoException).code !== "ENOENT") return;
			}
			const fd = fs.openSync(
				EVENT_LOG,
				fs.constants.O_WRONLY | fs.constants.O_APPEND | fs.constants.O_CREAT | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK,
				0o600,
			);
			try {
				const st = fs.fstatSync(fd);
				if (!st.isFile() || (typeof process.getuid === "function" && st.uid !== process.getuid())) return;
				if ((st.mode & 0o777) !== 0o600) fs.fchmodSync(fd, 0o600);
				let line = `${JSON.stringify({ ts: new Date().toISOString(), pid: process.pid, kind, ...data })}\n`;
				if (Buffer.byteLength(line) > MAX_LOG_RECORD_BYTES) line = `${JSON.stringify({ ts: new Date().toISOString(), pid: process.pid, kind, truncated: true })}\n`;
				if (st.size + Buffer.byteLength(line) > MAX_LOG_BYTES) fs.ftruncateSync(fd, 0);
				fs.writeSync(fd, line);
			} finally {
				fs.closeSync(fd);
			}
		} catch {}
	};

	/** What to log about a failure: the category, plus bounded text only when the debug setting is on. */
	const failure = (error: unknown): Record<string, unknown> => {
		const out: Record<string, unknown> = { error: errorKind(error) };
		if (settings.debug) out.text = errorText(error).slice(0, MAX_DEBUG_TEXT);
		return out;
	};

	/** Short text for the person at the terminal: our own messages as they are, anything else as its category. */
	const userError = (error: unknown): string => (error instanceof ControlError ? error.message : errorKind(error));

	/** Runs `fn` and logs instead of throwing: nothing in here may take the session down. */
	const guarded = <A extends unknown[], R = void>(label: string, fn: (...args: A) => R): ((...args: A) => R | undefined) => {
		return (...args: A) => {
			try {
				return fn(...args);
			} catch (error) {
				logEvent("handler_error", { label, ...failure(error) });
				return undefined;
			}
		};
	};

	/** The same for a promise nobody awaits. */
	const guardedAsync = (label: string, fn: () => Promise<void>): Promise<void> =>
		fn().catch(error => logEvent("handler_error", { label, ...failure(error) }));

	let ctx: ExtensionContext | undefined;
	let server: net.Server | undefined;
	let nonce = "";
	let socketPath = "";
	let registryPath = "";
	let generation = 0;
	let outstanding = 0;
	let mutationChain: Promise<void> = Promise.resolve();
	let dirWatcher: fs.FSWatcher | undefined;
	let selfDisabled = false;
	let stopped = false;
	const notified = new Set<string>();
	let lastFailure = "";
	/** True once control has been listening in this session; the directory is then worth waiting for. */
	let everActive = false;
	let parentWatcher: fs.FSWatcher | undefined;
	/** The one pending periodic recheck, if any. */
	let recheckTimer: Timer | undefined;
	/** "" when the live `disabledExtensions` watch works, else why not. */
	let liveDisableProblem = "";
	/** A host mutation (rename, abort) that was started and has not settled yet, even if its request timed out. */
	let hostMutationRunning = false;
	/** A compaction is in progress in omp (started by `compact`, not yet settled): other changes are refused at once, not queued behind it. */
	let compactionRunning = false;
	/**
	 * A replacement of the session (switch, new, fork, branch) that we allowed may be committing. omp gives an extension
	 * no way to ask, hooks get up to 30 s each and run one after the other, then come the abort and a flush, so a
	 * replacement can take arbitrarily long and no timer can say it is over. The hold therefore fails closed and ends only
	 * on a session lifecycle event: session_switch, session_branch, session_start or session_shutdown.
	 * Turn events are NOT evidence that a replacement was cancelled: fork and branch-into-new-session never disconnect the
	 * agent (agent-session.ts ~11927-12017, ~13969-14075), so the old loop can still emit turn_start while a successful
	 * replacement is pending. The cost: when another extension's hook cancels the replacement omp emits nothing, and
	 * the hold stays until the session restarts.
	 */
	let replacing = false;
	const clearReplacing = (): void => {
		replacing = false;
	};
	const markReplacing = (): void => {
		replacing = true;
	};
	const REPLACING_MESSAGE =
		"Session is switching. If a switch was cancelled by another extension, restart the session to re-enable remote changes.";
	/** An omp-started compaction (auto-compaction) is in progress, from its start and end events. */
	let autoCompacting = false;
	/** `session_compact` events seen, and the waiters that want the next one. */
	let compactionsSeen = 0;
	const compactionWaiters = new Set<() => void>();
	const isCompacting = (): boolean => compactionRunning || autoCompacting;
	/** True once the socket is bound and the registry published; the registry is never written before. */
	let listening = false;
	/** Counts listener starts; requests accepted by an earlier listener never touch the current one's counters. */
	let listenerEpoch = 0;
	/** Identity of the control directory this listener was started in (device and inode). */
	let dirIdentity = "";
	let unsubscribeDisabled: (() => void) | undefined;
	const clients = new Set<Client>();
	/** The open plan review, as far as this plugin knows; the host's getPendingPlanReview is the authority (see reconcilePlan). */
	let planReview: PlanReview | undefined;
	let askReview: { askId: string; openedAt: string; args: Record<string, unknown>; tooLarge: boolean } | undefined;
	type Delivery = { state: "pending" | "admitted" | "failed"; reason?: string };
	type AskGuard = { id: string; state: Delivery["state"] };
	const deliveredMessages = new Map<string, { at: number; askId?: string; text: string; result: Delivery; admit: () => void; onSettled?: (result: Delivery) => void; answer?: AskGuard }>();
	const askAnswers = new Map<string, AskGuard>();
	const pendingApprovals = new Map<string, Set<symbol>>();
	const executeApprovals = new Map<symbol, boolean>();
	type PlanNotesState = { requestId: string; reviewId: string; choice: string; approved: boolean; path: string; notes: string; notesDigest: string; state: "pending" | "sent" | "failed" | "uncertain"; at: string; reason?: string; timer?: Timer; promptSessionId?: string; delivering?: boolean; expectedSessionId: string; switches: number; expectFirst: boolean; epoch: number; approvalToken?: symbol };
	let planNotes: PlanNotesState | undefined;
	const planNotesReceipts = new Map<string, PlanNotesState>();
	const failPlanNotes = (reason: string): void => {
		if (!planNotes || planNotes.state !== "pending" || planNotes.delivering) return;
		planNotes.state = "failed";
		planNotes.reason = reason;
		planNotes.at = new Date().toISOString();
		planNotes.notes = "";
		if (planNotes.timer !== undefined) ctx?.clearTimer(planNotes.timer);
	};

	const isMainTui = (c: ExtensionContext): boolean => c.mode === "tui" && c.agent.kind === "main";
	const currentSessionId = (): string => ctx?.sessionManager.getSessionId() ?? "";

	const notifyOnce = (message: string, key = message): void => {
		if (notified.has(key) || !ctx) return;
		notified.add(key);
		try {
			ctx.ui.notify(`session-control: ${message}`, "warning");
		} catch {}
	};

	// -- activation ---------------------------------------------------------------------------------------------------

	const activation = (): Activation => {
		if (!SUPPORTED) return { state: "inert", reason: "Windows is not supported" };
		if (!CONTROL_DIR) return { state: "inert", reason: settings.warnings[0] ?? "no control directory configured" };
		if (selfDisabled) return { state: "off", reason: `${EXTENSION_ID} is in disabledExtensions` };
		const dir = inspectDir(CONTROL_DIR);
		if (dir.kind === "missing") return { state: "inert", reason: `${CONTROL_DIR} does not exist` };
		if (dir.kind === "unsafe") return { state: "unsafe", reason: dir.message };
		if (!isRegularFile(ENABLED_FILE)) return { state: "inert", reason: `no enabled marker in ${CONTROL_DIR}` };
		if (existsNoFollow(OFF_FILE)) return { state: "off", reason: `${OFF_FILE} exists` };
		return { state: "ready" };
	};

	const off = (): boolean => activation().state !== "ready";

	// -- registry and sockets -----------------------------------------------------------------------------------------

	/** Registries (and their sockets) left by sessions that crashed or were SIGKILLed. */
	const removeStaleEntries = (): void => {
		for (const name of fs.readdirSync(CONTROL_DIR)) {
			if (!name.endsWith(".json")) continue;
			const file = path.join(CONTROL_DIR, name);
			try {
				const text = readSmallFile(file, MAX_REGISTRY_BYTES);
				if (text === undefined) continue;
				const reg = JSON.parse(text) as { pid?: unknown; socket?: unknown };
				if (typeof reg.pid !== "number" || reg.pid === process.pid || pidAlive(reg.pid)) continue;
				if (typeof reg.socket === "string" && path.dirname(reg.socket) === CONTROL_DIR) {
					try {
						if (fs.lstatSync(reg.socket).isSocket()) fs.unlinkSync(reg.socket);
					} catch {}
				}
				fs.unlinkSync(file);
				logEvent("removed_stale", { stalePid: reg.pid });
			} catch {}
		}
	};

	const opsFor = (c: ExtensionContext | undefined): readonly string[] => [
		...OPS,
		...(planHost(c) ? PLAN_OPS : []),
		...(planHost(c) && planNotesSupported ? ["plan-notes"] : []),
		...SESSION_OPS.filter(op => supportsSessionOp(op, c, pi)),
	];

	/** The registry text, at most MAX_REGISTRY_BYTES: a long plan title is cut to fit, never the other fields. */
	const registryText = (c: ExtensionContext): string => {
		const build = (plan: { reviewId: string; title: string; openedAt: string } | null): string =>
			`${JSON.stringify({
				v: PROTOCOL_VERSION,
				versions: PROTOCOL_VERSIONS,
				nonce,
				pid: process.pid,
				socket: socketPath,
				sessionId: currentSessionId(),
				generation,
				plugin: { name: PLUGIN_NAME, version: PLUGIN_VERSION },
				ops: opsFor(c),
				planReview: plan,
				askReview: askReview ? { askId: askReview.askId, openedAt: askReview.openedAt } : null,
				writtenAt: new Date().toISOString(),
			})}\n`;
		if (!planReview) return build(null);
		const { reviewId, openedAt } = planReview;
		const title = fitTitle(planReview.title, t => Buffer.byteLength(build({ reviewId, title: t, openedAt })) <= MAX_REGISTRY_BYTES);
		if (title === undefined) {
			logEvent("plan_registry_dropped");
			return build(null);
		}
		return build({ reviewId, title, openedAt });
	};

	const writeRegistry = (): void => {
		if (!server || !listening || !registryPath || !ctx) return;
		writeFileAtomic(registryPath, registryText(ctx));
	};

	/** writeRegistry for callers that must not fail because of it (an answer that was already accepted). */
	const publishRegistry = (): void => {
		try {
			writeRegistry();
		} catch (error) {
			logEvent("registry_publish_error", failure(error));
		}
	};

	/**
	 * Brings `planReview` in line with the host's getPendingPlanReview, which is the authority: a delayed resolve for a
	 * review that was replaced, a compaction approval, a review that opened before this plugin was bound. A review
	 * stays the same (content, openedAt) while its id does. Returns whether the review's id changed.
	 */
	const reconcilePlan = (c: ExtensionContext): boolean => {
		const before = planReview?.reviewId;
		const host = planHost(c);
		if (!host) {
			planReview = undefined;
		} else {
			let pending: unknown;
			try {
				pending = host.getPendingPlanReview();
			} catch (error) {
				logEvent("plan_reconcile_error", failure(error));
				return false; // an unreadable host proves nothing: keep what is known
			}
			const review = pending === undefined || pending === null ? undefined : readReview(pending);
			if (!review || review.sessionId !== currentSessionId()) planReview = undefined;
			else if (planReview?.reviewId !== review.reviewId) planReview = { ...review, openedAt: new Date().toISOString() };
		}
		return planReview?.reviewId !== before;
	};

	/** reconcilePlan, then rewrites the registry when the review appeared, changed or went. */
	const syncPlan = (c: ExtensionContext): void => {
		if (reconcilePlan(c)) publishRegistry();
	};

	const clearIdle = (client: Client): void => {
		if (client.idle !== undefined) {
			try {
				ctx?.clearTimer(client.idle);
			} catch {}
			client.idle = undefined;
		}
	};

	/** (Re)arms the idle deadline. ctx.setTimeout contains a throwing callback and is cleared on shutdown. */
	const touch = (client: Client): void => {
		clearIdle(client);
		if (client.dead || !ctx) return;
		client.idle = ctx.setTimeout(() => {
			if (client.pending > 0) touch(client);
			else client.sock.destroy();
		}, IDLE_TIMEOUT_MS);
	};

	const clearFrame = (client: Client): void => {
		if (client.frame !== undefined) {
			try {
				ctx?.clearTimer(client.frame);
			} catch {}
			client.frame = undefined;
		}
	};

	/** Starts the deadline of the frame now being received; a client that cannot finish one in time is dropped. */
	const armFrame = (client: Client): void => {
		clearFrame(client);
		if (client.dead || !ctx) return;
		client.frame = ctx.setTimeout(() => client.sock.destroy(), FRAME_DEADLINE_MS);
	};

	/**
	 * Sends one last frame and closes. The socket is destroyed once the frame is flushed and, whatever the peer does
	 * with its side, after CLOSE_GRACE_MS: a peer that never closes cannot keep the descriptor.
	 */
	const sayGoodbye = (sock: net.Socket, body: Record<string, unknown>): void => {
		try {
			sock.end(`${JSON.stringify({ v: PROTOCOL_VERSION, ...body })}\n`, guarded("goodbye", () => sock.destroy()));
		} catch {
			sock.destroy();
			return;
		}
		try {
			ctx?.setTimeout(() => sock.destroy(), CLOSE_GRACE_MS);
		} catch {}
	};

	const reply = (sock: net.Socket, body: Record<string, unknown>): void => {
		if (sock.destroyed) return;
		try {
			if (sock.writableLength > MAX_WRITE_BACKLOG) {
				sock.destroy();
				return;
			}
			let text = JSON.stringify({ v: PROTOCOL_VERSION, ...body });
			if (Buffer.byteLength(text) > MAX_FRAME_BYTES) {
				const id = typeof body.id === "number" || (typeof body.id === "string" && body.id.length <= 128) ? body.id : null;
				text = JSON.stringify({ v: PROTOCOL_VERSION, id, ok: false, code: "internal", message: "the reply would exceed the frame limit" });
			}
			sock.write(`${text}\n`);
		} catch {}
	};

	const closeAll = (reason: string): void => {
		const had = server !== undefined || clients.size > 0;
		if (had) failPlanNotes("control disabled");
		for (const client of clients) {
			client.dead = true;
			clearIdle(client);
			clearFrame(client);
			sayGoodbye(client.sock, { id: null, ok: false, code: "closing", message: reason });
		}
		clients.clear();
		if (server) {
			try {
				server.close();
			} catch {}
			server = undefined;
		}
		for (const p of [socketPath, registryPath]) {
			if (!p) continue;
			try {
				fs.unlinkSync(p);
			} catch {}
		}
		socketPath = "";
		registryPath = "";
		listening = false;
		// Whatever the closed listener still has queued or hung must not count against, or block, the next one.
		listenerEpoch += 1;
		outstanding = 0;
		mutationChain = Promise.resolve();
		if (had) logEvent("closed", { reason });
	};

	// -- operations ---------------------------------------------------------------------------------------------------

	const requireCtx = (): ExtensionContext => {
		if (!ctx) throw new ControlError("not_ready", "session not bound yet");
		return ctx;
	};

	const snapshot = (c: ExtensionContext) => ({
		sessionId: currentSessionId(),
		generation,
		name: pi.getSessionName() ?? null,
		busy: !c.isIdle(),
		compacting: isCompacting(),
		pid: process.pid,
		protocol: PROTOCOL_VERSION,
		protocols: PROTOCOL_VERSIONS,
		ops: opsFor(c),
		plugin: { name: PLUGIN_NAME, version: PLUGIN_VERSION },
	});

	const checkTarget = (req: Request): void => {
		const live = { sessionId: currentSessionId(), generation };
		if (req.sessionId !== live.sessionId) throw new ControlError("stale_session", "sessionId does not match the live session", live);
		if (req.generation !== generation) throw new ControlError("stale_generation", "generation does not match", live);
	};
	const pruneMessages = (): void => {
		for (const [id, entry] of deliveredMessages) if (Date.now() - entry.at >= 15 * 60_000) deliveredMessages.delete(id);
	};
	const sendMessage = async (c: ExtensionContext, args: Record<string, unknown>, answering = false, internal = false, onSettled?: (result: Delivery) => void): Promise<Delivery & { duplicate?: boolean }> => {
		const { text, messageId } = args;
		if (typeof text !== "string" || !text.trim() || text.length > 8000) throw new ControlError("invalid_args", "text must contain 1 to 8000 characters");
		if (typeof messageId !== "string" || !(internal ? /^~plan-[a-f0-9]{64}$/.test(messageId) : /^[A-Za-z0-9_-]{1,128}$/.test(messageId))) throw new ControlError("invalid_args", "invalid messageId");
		const askId = answering ? args.askId : undefined;
		if (answering && (typeof askId !== "string" || !askId)) throw new ControlError("invalid_args", "invalid askId");
		pruneMessages();
		const previous = deliveredMessages.get(messageId);
		if (previous) {
			if (previous.askId !== askId || previous.text !== text) throw new ControlError("invalid_args", "messageId belongs to another message");
			return { ...previous.result, duplicate: true };
		}
		if (typeof askId === "string") {
			const prior = askAnswers.get(askId);
			if (prior && prior.state !== "failed") throw new ControlError("question_closed", "the question was already answered");
		}
		if (answering && (!askReview || askReview.askId !== askId)) throw new ControlError("changed", "the question changed");
		const admitted = Promise.withResolvers<void>();
		const answer: AskGuard | undefined = typeof askId === "string" ? { id: messageId, state: "pending" } : undefined;
		const entry = { at: Date.now(), ...(typeof askId === "string" ? { askId } : {}), text, result: { state: "pending" } as Delivery, admit: admitted.resolve, onSettled, answer };
		deliveredMessages.set(messageId, entry);
		if (typeof askId === "string" && answer) {
			askAnswers.set(askId, answer);
			if (askAnswers.size > 256) askAnswers.delete(askAnswers.keys().next().value as string);
		}
		if (deliveredMessages.size > 256) deliveredMessages.delete(deliveredMessages.keys().next().value as string);
		let returned: unknown;
		try {
			returned = pi.sendUserMessage(text);
		} catch {
			entry.result = { state: "failed", reason: "omp did not accept the message" };
			if (answer) answer.state = "failed";
			return entry.result;
		}
		let settle: Promise<void> | undefined;
		if (returned && typeof returned === "object" && "then" in returned && typeof returned.then === "function") {
			settle = guardedAsync("message_admission", async () => {
				try {
					await returned;
					if (entry.result.state === "pending") {
						entry.result = { state: "admitted" };
						if (answer) answer.state = "admitted";
						entry.onSettled?.(entry.result);
					}
				} catch {
					if (entry.result.state === "pending") {
						entry.result = { state: "failed", reason: "omp did not accept the message" };
						if (answer) answer.state = "failed";
						entry.onSettled?.(entry.result);
					}
				}
			});
		}
		const deadline = Promise.withResolvers<void>();
		const timer = c.setTimeout(() => deadline.resolve(), 5000);
		await Promise.race(settle ? [settle, admitted.promise, deadline.promise] : [admitted.promise, deadline.promise]);
		c.clearTimer(timer);
		return { ...entry.result };
	};


	const runOp = async (req: Request): Promise<unknown> => {
		const c = requireCtx();
		switch (req.op) {
			case "snapshot":
				return snapshot(c);
			case "ask-review":
				if (!askReview) return { open: false };
				if (askReview.tooLarge) return { open: true, askId: askReview.askId, tooLarge: true };
				return { open: true, askId: askReview.askId, ...askReview.args };
			case "ask-answer":
				return sendMessage(c, req.args, true);
			case "send-message":
				return sendMessage(c, req.args);
			case "send-status": {
				const id = req.args.messageId;
				if (typeof id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(id)) throw new ControlError("invalid_args", "invalid messageId");
				pruneMessages();
				return deliveredMessages.get(id)?.result ?? { state: "unknown" };
			}
			case "plan-notes":
				return startPlanNotes(c, req.args);
			case "abort": {
				const wasBusy = !c.isIdle();
				if (wasBusy) c.abort();
				return { aborted: wasBusy };
			}
			case "rename": {
				const name = cleanName(req.args.name);
				const before = pi.getSessionName() ?? null;
				await pi.setSessionName(name);
				const after = pi.getSessionName() ?? null;
				if (after !== name) throw new ControlError("not_renamed", "omp kept the previous name", { before, after });
				return { before, name: after };
			}
			case "plan-review":
				return readPlan(c, req);
			case "plan-answer":
				return answerPlan(c, req.args);
			case "session-info":
				return sessionInfo(c);
			case "compact":
				return compactSession(c, req.args);
			case "set-model":
				return setModel(c, req.args);
			case "set-thinking":
				return setThinking(c, req.args);
			case "set-mode":
				return setMode(c, req.args);
			case "set-advisor":
				return setAdvisor(c, req.args);
			default:
				throw new ControlError("unknown_op", `unknown op ${req.op}`);
		}
	};

	/** One piece of the pinned plan text; see docs/protocol/control-1.md, `plan-review`. */
	const readPlan = (c: ExtensionContext, req: Request): unknown => {
		syncPlan(c);
		const review = planReview;
		if (!review) return { open: false };
		const content = review.planContent;
		const total = content.length; // UTF-16 code units
		const offset = req.args.offset === undefined ? 0 : req.args.offset;
		if (typeof offset !== "number" || !Number.isInteger(offset) || offset < 0 || offset > total) {
			throw new ControlError("invalid_args", "offset must be an integer from 0 to the plan's length");
		}
		if (offset > 0 && offset < total && isHigh(content.charCodeAt(offset - 1)) && isLow(content.charCodeAt(offset))) {
			throw new ControlError("invalid_args", "offset falls inside a character");
		}
		const result = (end: number) => ({
			open: true,
			reviewId: review.reviewId,
			title: review.title,
			planFilePath: review.planFilePath,
			options: review.options,
			tiers: review.tiers,
			total,
			offset,
			nextOffset: end,
			done: end === total,
			chunk: content.slice(offset, end),
		});
		// The whole reply, envelope included, is serialized to measure it: JSON escaping makes a character cost 1 to 6 bytes.
		const fits = (end: number): boolean => Buffer.byteLength(JSON.stringify({ v: PROTOCOL_VERSION, id: req.id, ok: true, result: result(end) })) <= MAX_FRAME_BYTES;
		if (!fits(offset)) throw new ControlError("too_large", "the review's metadata does not fit in one frame");
		// The terminal chunk is measured on its own (`done` is true there and the text is the longest), then the search
		// runs over complete-character boundaries only: a lone surrogate is escaped to 6 bytes, so measuring a position
		// inside a pair would wrongly reject text that fits.
		// (A remainder longer than a frame cannot fit, so it is never serialized: every candidate is at most one frame of text.)
		if (total - offset <= MAX_FRAME_BYTES && fits(total)) return result(total);
		const boundary = (end: number): number => (isHigh(content.charCodeAt(end - 1)) && isLow(content.charCodeAt(end)) ? end + 1 : end);
		let lo = offset; // always a boundary that fits
		let hi = Math.min(total - 1, offset + MAX_FRAME_BYTES - 1); // a unit costs at least one byte, so a full frame of text cannot fit
		while (lo < hi) {
			const mid = Math.ceil((lo + hi) / 2);
			const end = boundary(mid); // > lo, and at most hi + 1
			if (end < total && fits(end)) lo = end;
			else hi = mid - 1;
		}
		if (lo === offset) throw new ControlError("too_large", "the review's metadata leaves no room for text");
		return result(lo);
	};

	const answerPlan = (c: ExtensionContext, args: Record<string, unknown>): unknown => {
		const host = planHost(c);
		if (!host) throw new ControlError("unknown_op", "unknown op");
		const { reviewId, choice, feedback, role } = args;
		if (typeof reviewId !== "string" || reviewId === "" || reviewId.length > MAX_REVIEW_ID_CHARS) {
			throw new ControlError("invalid_args", `reviewId must be a string of 1 to ${MAX_REVIEW_ID_CHARS} characters`);
		}
		if (typeof choice !== "string" || !PLAN_CHOICES.includes(choice as PlanChoice)) {
			throw new ControlError("invalid_args", `choice must be one of ${PLAN_CHOICES.join(", ")}`);
		}
		if (feedback !== undefined && (typeof feedback !== "string" || feedback.length > MAX_FEEDBACK_CHARS)) {
			throw new ControlError("invalid_args", `feedback must be a string of at most ${MAX_FEEDBACK_CHARS} characters`);
		}
		if (choice === "refine" && (typeof feedback !== "string" || feedback.trim() === "")) {
			throw new ControlError("invalid_args", "refine needs non-blank feedback");
		}
		if (role !== undefined) {
			if (typeof role !== "string" || role === "" || role.length > MAX_ROLE_CHARS) {
				throw new ControlError("invalid_args", `role must be a string of 1 to ${MAX_ROLE_CHARS} characters`);
			}
			if (!Object.hasOwn(APPROVALS, choice)) throw new ControlError("invalid_args", "role applies only to execute, compact and keep");
		}
		syncPlan(c);
		const review = planReview;
		if (!review || review.reviewId !== reviewId) throw new ControlError("not_accepted", NOT_ACCEPTED_MESSAGE);
		if (role !== undefined && !review.tiers?.some(t => t.role === role)) {
			throw new ControlError("invalid_args", "role does not name a tier of this review");
		}
		const option = review.options.find(o => o.id === choice);
		if (!option || option.disabled) throw new ControlError("not_accepted", NOT_ACCEPTED_MESSAGE);
		const input: { feedback?: string; role?: string } = {};
		if (choice === "refine") input.feedback = feedback as string;
		if (role !== undefined) input.role = role as string;
		const path = review.planFilePath;
		const approval = Object.hasOwn(APPROVALS, choice);
		if (approval && pendingApprovals.has(path)) throw new ControlError("busy", "another approval of this plan is still starting");
		const token = approval ? Symbol() : undefined;
		if (token) {
			const pending = pendingApprovals.get(path) ?? new Set<symbol>();
			pending.add(token);
			pendingApprovals.set(path, pending);
			if (choice === "execute") executeApprovals.set(token, false);
			if (planNotes?.reviewId === reviewId && planNotes.state === "pending") planNotes.approvalToken = token;
			c.setTimeout(() => {
				if (!pendingApprovals.get(path)?.has(token)) return;
				removePendingApproval(path, token);
				if (planNotes?.approvalToken === token && planNotes.state === "failed" && planNotes.reason === "plan did not start within 5 minutes") {
					planNotes.reason = "the plan did not start; compaction was cancelled";
					planNotes.at = new Date().toISOString();
				}
			}, 30 * 60_000);
		}
		let accepted: boolean;
		try {
			accepted = host.resolvePlanReview(reviewId, choice, input); // never retried
		} catch (error) {
			if (token) removePendingApproval(path, token);
			throw error;
		}
		syncPlan(c);
		if (accepted !== true) {
			if (token) removePendingApproval(path, token);
			throw new ControlError("not_accepted", NOT_ACCEPTED_MESSAGE);
		}
		return { accepted: true };
	};

	const removePendingApproval = (path: string, token?: symbol): void => {
		const pending = pendingApprovals.get(path);
		if (!pending) return;
		const selected = token ?? pending.values().next().value;
		if (selected) pending.delete(selected);
		if (selected) executeApprovals.delete(selected);
		if (pending.size === 0) pendingApprovals.delete(path);
	};

	const notesStatus = (item: PlanNotesState) => ({
		requestId: item.requestId, reviewId: item.reviewId, choice: item.choice, approved: item.approved, state: item.state, ...(item.reason ? { reason: item.reason } : {}), at: item.at,
	});

	const startPlanNotes = (c: ExtensionContext, args: Record<string, unknown>): unknown => {
		const { requestId, notes, reviewId, choice } = args;
		if (typeof requestId !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(requestId)) throw new ControlError("invalid_args", "invalid requestId");
		if (typeof notes !== "string" || !notes.trim() || notes.length > 8000) throw new ControlError("invalid_args", "invalid notes");
		if (typeof reviewId !== "string" || typeof choice !== "string" || !Object.hasOwn(APPROVALS, choice)) throw new ControlError("invalid_args", "invalid approval");
		for (const [id, receipt] of planNotesReceipts) {
			if (receipt.state !== "pending" && Date.now() - Date.parse(receipt.at) >= 15 * 60_000) planNotesReceipts.delete(id);
		}
		const previous = planNotesReceipts.get(requestId);
		if (previous) {
			if (previous.reviewId !== reviewId || previous.choice !== choice || previous.notesDigest !== crypto.createHash("sha256").update(Buffer.from(notes, "utf16le")).digest("hex")) throw new ControlError("invalid_args", "requestId belongs to another approval");
			return { approved: previous.approved, requestId, notes: previous.state, ...(previous.reason ? { reason: previous.reason } : {}), duplicate: true };
		}
		syncPlan(c);
		const review = planReview;
		if (!review || review.reviewId !== reviewId) throw new ControlError("changed", "the plan review changed");
		if (!review.planFilePath) throw new ControlError("changed", "the plan has no path");
		const item: PlanNotesState = { requestId, reviewId, choice, approved: false, path: review.planFilePath, notes, notesDigest: crypto.createHash("sha256").update(Buffer.from(notes, "utf16le")).digest("hex"), state: "pending", at: new Date().toISOString(), expectedSessionId: currentSessionId(), switches: 0, expectFirst: choice !== "execute", epoch: listenerEpoch };
		planNotesReceipts.set(requestId, item);
		if (planNotesReceipts.size > 256) planNotesReceipts.delete(planNotesReceipts.keys().next().value as string);
		if (pendingApprovals.has(item.path)) {
			item.state = "failed";
			item.reason = "another approval of this plan is still starting";
			if (!planNotes || planNotes.state !== "pending") planNotes = item;
			item.notes = "";
			return { approved: false, requestId, notes: "failed", reason: item.reason };
		}
		if (planNotes?.state === "pending") {
			planNotesReceipts.delete(requestId);
			throw new ControlError("busy", "another plan notes request is pending");
		}
		planNotes = item;
		try {
			answerPlan(c, { reviewId, choice, ...(args.role === undefined ? {} : { role: args.role }) });
			item.approved = true;
		} catch (error) {
			planNotesReceipts.delete(requestId);
			planNotes = undefined;
			throw error;
		}
		item.timer = c.setTimeout(() => {
			if (item.state !== "pending") return;
			failPlanNotes("the plan did not start; compaction was cancelled");
			if (item.approvalToken) removePendingApproval(item.path, item.approvalToken);
		}, 30 * 60_000);
		return { approved: true, requestId, notes: "pending" };
	};

	const deliverPlanNotes = (c: ExtensionContext): void => {
		const item = planNotes;
		if (!item || item.state !== "pending" || !item.promptSessionId || item.delivering || isCompacting()) return;
		if (off() || stopped || listenerEpoch !== item.epoch) return failPlanNotes("control disabled");
		item.delivering = true;
		if (item.timer !== undefined) c.clearTimer(item.timer);
		const messageId = `~plan-${crypto.createHash("sha256").update(item.requestId).digest("hex")}`;
		void guardedAsync("plan_notes", async () => {
			try {
				if (item.state !== "pending" || off() || stopped || listenerEpoch !== item.epoch) throw new ControlError("disabled", "control disabled");
				if (currentSessionId() !== item.promptSessionId || replacing || hostMutationRunning || isCompacting()) throw new ControlError("changed", "the session changed or is busy");
				const result = await sendMessage(requireCtx(), { text: item.notes, messageId }, false, true, result => {
					if (item.state !== "pending" && item.state !== "uncertain") return;
					item.state = result.state === "admitted" ? "sent" : "failed";
					item.reason = result.reason;
					item.at = new Date().toISOString();
				});
				if (item.state !== "pending") return;
				if (result.state === "admitted") item.state = "sent";
				else if (result.state === "pending") {
					item.state = "uncertain";
					item.reason = "notes may not have been delivered; check the session";
				} else {
					item.state = "failed";
					item.reason = result.reason;
				}
			} catch (error) {
				if (item.state === "pending") {
					item.state = "failed";
					item.reason = off() || stopped || listenerEpoch !== item.epoch ? "control disabled" : "notes could not be sent";
				}
			} finally {
				item.at = new Date().toISOString();
				item.notes = "";
			}
		});
	};

	// -- session commands ---------------------------------------------------------------------------------------------

	/** The error for something omp threw or refused. omp's own text can hold paths, so the client gets a fixed message. */
	const hostFailed = (op: string, error: unknown): ControlError => {
		if (error instanceof ControlError) return error;
		logEvent("op_error", { op, ...failure(error) });
		return new ControlError("failed", `omp could not run ${op}`);
	};

	const requireSupport = (op: SessionOp, c: ExtensionContext): void => {
		if (!supportsSessionOp(op, c, pi)) throw new ControlError("unsupported", "this omp does not offer that operation");
	};

	const piApi = pi as unknown as {
		getThinkingLevel?: () => unknown;
		setThinkingLevel?: (level: string) => void;
		setModel?: (model: unknown) => Promise<boolean>;
	};

	const currentModel = (c: ExtensionContext): unknown => {
		const query = (c as unknown as HostSurface).models as { current?: () => unknown } | undefined;
		return isFn(query?.current) ? query?.current?.() : (c as { model?: unknown }).model;
	};
	const availableModels = (c: ExtensionContext): unknown[] => {
		const query = (c as unknown as HostSurface).models as { list?: () => unknown } | undefined;
		const list = isFn(query?.list) ? query?.list?.() : [];
		return Array.isArray(list) ? list : [];
	};
	/**
	 * The models a client may pick, the set the terminal's /model picker offers (omp's authenticated list, ctx.models.list)
	 * in this order: the current model (always, even when the list lacks it), the role models that are in the list
	 * (default, smol, slow ... as configured, resolved by omp), then the rest in omp's order. Capped at MAX_MODELS, so
	 * the first entries are the ones that matter. `session-info` shows exactly this and `set-model` validates against it.
	 */
	const offeredModels = (c: ExtensionContext): Array<{ raw: unknown; ref: ModelRef }> => {
		const out: Array<{ raw: unknown; ref: ModelRef }> = [];
		const seen = new Set<string>();
		const add = (raw: unknown): void => {
			const ref = readModel(raw, true);
			if (!ref || out.length >= MAX_MODELS) return;
			const key = `${ref.provider}\u0000${ref.id}`;
			if (seen.has(key)) return;
			seen.add(key);
			out.push({ raw, ref });
		};
		const available = availableModels(c);
		const inList = new Map<string, unknown>();
		for (const m of available) {
			const ref = readModel(m, false);
			if (ref) inList.set(`${ref.provider}\u0000${ref.id}`, m);
		}
		add(currentModel(c));
		const query = (c as unknown as HostSurface).models as { resolve?: (spec: string) => unknown } | undefined;
		if (isFn(query?.resolve)) {
			for (const role of MODEL_ROLES) {
				let resolved: unknown;
				try {
					resolved = query?.resolve?.(`@${role}`);
				} catch {
					continue;
				}
				const ref = readModel(resolved, false);
				const known = ref ? inList.get(`${ref.provider}\u0000${ref.id}`) : undefined;
				if (known !== undefined) add(known);
			}
		}
		for (const m of available) add(m);
		return out;
	};
	/**
	 * The final selectable models: offeredModels cut (by halving) until the list as advertised fits MAX_MODELS_BYTES. It
	 * depends on the models only, so `session-info` advertises exactly this and `set-model` validates against exactly this.
	 */
	const selectableModels = (c: ExtensionContext): Array<{ raw: unknown; ref: ModelRef }> => {
		const list = offeredModels(c);
		while (list.length > 0 && Buffer.byteLength(JSON.stringify(list.map(o => o.ref))) > MAX_MODELS_BYTES) {
			list.length = Math.floor(list.length / 2);
		}
		return list;
	};
	const currentThinking = (): string | null => {
		const level = isFn(piApi.getThinkingLevel) ? piApi.getThinkingLevel?.() : undefined;
		return typeof level === "string" ? level : null;
	};
	const modeState = (c: ExtensionContext): Record<string, { status?: unknown }> | undefined => {
		const modes = (c as unknown as { sessionModes?: { getState?: () => unknown } }).sessionModes;
		if (!isFn(modes?.getState)) return undefined;
		const state = modes?.getState?.();
		return state && typeof state === "object" ? (state as Record<string, { status?: unknown }>) : undefined;
	};
	const interruptMode = (): "immediate" | "wait" | null => {
		try {
			const value = interruptSetting?.get(pi.pi.settings);
			return value === "immediate" || value === "wait" ? value : null;
		} catch {
			return null;
		}
	};
	const advisorInfo = (c: ExtensionContext): unknown => {
		try {
			const advisor = (c as unknown as { advisor?: { getState?: () => unknown } }).advisor;
			if (!isFn(advisor?.getState)) return null;
			const state = advisor?.getState?.();
			if (!state || typeof state !== "object" || !("enabled" in state) || typeof state.enabled !== "boolean" ||
				!("active" in state) || typeof state.active !== "boolean" || !("advisors" in state) || !Array.isArray(state.advisors)) return null;
			const statuses = ["running", "paused", "quota_exhausted", "error", "no_model"];
			return {
				enabled: state.enabled,
				active: state.active,
				model: readModel("model" in state ? state.model : undefined, false) ?? null,
				advisors: state.advisors.slice(0, 16)
					.filter(a => a && typeof a.name === "string" && a.name !== "")
					.map(a => ({ name: (a.name as string).slice(0, 64), status: typeof a.status === "string" && statuses.includes(a.status) ? a.status : "error" })),
			};
		} catch {
			return null;
		}
	};


	const sessionInfo = (c: ExtensionContext): unknown => {
		requireSupport("session-info", c);
		try {
			const modelObject = currentModel(c);
			const state = modeState(c);
			const modes = { plan: false, goal: false, loop: false, vibe: false };
			const modeStatus: Record<string, string> = {};
			for (const key of MODE_STATUS_KEYS) {
				const status = state?.[key]?.status;
				modes[key] = status === "active";
				modeStatus[key] = typeof status === "string" ? status : "inactive";
			}
			const rawGoal = state?.goal as { objective?: unknown; tokenBudget?: unknown } | undefined;
			const candidate = rawGoal && typeof rawGoal.objective === "string" && rawGoal.objective.length <= MAX_OBJECTIVE_CHARS && rawGoal.objective.isWellFormed()
				? { objective: rawGoal.objective, ...(typeof rawGoal.tokenBudget === "number" && Number.isFinite(rawGoal.tokenBudget) ? { tokenBudget: rawGoal.tokenBudget } : {}) }
				: null;
			const goal = candidate && Buffer.byteLength(JSON.stringify(candidate)) <= 8192 ? candidate : null;
			return {
				model: readModel(modelObject, false) ?? null,
				compacting: isCompacting(),
				thinkingLevel: currentThinking(),
				thinkingLevels: thinkingLevelsOf(modelObject),
				models: selectableModels(c).map(o => o.ref),
				mode: modes,
				modeStatus,
				goal,
				interruptMode: interruptMode(),
				planNotes: planNotes ? notesStatus(planNotes) : null,
				advisor: advisorInfo(c),
			};
		} catch (error) {
			throw hostFailed("session-info", error);
		}
	};

	/** True once a `session_compact` event newer than `since` has been seen, waiting a short while for a late one. */
	const compactionEventSince = (c: ExtensionContext, since: number): Promise<boolean> =>
		new Promise<boolean>(resolve => {
			if (compactionsSeen > since) return resolve(true);
			let timer: Timer | undefined;
			const done = (): void => {
				if (timer !== undefined) c.clearTimer(timer);
				compactionWaiters.delete(done);
				resolve(compactionsSeen > since);
			};
			timer = c.setTimeout(done, COMPACT_EVENT_GRACE_MS);
			compactionWaiters.add(done);
		});

	const compactSession = async (c: ExtensionContext, args: Record<string, unknown>): Promise<unknown> => {
		requireSupport("compact", c);
		const { instructions } = args;
		if (instructions !== undefined && (typeof instructions !== "string" || instructions.length > MAX_INSTRUCTIONS_CHARS)) {
			throw new ControlError("invalid_args", `instructions must be a string of at most ${MAX_INSTRUCTIONS_CHARS} characters`);
		}
		// A manual compaction aborts a turn in flight: that is never done as a side effect of this request.
		if (!c.isIdle()) throw new ControlError("failed", "the agent is busy; stop the turn before compacting");
		try {
			// Resolves when the compaction has finished (or there was nothing to compact); rejects when it failed.
			compactionRunning = true;
			const seenBefore = compactionsSeen;
			await (instructions === undefined || instructions.trim() === "" ? c.compact() : c.compact(instructions));
			if (!(await compactionEventSince(c, seenBefore))) throw new ControlError("failed", COMPACT_NOT_DONE_MESSAGE);
		} catch (error) {
			throw hostFailed("compact", error);
		} finally {
			compactionRunning = false;
		}
		return { ok: true };
	};

	const modelArg = (value: unknown, name: string): string => {
		if (typeof value !== "string" || value === "" || value.length > MAX_MODEL_FIELD_CHARS) {
			throw new ControlError("invalid_args", `${name} must be a string of 1 to ${MAX_MODEL_FIELD_CHARS} characters`);
		}
		return value;
	};

	const setModel = async (c: ExtensionContext, args: Record<string, unknown>): Promise<unknown> => {
		requireSupport("set-model", c);
		const provider = modelArg(args.provider, "provider");
		const id = modelArg(args.id, "id");
		try {
			const target = selectableModels(c).find(o => o.ref.provider === provider && o.ref.id === id)?.raw;
			if (!target) throw new ControlError("invalid_args", "that model is not available in this session");
			if ((await piApi.setModel?.(target)) !== true) throw new ControlError("failed", "omp did not switch: no usable API key for that model");
			const now = readModel(currentModel(c), false);
			if (now && (now.provider !== provider || now.id !== id)) throw new ControlError("failed", "omp kept the previous model");
			return { model: now ?? { provider, id } };
		} catch (error) {
			throw hostFailed("set-model", error);
		}
	};
	const setAdvisor = async (c: ExtensionContext, args: Record<string, unknown>): Promise<unknown> => {
		requireSupport("set-advisor", c);
		const hasEnabled = Object.hasOwn(args, "enabled");
		const hasProvider = Object.hasOwn(args, "provider");
		const hasId = Object.hasOwn(args, "id");
		if ((hasEnabled && typeof args.enabled !== "boolean") || hasProvider !== hasId || (!hasEnabled && !hasProvider)) {
			throw new ControlError("invalid_args", "set-advisor needs enabled, or provider and id");
		}
		const provider = hasProvider ? modelArg(args.provider, "provider") : undefined;
		const id = hasId ? modelArg(args.id, "id") : undefined;
		try {
			const advisor = (c as unknown as { advisor: { setEnabled: (enabled: boolean) => unknown; setModel: (model: unknown) => unknown } }).advisor;
			const target = provider !== undefined && id !== undefined
				? selectableModels(c).find(o => o.ref.provider === provider && o.ref.id === id)?.raw
				: undefined;
			if (provider !== undefined && !target) throw new ControlError("invalid_args", "that model is not available in this session");
			if (hasEnabled) await advisor.setEnabled(args.enabled as boolean);
			if (target !== undefined) {
				try {
					await advisor.setModel(target);
				} catch (error) {
					const message = error instanceof Error ? error.message : "";
					if (message.startsWith("turn the advisor on first")) throw new ControlError("invalid_args", message);
					if (message.startsWith("the advisor did not start on that model")) throw new ControlError("failed", message);
					throw error;
				}
			}
			return { advisor: advisorInfo(c) };
		} catch (error) {
			throw hostFailed("set-advisor", error);
		}
	};


	const setThinking = (c: ExtensionContext, args: Record<string, unknown>): unknown => {
		requireSupport("set-thinking", c);
		const { level } = args;
		if (typeof level !== "string" || level === "" || level.length > 32) throw new ControlError("invalid_args", "level must be a non-empty string");
		try {
			const levels = thinkingLevelsOf(currentModel(c));
			if (!levels.includes(level)) throw new ControlError("invalid_args", "level is not available for the current model", { thinkingLevels: levels });
			piApi.setThinkingLevel?.(level);
			return { thinkingLevel: currentThinking() };
		} catch (error) {
			throw hostFailed("set-thinking", error);
		}
	};

	const setMode = async (c: ExtensionContext, args: Record<string, unknown>): Promise<unknown> => {
		requireSupport("set-mode", c);
		const { mode, action, objective } = args;
		if (typeof mode !== "string" || !Object.hasOwn(MODE_ACTIONS, mode)) throw new ControlError("invalid_args", "mode must be plan, vibe, goal or loop");
		if (typeof action !== "string" || !(MODE_ACTIONS[mode] as readonly string[]).includes(action)) {
			throw new ControlError("invalid_args", `${mode} accepts only: ${(MODE_ACTIONS[mode] as readonly string[]).join(", ")}`);
		}
		if (objective !== undefined && (typeof objective !== "string" || objective.length > MAX_OBJECTIVE_CHARS || !objective.isWellFormed())) {
			throw new ControlError("invalid_args", `objective must be a string of at most ${MAX_OBJECTIVE_CHARS} characters`);
		}
		const goalEnter = mode === "goal" && action === "enter";
		if (goalEnter && (typeof objective !== "string" || objective.trim() === "")) throw new ControlError("invalid_args", "entering goal mode needs a non-blank objective");
		if (!goalEnter && objective !== undefined) throw new ControlError("invalid_args", "objective applies only to entering goal mode");
		const request = goalEnter ? { mode, action, objective } : { mode, action };
		try {
			const modes = (c as unknown as { sessionModes: { set(request: unknown): Promise<unknown> } }).sessionModes;
			const result = (await modes.set(request)) as { ok?: unknown; changed?: unknown; code?: unknown } | undefined;
			if (result?.ok === true) return { changed: result.changed === true };
			const code = typeof result?.code === "string" && /^[a-z_]{1,32}$/.test(result.code) ? result.code : "refused";
			throw new ControlError("failed", `omp refused the change: ${code}`);
		} catch (error) {
			throw hostFailed("set-mode", error);
		}
	};

	/** Runs the operation, but not for longer than OP_TIMEOUT_MS: a hung omp call must not wedge control. */
	const runWithDeadline = async (c: ExtensionContext, req: Request): Promise<unknown> => {
		let timer: Timer | undefined;
		const deadline = new Promise<never>((_resolve, reject) => {
			const limit = req.op === "compact" ? COMPACT_TIMEOUT_MS : OP_TIMEOUT_MS;
			const note = req.op === "compact" ? "; it may still be running" : "";
			timer = c.setTimeout(() => reject(new ControlError("timeout", `omp did not finish ${req.op} within ${limit / 1000} s${note}`)), limit);
		});
		// A mutation keeps the host busy until omp settles it, however long that takes: the flag is cleared by the
		// operation itself, not by this deadline, so a timed-out change cannot overlap the next one.
		const work = MUTATING[req.op] && req.op !== "send-message" && req.op !== "ask-answer"
			? (async () => {
					hostMutationRunning = true;
					try {
						return await runOp(req);
					} finally {
						hostMutationRunning = false;
					}
				})()
			: runOp(req);
		try {
			return await Promise.race([work, deadline]);
		} finally {
			if (timer !== undefined) c.clearTimer(timer);
		}
	};

	const handleLine = (client: Client, line: string): void => {
		const sock = client.sock;
		let raw: unknown;
		try {
			raw = JSON.parse(line);
		} catch {
			return reply(sock, { id: null, ok: false, code: "bad_json", message: "frame is not JSON" });
		}
		if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
			return reply(sock, { id: null, ok: false, code: "bad_request", message: "frame must be an object" });
		}
		const frame = raw as Record<string, unknown>;
		const id =
			(typeof frame.id === "number" && Number.isFinite(frame.id)) || (typeof frame.id === "string" && frame.id.length <= 128) ? frame.id : null;
		const fail = (code: string, message: string, detail?: unknown) =>
			reply(sock, { id, ok: false, code, message, ...(detail !== undefined ? { detail } : {}) });
		if (frame.v !== PROTOCOL_VERSION) return fail("bad_version", `v must be ${PROTOCOL_VERSION}`, { supported: PROTOCOL_VERSIONS });
		if (id === null) return fail("bad_request", "id must be a string (<=128) or number");
		if (frame.nonce !== nonce) return fail("bad_nonce", "nonce does not match this process");
		if (off()) {
			fail("disabled", "session control is turned off");
			closeAll("off");
			return;
		}
		if (typeof frame.op !== "string" || !Object.hasOwn(MUTATING, frame.op)) return fail("unknown_op", "unknown op");
		if (client.reserved && frame.op !== "abort" && frame.op !== "snapshot") return fail("too_many_clients", "reserved for snapshot and abort");
		if ((PLAN_OPS as readonly string[]).includes(frame.op) && ctx && !planHost(ctx)) return fail("unknown_op", "unknown op");
		if (frame.op === "plan-notes" && (!ctx || !planHost(ctx) || !planNotesSupported)) return fail("unsupported", "plan notes need omp 18.19 or newer");
		if (isSessionOp(frame.op) && ctx && !supportsSessionOp(frame.op, ctx, pi)) return fail("unsupported", "this omp does not offer that operation");
		if (frame.args !== undefined && (typeof frame.args !== "object" || frame.args === null || Array.isArray(frame.args))) {
			return fail("invalid_args", "args must be an object");
		}
		const req: Request = {
			id,
			sessionId: typeof frame.sessionId === "string" ? frame.sessionId : null,
			generation: typeof frame.generation === "number" ? frame.generation : null,
			op: frame.op,
			args: (frame.args as Record<string, unknown> | undefined) ?? {},
		};
		if (MUTATING[req.op] && replacing) return fail("busy", REPLACING_MESSAGE);
		if (MUTATING[req.op] && compactionRunning) return fail("mutation_pending", "a compaction is still running in omp");
		if (outstanding >= MAX_OUTSTANDING + (req.op === "abort" || (client.reserved && req.op === "snapshot") ? 1 : 0)) return fail("overloaded", "too many requests in flight");
		outstanding += 1;
		client.pending += 1;
		const epoch = listenerEpoch;
		const execute = async (): Promise<void> => {
			try {
				// A client that has gone, or a listener that was closed meanwhile, is owed no work: skip the mutation.
				if (client.dead || epoch !== listenerEpoch) return;
				if (off()) throw new ControlError("disabled", "session control is turned off");
				if (Object.hasOwn(TARGET_CHECKED, req.op)) checkTarget(req); // re-checked when the operation actually starts
				if (MUTATING[req.op] && replacing) throw new ControlError("busy", REPLACING_MESSAGE); // re-checked when it starts
				if (MUTATING[req.op] && hostMutationRunning) throw new ControlError("mutation_pending", "an earlier change has not finished in omp yet");
				reply(sock, { id, ok: true, result: await runWithDeadline(requireCtx(), req) });
			} catch (error) {
				// omp's own error text can hold file paths: clients get a fixed message, events.log gets the text.
				const ce =
					error instanceof ControlError ? error : new ControlError("internal", "omp failed while running the operation");
				if (!(error instanceof ControlError)) logEvent("op_error", { op: req.op, ...failure(error) });
				if (ce.code === "timeout") logEvent("op_timeout", { op: req.op });
				reply(sock, { id, ok: false, code: ce.code, message: ce.message, ...(ce.detail !== undefined ? { detail: ce.detail } : {}) });
			} finally {
				if (epoch === listenerEpoch) outstanding -= 1;
				client.pending -= 1;
			}
		};
		if (req.op === "send-message" || req.op === "ask-answer") {
			mutationChain = mutationChain.then(() => { void guardedAsync("mutation", execute); });
		} else if (MUTATING[req.op]) mutationChain = mutationChain.then(() => guardedAsync("mutation", execute));
		else void guardedAsync("op", execute);
	};

	const onConnection = (sock: net.Socket): void => {
		sock.on("error", guarded("socket_error", () => sock.destroy()));
		if (off()) return sayGoodbye(sock, { id: null, ok: false, code: "disabled", message: "session control is turned off" });
		const ordinary = [...clients].filter(client => !client.reserved).length;
		const reserved = ordinary >= MAX_CLIENTS;
		if (reserved && [...clients].some(client => client.reserved)) {
			return sayGoodbye(sock, { id: null, ok: false, code: "too_many_clients", message: `max ${MAX_CLIENTS} plus one abort slot` });
		}
		const client: Client = { sock, buffer: Buffer.alloc(0), idle: undefined, frame: undefined, dead: false, pending: 0, reserved };
		clients.add(client);
		touch(client);
		sock.on(
			"data",
			guarded("data", (chunk: Buffer) => {
				if (client.dead) return;
				touch(client);
				client.buffer = Buffer.concat([client.buffer, chunk]);
				let finishedFrame = false;
				for (;;) {
					const nl = client.buffer.indexOf(0x0a);
					if (nl < 0 || nl > MAX_FRAME_BYTES) break;
					const bytes = client.buffer.subarray(0, nl);
					client.buffer = client.buffer.subarray(nl + 1);
					finishedFrame = true;
					// A failure inside one frame answers that frame and never stops the frames behind it.
					try {
						let line: string | undefined;
						try {
							line = UTF8.decode(bytes);
						} catch {
							reply(sock, { id: null, ok: false, code: "bad_json", message: "frame is not valid UTF-8" });
						}
						if (line !== undefined && line.trim()) handleLine(client, line);
					} catch (error) {
						logEvent("frame_error", failure(error));
						reply(sock, { id: null, ok: false, code: "internal", message: "omp failed while handling the frame" });
					}
					if (client.dead) return;
				}
				if (client.buffer.length > MAX_FRAME_BYTES) {
					client.dead = true;
					client.buffer = Buffer.alloc(0);
					clearFrame(client);
					sayGoodbye(sock, { id: null, ok: false, code: "too_large", message: `frame exceeds ${MAX_FRAME_BYTES} bytes` });
				} else if (client.buffer.length === 0) {
					clearFrame(client);
				} else if (finishedFrame || client.frame === undefined) {
					armFrame(client); // a new, incomplete frame has started: it must finish in time
				}
			}),
		);
		sock.on(
			"close",
			guarded("close", () => {
				client.dead = true;
				clearIdle(client);
				clearFrame(client);
				clients.delete(client);
			}),
		);
	};

	/** A start that did not complete: close whatever exists, record why, tell the user once. */
	const failStart = (kind: string, what: string, error: unknown): void => {
		logEvent(kind, failure(error));
		lastFailure = `${what} (${userError(error)})`;
		closeAll(kind);
		notifyOnce(`could not publish control: ${lastFailure}`, kind);
	};

	const start = (reason: string): void => {
		if (server || stopped || !ctx) return;
		try {
			const st = ensurePrivateDir(CONTROL_DIR, false);
			dirOk = true;
			dirIdentity = `${st.dev}:${st.ino}`;
			removeStaleEntries();
			nonce = crypto.randomBytes(16).toString("hex");
			socketPath = path.join(CONTROL_DIR, `${crypto.randomBytes(8).toString("hex")}.sock`);
			registryPath = path.join(CONTROL_DIR, `${nonce}.json`);
			if (Buffer.byteLength(socketPath) > maxSocketPathBytes()) {
				throw new ControlError("unsafe_path", `the control directory path is too long for a Unix socket (max ${maxSocketPathBytes() - 22} bytes)`);
			}
			const srv = net.createServer(guarded("connection", onConnection));
			srv.maxConnections = MAX_CONNECTIONS;
			srv.on(
				"error",
				guarded("server_error", (error: Error) => {
					logEvent("server_error", failure(error));
					if (server === srv) closeAll("server_error");
				}),
			);
			server = srv;
			const boundPath = socketPath;
			srv.listen(
				boundPath,
				guarded("listen", () => {
					if (server !== srv) return;
					// Binding, securing the socket and publishing the registry are one step: if any part fails, nothing of
					// it is left running.
					try {
						fs.chmodSync(boundPath, 0o600);
					} catch (error) {
						return failStart("socket_mode_error", "the socket could not be secured", error);
					}
					listening = true;
					try {
						writeRegistry(); // published only after the bind succeeded
					} catch (error) {
						return failStart("registry_publish_error", "the registry could not be published", error);
					}
					lastFailure = "";
					everActive = true;
					logEvent("listening", { reason, sessionId: currentSessionId(), generation });
					if (liveDisableProblem) {
						notifyOnce(
							`live disabling through disabledExtensions is unavailable (${liveDisableProblem}); use /session-control off or the off file to close control without a restart`,
							"live_disable",
						);
					}
				}),
			);
		} catch (error) {
			logEvent("start_error", failure(error));
			lastFailure = `could not start (${userError(error)})`;
			notifyOnce(lastFailure);
			server = undefined;
			socketPath = "";
			registryPath = "";
			listening = false;
		}
	};

	/** What is wrong with the running listener, or "": the directory was replaced, or its socket or registry vanished. */
	const listenerProblem = (): string => {
		if (!server) return "";
		let st: fs.Stats;
		try {
			st = fs.lstatSync(CONTROL_DIR);
		} catch {
			return "control_dir_gone";
		}
		if (`${st.dev}:${st.ino}` !== dirIdentity) return "control_dir_replaced";
		if (!listening) return "";
		try {
			if (!fs.lstatSync(socketPath).isSocket()) return "socket_replaced";
		} catch {
			return "socket_gone";
		}
		return existsNoFollow(registryPath) ? "" : "registry_gone";
	};

	/**
	 * Brings the listener in line with the files on disk. Idempotent; called on every relevant change.
	 *
	 * File-system watches can miss a change: one that happens while a watch is still being armed is lost (macOS arms
	 * asynchronously), and events can be dropped under load. So this never relies on events alone:
	 *  - a watcher is armed *before* the state is read, so a change made after the arming is either seen by the read or
	 *    delivered as an event (watch, then recheck);
	 *  - while there is something to wait for (the directory exists, or was in use), it runs again every RECHECK_MS through
	 *    ctx.setTimeout (isolated by omp, unref'd, cleared at shutdown), one timer at a time.
	 */
	const reconcile = (reason: string): void => {
		if (!ctx || stopped) return;
		let dir = inspectDir(CONTROL_DIR);
		if (dir.kind === "ok") {
			unwatchParent();
			watchDir(); // a no-op when the watch is already armed
		}
		const a = activation();
		if (a.state === "ready") {
			const problem = listenerProblem();
			if (problem) {
				// The directory under us was removed or replaced (or our files were): start again from scratch, which
				// also re-arms the watch on whatever is at the path now.
				closeAll(problem);
				unwatchDir();
				return reconcile(`recovered_${problem}`);
			}
			start(reason);
			syncRecheck(true);
			return;
		}
		if (server || clients.size) closeAll(a.state === "off" ? "off" : a.state);
		if (a.state === "unsafe") notifyOnce(a.reason);
		dir = inspectDir(CONTROL_DIR);
		if (dir.kind !== "ok") {
			unwatchDir(); // a missing or unsafe directory cannot be watched
			// A directory that was in use and vanished is waited for through its parent, so that putting it back
			// brings control back without a restart. Arm, then look again: it may have come back meanwhile.
			if (dir.kind === "missing" && everActive && watchParent() && inspectDir(CONTROL_DIR).kind !== "missing") {
				return reconcile(`${reason}_recheck`);
			}
		}
		syncRecheck(dir.kind !== "missing" || everActive);
	};

	/** Keeps exactly one periodic recheck pending while `want`, and none otherwise. */
	const syncRecheck = (want: boolean): void => {
		if (!want || stopped || !ctx) {
			if (recheckTimer !== undefined) {
				try {
					ctx?.clearTimer(recheckTimer);
				} catch {}
				recheckTimer = undefined;
			}
			return;
		}
		if (recheckTimer !== undefined) return;
		recheckTimer = ctx.setTimeout(() => {
			recheckTimer = undefined;
			try {
				reconcile("periodic");
			} catch (error) {
				logEvent("recheck_error", failure(error));
				syncRecheck(true); // a failed look must not end the watching
			}
		}, RECHECK_MS);
	};

	const unwatchParent = (): void => {
		try {
			parentWatcher?.close();
		} catch {}
		parentWatcher = undefined;
	};

	/** Arms the watch on the directory's parent; true when it was armed just now. */
	const watchParent = (): boolean => {
		if (parentWatcher || !SUPPORTED || !CONTROL_DIR) return false;
		try {
			const base = path.basename(CONTROL_DIR);
			// Only the appearance of the directory's name matters; reconcile re-validates everything about it.
			parentWatcher = fs.watch(
				path.dirname(CONTROL_DIR),
				{ persistent: false },
				guarded("parent_watch", (_kind: string, filename: string | null) => {
					if (!filename || filename === base) reconcile("parent_watch");
				}),
			);
			parentWatcher.on("error", guarded("parent_watch_error", () => unwatchParent()));
			return true;
		} catch (error) {
			logEvent("watch_error", failure(error));
			return false;
		}
	};

	const unwatchDir = (): void => {
		try {
			dirWatcher?.close();
		} catch {}
		dirWatcher = undefined;
	};

	const watchDir = (): void => {
		if (dirWatcher || !SUPPORTED || !CONTROL_DIR || inspectDir(CONTROL_DIR).kind !== "ok") return;
		try {
			// Registry writes, sockets and the log in the same directory must not loop: only the marker files matter.
			dirWatcher = fs.watch(
				CONTROL_DIR,
				{ persistent: false },
				guarded("watch", (_kind: string, filename: string | null) => {
					// Other sessions' registries and sockets, temporary files and the log are noise; our own files are not.
					const ours = filename === path.basename(registryPath) || filename === path.basename(socketPath);
					if (filename && CHURN_FILE.test(filename) && !ours) return;
					reconcile("watch");
				}),
			);
			dirWatcher.on("error", guarded("watch_error", () => unwatchDir()));
		} catch (error) {
			logEvent("watch_error", failure(error));
		}
	};

	// A live `disabledExtensions` edit suspends a global extension but never unloads it, and its open sockets keep
	// answering. Watch the setting ourselves and close on our own id. The registry module is omp-internal, so a missing
	// or changed module (or a renamed `lookup`) only loses this one feature.
	const disabledSetting = await optional(async () => (await loadSettingsRegistry()).lookup("disabledExtensions"));
	const interruptSetting = await optional(async () => (await loadSettingsRegistry()).lookup("interruptMode"));
	if (!disabledSetting) liveDisableProblem = "omp's settings registry is not available";
	const isSelfDisabled = (): boolean => {
		try {
			const value = disabledSetting?.get(pi.pi.settings);
			return Array.isArray(value) && value.includes(EXTENSION_ID);
		} catch {
			return false;
		}
	};
	try {
		disabledSetting?.get(pi.pi.settings); // fails here, visibly, if omp changed the shape of the setting
		unsubscribeDisabled = disabledSetting?.listen(
			pi.pi.settings,
			guarded("disabled_setting", () => {
				const now = isSelfDisabled();
				if (now === selfDisabled) return;
				selfDisabled = now;
				reconcile(now ? "disabledExtensions" : "re_enabled");
			}),
		);
	} catch (error) {
		liveDisableProblem = "the settings subscription failed";
		logEvent("listen_error", failure(error));
	}

	const bind = (c: ExtensionContext, reason: string): boolean => {
		if (!isMainTui(c)) return false;
		ctx = c;
		selfDisabled = isSelfDisabled();
		if (settings.warnings.length > 0 && !CONTROL_DIR) notifyOnce(settings.warnings[0] as string);
		syncPlan(c); // a review that opened before this plugin was bound
		reconcile(reason);
		return true;
	};

	pi.on(
		"session_start",
		guarded("session_start", (_event: unknown, c: ExtensionContext) => {
			clearReplacing();
			bind(c, "session_start");
		}),
	);
	// A switch (/new, /resume, /fork) and a branch both replace the session: the registry must follow.
	const onSessionReplaced = (reason: string) => (event: unknown, c: ExtensionContext) => {
		const item = planNotes;
		if (item?.state === "pending") {
			const newSwitch = reason === "session_switch" && !!event && typeof event === "object" && "reason" in event && event.reason === "new";
			if (item.choice !== "execute" || !newSwitch || item.switches !== 0 || item.promptSessionId) {
				failPlanNotes("session changed before the plan started; notes not sent");
				if (item.approvalToken) removePendingApproval(item.path, item.approvalToken);
			} else {
				item.switches = 1;
				item.expectedSessionId = c.sessionManager.getSessionId();
				item.expectFirst = true;
			}
		}
		if (item?.state !== "pending" && item?.approvalToken) removePendingApproval(item.path, item.approvalToken);
		const firstNew = reason === "session_switch" && !!event && typeof event === "object" && "reason" in event && event.reason === "new";
		for (const [path, tokens] of pendingApprovals) {
			for (const token of tokens) {
				if (firstNew && executeApprovals.get(token) === false) executeApprovals.set(token, true);
				else removePendingApproval(path, token);
			}
		}
		clearReplacing(); // the replacement committed: changes are checked against the new generation again
		planReview = undefined; // the old session's review is gone; bind reads the host again
		askReview = undefined;
		askAnswers.clear();
		deliveredMessages.clear();
		if (!bind(c, reason)) return;
		generation += 1;
		writeRegistry();
	};
	// A change we started (set-model awaits before it mutates, a compaction runs for a long time) must land on the session
	// it was meant for: while one is unsettled, even after its request timed out, the terminal may not replace the session.
	const refuseReplacement = (_event: unknown, c: ExtensionContext): { cancel: true } | undefined => {
		if (!ctx || !isMainTui(c)) return undefined;
		if (!(hostMutationRunning || compactionRunning)) {
			// Allowed. Other hooks, the abort and the flush still run before omp commits, and a change admitted in that gap
			// would land on the replacement: refuse changes from here until a lifecycle event (see `replacing`).
			markReplacing();
			return undefined;
		}
		logEvent("replacement_refused");
		try {
			c.ui.notify("session-control: a change requested over the control socket is still running; try again in a moment", "warning");
		} catch {} // the veto never depends on the notice
		return { cancel: true };
	};
	pi.on("session_before_switch", guarded("session_before_switch", refuseReplacement));
	pi.on("session_before_branch", guarded("session_before_branch", refuseReplacement));
	pi.on(
		"session_compact",
		guarded("session_compact", () => {
			compactionsSeen += 1;
			for (const waiter of [...compactionWaiters]) waiter();
		}),
	);
	pi.on("auto_compaction_start", guarded("auto_compaction_start", () => void (autoCompacting = true)));
	pi.on("auto_compaction_end", guarded("auto_compaction_end", (_event: unknown, c: ExtensionContext) => {
		autoCompacting = false;
		if (ctx && isMainTui(c)) deliverPlanNotes(c);
	}));
	pi.on("session_switch", guarded("session_switch", onSessionReplaced("session_switch")));
	pi.on("session_branch", guarded("session_branch", onSessionReplaced("session_branch")));
	pi.on("input", guarded("plan_input", (_event: unknown, c: ExtensionContext) => {
		if (ctx && isMainTui(c)) {
			pendingApprovals.clear();
			executeApprovals.clear();
		}
		if (ctx && isMainTui(c) && planNotes?.state === "pending" && !planNotes.promptSessionId) {
			failPlanNotes("session changed before the plan started; notes not sent");
			if (planNotes.approvalToken) removePendingApproval(planNotes.path, planNotes.approvalToken);
		}
	}));
	pi.on("message_start", guarded("plan_prompt", (event: unknown, c: ExtensionContext) => {
		if (!ctx || !isMainTui(c)) return;
		if (!event || typeof event !== "object" || !("message" in event)) return;
		const message = event.message;
		if (!message || typeof message !== "object") return;
		const role = "role" in message ? message.role : undefined;
		const content = "content" in message && Array.isArray(message.content) ? message.content : [];
		const text = content.map((part: { type?: unknown; text?: unknown }) => part?.type === "text" && typeof part.text === "string" ? part.text : "").join("");
		if (role === "user") {
			const manager = c.sessionManager as unknown as { getBranch?: () => unknown };
			const branch = text.includes('<model agent="') ? manager.getBranch?.() : undefined;
			const tags = new Map<string, string>();
			if (Array.isArray(branch)) {
				for (const value of branch) {
					if (!value || typeof value !== "object" || value.type !== "custom" || value.customType !== "model_mention") continue;
					const data = value.data;
					if (data && typeof data === "object" && typeof data.agent === "string" && typeof data.selector === "string" && typeof data.name === "string") {
						tags.set(data.selector, `<model agent="${data.agent}" name="${data.name}"/>`);
					}
				}
			}
			const models = availableModels(c);
			const exact = new Set(models.flatMap(model => {
				if (!model || typeof model !== "object" || !("provider" in model) || !("id" in model) || typeof model.provider !== "string" || typeof model.id !== "string") return [];
				return [`${model.provider}/${model.id}`];
			}));
			for (const entry of deliveredMessages.values()) {
				if (entry.result.state !== "pending") continue;
				const expanded = entry.text.replace(/(^|\s)\^([^\s^]+)(?=\s|$)/g, (token, delimiter: string, selector: string) => {
					let canonical = selector;
					if (models.length > 0 && !exact.has(selector) && !tags.has(selector)) {
						const colon = selector.lastIndexOf(":");
						if (colon > 0) {
							const base = selector.slice(0, colon);
							const suffix = selector.slice(colon + 1);
							const efforts = ["minimal", "low", "medium", "high", "xhigh", "max"];
							const matches = efforts.filter(effort => effort.startsWith(suffix));
							if (suffix === "auto") canonical = base;
							else if (suffix.length >= 2 && matches.length === 1 && exact.has(base) && !exact.has(`${base}:${matches[0]}`)) canonical = `${base}:${matches[0]}`;
						}
					}
					const tag = tags.get(canonical);
					return tag ? `${delimiter}${tag}` : token;
				});
				if (entry.text === text || expanded === text) {
					entry.result = { state: "admitted" };
					if (entry.answer) entry.answer.state = "admitted";
					entry.onSettled?.(entry.result);
					entry.admit();
					break;
				}
			}
		}
		const marker = "synthetic" in message && message.synthetic === true && (role === "developer" || role === "user") && text.startsWith("Plan approved.")
			? text.match(/\n<plan path="([^"]+)">/) : null;
		const pending = planNotes?.state === "pending" && planNotes.approved && !planNotes.promptSessionId ? planNotes : undefined;
		if (pending) {
			const isMatching = marker?.[1] === pending.path;
			if (c.sessionManager.getSessionId() !== pending.expectedSessionId ||
				(pending.expectFirst && !isMatching) ||
				(role === "user" && !isMatching) ||
				(isMatching && pending.choice === "execute" && pending.switches !== 1)) {
				failPlanNotes("session changed before the plan started; notes not sent");
			}
		}
		if (!marker) return;
		removePendingApproval(marker[1]);
		const item = planNotes;
		if (!item || !item.approved || item.state !== "pending" || item.path !== marker[1] || item.promptSessionId) return;
		item.promptSessionId = c.sessionManager.getSessionId();
		deliverPlanNotes(c);
	}));
	pi.on("tool_execution_start", guarded("ask_start", (event: unknown, c: ExtensionContext) => {
		if (!ctx || !isMainTui(c)) return;
		const e = event as { toolName?: unknown; toolCallId?: unknown; args?: unknown };
		if (e.toolName !== "ask" || typeof e.toolCallId !== "string" || e.toolCallId.length < 1 || e.toolCallId.length > 128 || !e.args || typeof e.args !== "object") return;
		const source = e.args as Record<string, unknown>;
		const args = typeof source.html === "string" ? { html: source.html } : { questions: source.questions };
		const tooLarge = Buffer.byteLength(JSON.stringify(args)) > 32 * 1024;
		askReview = { askId: e.toolCallId, openedAt: new Date().toISOString(), args: tooLarge ? {} : args, tooLarge };
		publishRegistry();
	}));
	pi.on("tool_execution_end", guarded("ask_end", (event: unknown, c: ExtensionContext) => {
		if (!isMainTui(c)) return;
		const e = event as { toolName?: unknown; toolCallId?: unknown };
		if (e.toolName === "ask" && askReview?.askId === e.toolCallId) {
			askReview = undefined;
			publishRegistry();
		}
	}));
	pi.on(
		"session_shutdown",
		guarded("session_shutdown", (_event: unknown, c: ExtensionContext) => {
			if (!isMainTui(c)) return;
			clearReplacing();
			stopped = true;
			unwatchDir();
			unwatchParent();
			syncRecheck(false);
			closeAll("session_shutdown");
			try {
				unsubscribeDisabled?.();
			} catch {}
			unsubscribeDisabled = undefined;
		}),
	);
	// The review is tracked from the events and checked against the host after every one of them. Both handlers are
	// synchronous: omp waits for handlers of plan_review_requested before it reports the resolve.
	pi.on(
		"plan_review_requested",
		guarded("plan_review_requested", (event: unknown, c: ExtensionContext) => {
			if (!ctx || !isMainTui(c) || !planHost(c)) return;
			const review = readReview(event);
			if (review && review.sessionId === currentSessionId() && planReview?.reviewId !== review.reviewId) {
				planReview = { ...review, openedAt: new Date().toISOString() };
				publishRegistry();
			}
			syncPlan(c);
		}),
	);
	pi.on(
		"plan_review_resolved",
		guarded("plan_review_resolved", (event: unknown, c: ExtensionContext) => {
			if (!ctx || !isMainTui(c) || !planHost(c)) return;
			const e = (event ?? {}) as Record<string, unknown>;
			if (planReview && e.reviewId === planReview.reviewId && e.sessionId === planReview.sessionId) {
				planReview = undefined;
				publishRegistry();
			}
			syncPlan(c); // a replacement review, or one the host still holds
		}),
	);

	// -- /session-control ---------------------------------------------------------------------------------------------

	const describe = (): string => {
		const a = activation();
		const lines = [
			`${PLUGIN_NAME} ${PLUGIN_VERSION}, protocol ${PROTOCOL_VERSIONS.join(", ")}`,
			`control directory: ${CONTROL_DIR || "(none)"}`,
			a.state === "ready"
				? server
					? `state: listening, ${clients.size} client(s) connected`
					: lastFailure
						? `state: failed, ${lastFailure}`
						: "state: enabled, not listening in this session"
				: `state: ${a.state} (${a.reason})`,
			liveDisableProblem
				? `live disabling through disabledExtensions: unavailable (${liveDisableProblem}); the off file still works`
				: "live disabling through disabledExtensions: available",
		];
		lines.push(
			planHost(ctx)
				? `plan review: answerable through ${PLAN_OPS.join(", ")}${planReview ? " (a review is open)" : ""}`
				: "plan review: not available (this omp has no getPendingPlanReview and resolvePlanReview)",
		);
		for (const w of settings.warnings) lines.push(`warning: ${w}`);
		return lines.join("\n");
	};

	const COMMAND_USAGE = "usage: /session-control status | on | off";

	pi.registerCommand("session-control", {
		description: "Show or change session control: status | on | off",
		getArgumentCompletions: prefix =>
			["status", "on", "off"].filter(v => v.startsWith(prefix.trim())).map(v => ({ value: v, label: v })),
		handler: async (args: string, c) => {
			const say = (message: string, type: "info" | "warning" | "error" = "info"): void => {
				try {
					c.ui.notify(message, type);
				} catch {}
			};
			try {
				if (!isMainTui(c)) return say("session-control works in the main interactive session only", "warning");
				ctx = c;
				const sub = args.trim().toLowerCase();
				if (sub === "" || sub === "status") return say(describe());
				if (!SUPPORTED || !CONTROL_DIR) {
					const a = activation();
					return say(`session control is unavailable: ${a.state === "ready" ? "unknown" : a.reason}`, "error");
				}
				if (sub === "on") {
					ensurePrivateDir(CONTROL_DIR, true);
					dirOk = true;
					touchPrivateFile(ENABLED_FILE);
					removeIfPresent(OFF_FILE);
					reconcile("command");
					return say(
						`${describe()}\nOther sessions that were already running pick this up only if they watch the directory; restart them otherwise.`,
					);
				}
				if (sub === "off") {
					if (inspectDir(CONTROL_DIR).kind !== "ok") return say("session control is already off: no control directory");
					ensurePrivateDir(CONTROL_DIR, false);
					dirOk = true;
					touchPrivateFile(OFF_FILE);
					reconcile("command");
					return say(`session control is off for every session that watches ${CONTROL_DIR}. \`/session-control on\` turns it back on.`);
				}
				say(COMMAND_USAGE, "warning");
			} catch (error) {
				say(`session-control: ${userError(error)}`, "error");
			}
		},
	});
}
